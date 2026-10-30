use anyhow::{Context, Result, bail};
use serde::{Deserialize, Serialize};
use std::{
    fs,
    io::Write,
    path::{Path, PathBuf},
};

#[derive(Clone, Serialize, Deserialize)]
pub struct Config {
    pub base_url: String,
    pub device_token: String,
    #[serde(default = "default_interval")]
    pub capture_interval_seconds: u64,
    #[serde(default = "default_limit")]
    pub cache_limit_bytes: u64,
}

fn default_interval() -> u64 {
    300
}
fn default_limit() -> u64 {
    1024 * 1024 * 1024
}

impl std::fmt::Debug for Config {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Config")
            .field("base_url", &self.base_url)
            .field("device_token", &"[redacted]")
            .finish()
    }
}

impl Config {
    pub fn new(base_url: String, device_token: String) -> Result<Self> {
        let config = Self {
            base_url: base_url.trim_end_matches('/').into(),
            device_token,
            capture_interval_seconds: default_interval(),
            cache_limit_bytes: default_limit(),
        };
        config.validate()?;
        Ok(config)
    }

    pub fn validate(&self) -> Result<()> {
        let url = reqwest::Url::parse(&self.base_url).context("站点地址无效")?;
        let local = matches!(
            url.host_str(),
            Some("localhost" | "127.0.0.1" | "[::1]" | "::1")
        );
        if (url.scheme() != "https" && !(url.scheme() == "http" && local))
            || url.username() != ""
            || url.password().is_some()
            || url.query().is_some()
            || url.fragment().is_some()
        {
            bail!("使用HTTPS站点地址；仅localhost/127.0.0.1允许HTTP，无内嵌凭证或查询参数");
        }
        if self.device_token.len() < 16
            || self.device_token.len() > 4096
            || self.device_token.contains(['\r', '\n'])
        {
            bail!("设备凭证无效");
        }
        if self.capture_interval_seconds < 30
            || self.capture_interval_seconds > 86400
            || self.cache_limit_bytes > default_limit()
        {
            bail!("采集间隔或缓存上限无效");
        }
        Ok(())
    }

    pub fn load(root: &Path) -> Result<Self> {
        let value: Self = serde_json::from_slice(
            &fs::read(root.join("config.json")).context("尚未配对；先运行init")?,
        )?;
        value.validate()?;
        Ok(value)
    }

    pub fn save(&self, root: &Path) -> Result<()> {
        self.validate()?;
        private_directory(root)?;
        let temp = root.join(format!("config-{}.tmp", uuid::Uuid::new_v4()));
        let mut file = private_file(&temp, true)?;
        file.write_all(&serde_json::to_vec_pretty(self)?)?;
        file.sync_all()?;
        drop(file);
        let final_path = root.join("config.json");
        fs::rename(temp, final_path)?;
        Ok(())
    }
}

pub fn default_data_dir() -> PathBuf {
    if let Some(path) = std::env::var_os("LYPH_COLLECTOR_DIR") {
        return path.into();
    }
    #[cfg(windows)]
    {
        PathBuf::from(std::env::var_os("LOCALAPPDATA").unwrap_or_else(|| ".".into()))
            .join("lyph-collector")
    }
    #[cfg(not(windows))]
    {
        PathBuf::from(std::env::var_os("XDG_DATA_HOME").unwrap_or_else(|| {
            let home = std::env::var_os("HOME").unwrap_or_else(|| ".".into());
            PathBuf::from(home).join(".local/share").into_os_string()
        }))
        .join("lyph-collector")
    }
}

pub fn private_directory(path: &Path) -> Result<()> {
    fs::create_dir_all(path)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(path, fs::Permissions::from_mode(0o700))?;
    }
    Ok(())
}

pub fn private_file(path: &Path, create_new: bool) -> Result<fs::File> {
    let mut options = fs::OpenOptions::new();
    options.read(true).write(true);
    if create_new {
        options.create_new(true);
    } else {
        options.create(true).truncate(false);
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    Ok(options.open(path)?)
}
