use crate::{Config, Event, Status};
use reqwest::blocking::{Client, multipart};
use serde::{Deserialize, Serialize};
use std::{fs, io::Read, time::Duration};

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SyncError {
    Network,
    Revoked,
    Rejected(u16),
    InvalidResponse,
    LocalFile,
}

impl std::fmt::Display for SyncError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Network => write!(f, "network_unavailable"),
            Self::Revoked => write!(f, "device_revoked"),
            Self::Rejected(code) => write!(f, "server_rejected_{code}"),
            Self::InvalidResponse => write!(f, "invalid_server_confirmation"),
            Self::LocalFile => write!(f, "local_file_unavailable"),
        }
    }
}
impl std::error::Error for SyncError {}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct RemoteConfig {
    pub device_id: String,
    pub paused: bool,
    pub capture_interval_seconds: u64,
    pub cache_limit_bytes: u64,
    pub upload_concurrency: u64,
    #[serde(default = "max_upload")]
    pub max_upload_bytes: u64,
}
fn max_upload() -> u64 {
    20 * 1024 * 1024
}

#[derive(Debug, Serialize, Deserialize)]
pub struct CaptureAck {
    pub id: String,
    #[serde(default)]
    pub duplicate: bool,
}

pub trait DeviceApi: Send + Sync {
    fn config(&self) -> Result<RemoteConfig, SyncError>;
    fn upload(&self, event: &Event) -> Result<CaptureAck, SyncError>;
    fn heartbeat(&self, status: &Status) -> Result<RemoteConfig, SyncError>;
}

pub struct HttpApi {
    client: Client,
    config: Config,
}

impl HttpApi {
    pub fn new(config: Config) -> anyhow::Result<Self> {
        config.validate()?;
        let client = Client::builder()
            .connect_timeout(Duration::from_secs(10))
            .timeout(Duration::from_secs(45))
            .redirect(reqwest::redirect::Policy::none())
            .build()?;
        Ok(Self { client, config })
    }
    fn url(&self, path: &str) -> String {
        format!("{}/api/v1/device/{}", self.config.base_url, path)
    }
}

fn parsed<T: serde::de::DeserializeOwned>(
    response: reqwest::blocking::Response,
) -> Result<T, SyncError> {
    let status = response.status();
    if matches!(status.as_u16(), 401 | 403) {
        return Err(SyncError::Revoked);
    }
    if status.is_server_error() || status.as_u16() == 429 {
        return Err(SyncError::Network);
    }
    if !status.is_success() {
        return Err(SyncError::Rejected(status.as_u16()));
    }
    // Never log server bodies/headers; they can include provider secrets or submitted text.
    serde_json::from_reader(response.take(65536)).map_err(|_| SyncError::InvalidResponse)
}

impl DeviceApi for HttpApi {
    fn config(&self) -> Result<RemoteConfig, SyncError> {
        let response = self
            .client
            .get(self.url("config"))
            .bearer_auth(&self.config.device_token)
            .send()
            .map_err(|_| SyncError::Network)?;
        parsed(response)
    }
    fn upload(&self, event: &Event) -> Result<CaptureAck, SyncError> {
        let mut form = multipart::Form::new()
            .text("capture_id", event.id.clone())
            .text("captured_at", event.captured_at.clone())
            .text("source_kind", event.source_kind.clone())
            .text("text", event.text.clone());
        if let Some(path) = &event.file_path {
            let bytes = fs::read(path).map_err(|_| SyncError::LocalFile)?;
            if event.sha256.as_deref() != Some(crate::store::sha256(&bytes).as_str()) {
                return Err(SyncError::LocalFile);
            }
            let (mime, extension) = image_type(&bytes)?;
            let part = multipart::Part::bytes(bytes)
                .file_name(format!("{}.{}", event.id, extension))
                .mime_str(mime)
                .map_err(|_| SyncError::LocalFile)?;
            form = form.part("file", part);
        }
        let response = self
            .client
            .post(self.url("captures"))
            .bearer_auth(&self.config.device_token)
            .multipart(form)
            .send()
            .map_err(|_| SyncError::Network)?;
        parsed(response)
    }
    fn heartbeat(&self, status: &Status) -> Result<RemoteConfig, SyncError> {
        let response = self.client.post(self.url("heartbeat")).bearer_auth(&self.config.device_token)
            .json(&serde_json::json!({"pending_count":status.pending_count,"last_capture_at":status.last_capture_at,"last_error":status.last_error}))
            .send().map_err(|_|SyncError::Network)?;
        parsed(response)
    }
}

pub fn image_type(bytes: &[u8]) -> Result<(&'static str, &'static str), SyncError> {
    if bytes.starts_with(b"\x89PNG\r\n\x1a\n") {
        Ok(("image/png", "png"))
    } else if bytes.starts_with(b"\xff\xd8\xff") {
        Ok(("image/jpeg", "jpg"))
    } else if bytes.starts_with(b"RIFF") && bytes.get(8..12) == Some(b"WEBP") {
        Ok(("image/webp", "webp"))
    } else {
        Err(SyncError::LocalFile)
    }
}
