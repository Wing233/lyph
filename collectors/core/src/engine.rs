use crate::config::private_file;
use crate::{Config, DeviceApi, RemoteConfig, Store, SyncError};
use anyhow::{Context, Result};
use chrono::Utc;
use std::{
    fs::File,
    time::{Duration, Instant},
};

pub struct Engine<A: DeviceApi> {
    pub store: Store,
    config: Config,
    api: A,
    _lock: File,
    last_config: Option<Instant>,
    last_capture: Instant,
    last_heartbeat: Option<Instant>,
}

impl<A: DeviceApi> Engine<A> {
    pub fn new(store: Store, config: Config, api: A) -> Result<Self> {
        let lock = private_file(&store.root.join("engine.lock"), false)?;
        lock.try_lock().context("另一个采集进程已在运行")?;
        if store.state("capture_interval_seconds")?.is_none() {
            store.set_state(
                "capture_interval_seconds",
                &config.capture_interval_seconds.to_string(),
            )?;
        }
        if store.state("cache_limit_bytes")?.is_none() {
            store.set_state("cache_limit_bytes", &config.cache_limit_bytes.to_string())?;
        }
        if !crate::screen::desktop_available() {
            store.set_state("capture_status", "capture_unavailable")?;
        }
        Ok(Self {
            store,
            config,
            api,
            _lock: lock,
            last_config: None,
            last_capture: Instant::now(),
            last_heartbeat: None,
        })
    }

    fn apply_remote(&self, value: RemoteConfig) -> Result<()> {
        if value.upload_concurrency != 1 || !(30..=86400).contains(&value.capture_interval_seconds)
        {
            anyhow::bail!("服务端采集配置无效");
        }
        self.store
            .set_state("remote_paused", if value.paused { "true" } else { "false" })?;
        self.store.set_state(
            "capture_interval_seconds",
            &value.capture_interval_seconds.to_string(),
        )?;
        self.store.set_state(
            "cache_limit_bytes",
            &value.cache_limit_bytes.min(1024 * 1024 * 1024).to_string(),
        )?;
        self.store.set_state("revoked", "false")?;
        Ok(())
    }

    fn record_error(&self, error: &SyncError) -> Result<()> {
        self.store.set_state("last_error", &error.to_string())?;
        if *error == SyncError::Revoked {
            self.store.set_state("revoked", "true")?;
            self.store.set_state("capture_status", "device_revoked")?;
        }
        Ok(())
    }

    pub fn refresh_config(&mut self) -> Result<()> {
        self.last_config = Some(Instant::now());
        match self.api.config() {
            Ok(value) => self.apply_remote(value),
            Err(error) => {
                self.record_error(&error)?;
                Ok(())
            }
        }
    }

    pub fn upload_one(&mut self, timestamp: i64) -> Result<bool> {
        if self.store.status()?.revoked {
            return Ok(false);
        };
        let Some(event) = self.store.next_due(timestamp)? else {
            return Ok(false);
        };
        match self.api.upload(&event) {
            Ok(ack) if ack.id == event.id => {
                self.store.acknowledge(&event.id, &ack.id)?;
                self.store.set_state("last_error", "")?;
                Ok(true)
            }
            Ok(_) => {
                self.store.retry(
                    &event.id,
                    &SyncError::InvalidResponse.to_string(),
                    timestamp,
                )?;
                Ok(false)
            }
            Err(error) => {
                self.record_error(&error)?;
                self.store.retry(&event.id, &error.to_string(), timestamp)?;
                Ok(false)
            }
        }
    }

    pub fn capture_now(&mut self) -> Result<()> {
        let status = self.store.status()?;
        if status.local_paused || status.remote_paused || status.revoked {
            return Ok(());
        };
        let limit = status.cache_limit_bytes.min(self.config.cache_limit_bytes);
        if status.cache_bytes >= limit {
            self.store.set_state("capture_status", "cache_full")?;
            return Ok(());
        }
        self.last_capture = Instant::now();
        match crate::screen::capture_png() {
            Ok(bytes) => {
                if let Err(error) = self.store.enqueue(
                    "screenshot",
                    "",
                    Some(&bytes),
                    &Utc::now().to_rfc3339(),
                    limit,
                ) {
                    self.store
                        .set_state("last_error", "capture_cache_write_failed")?;
                    return Err(error);
                }
                self.store.set_state("capture_status", "capturing")?;
                Ok(())
            }
            Err(_) => {
                self.store
                    .set_state("capture_status", "capture_unavailable")?;
                self.store.set_state("last_error", "capture_unavailable")?;
                Ok(())
            }
        }
    }

    pub fn tick(&mut self) -> Result<()> {
        if self
            .last_config
            .is_none_or(|t| t.elapsed() >= Duration::from_secs(60))
        {
            self.refresh_config()?;
        }
        let interval = self.store.status()?.capture_interval_seconds.max(30);
        if self.last_capture.elapsed() >= Duration::from_secs(interval) {
            self.capture_now()?;
        }
        self.upload_one(Utc::now().timestamp())?;
        if self
            .last_heartbeat
            .is_none_or(|t| t.elapsed() >= Duration::from_secs(60))
        {
            self.last_heartbeat = Some(Instant::now());
            let status = self.store.status()?;
            match self.api.heartbeat(&status) {
                Ok(value) => self.apply_remote(value)?,
                Err(error) => self.record_error(&error)?,
            };
        }
        Ok(())
    }
}
