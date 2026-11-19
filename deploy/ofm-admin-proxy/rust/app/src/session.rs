use std::path::PathBuf;
use std::sync::Arc;
use std::time::{Duration, SystemTime};

use http_body_util::Full;
use hyper::body::Bytes;
use hyper::header::{self, HeaderValue};
use hyper::{Request, StatusCode};
use serde_json::{Value, json};
use tokio::sync::{Mutex, watch};

use crate::{AUTHORITY, COOKIE_NAME, http, log};

const TTL: Duration = Duration::from_secs(7 * 60 * 60);
const TIMEOUT: Duration = Duration::from_secs(15);
type SessionResult = Result<HeaderValue, Arc<str>>;

pub struct Sessions {
    settings: PathBuf,
    state: Arc<Mutex<State>>,
}

#[derive(Default)]
struct State {
    cached: Option<Session>,
    pending: Option<watch::Receiver<Option<SessionResult>>>,
}

struct Session {
    key: String,
    cookie: HeaderValue,
    until: SystemTime,
}

impl Sessions {
    pub fn new(settings: PathBuf) -> Self {
        Self {
            settings,
            state: Arc::default(),
        }
    }

    fn read_key(&self) -> Result<(String, bool), Arc<str>> {
        let data = std::fs::read(&self.settings).map_err(|error| error.to_string())?;
        let input = String::from_utf8_lossy(&data);
        let settings: Value = serde_json::from_str(&input).map_err(|error| error.to_string())?;
        let management = settings["managementKey"]
            .as_str()
            .filter(|key| !key.is_empty());
        let key = management
            .or_else(|| {
                settings["forwardKey"]
                    .as_str()
                    .filter(|key| !key.is_empty())
            })
            .ok_or("settings.json has no managementKey/forwardKey")?;
        Ok((key.to_owned(), management.is_some()))
    }

    pub async fn ensure(&self, force: bool) -> SessionResult {
        let (key, privileged) = self.read_key()?;
        let mut state = self.state.lock().await;
        if !force
            && let Some(cached) = &state.cached
            && cached.key == key
            && SystemTime::now() < cached.until
        {
            return Ok(cached.cookie.clone());
        }
        let mut pending = match &state.pending {
            Some(existing) => existing.clone(),
            None => {
                let (sender, receiver) = watch::channel(None);
                state.pending = Some(receiver.clone());
                // Keep the shared login alive if the client that started it disconnects.
                let state = Arc::clone(&self.state);
                tokio::spawn(async move {
                    let result = create(&key).await;
                    let mut state = state.lock().await;
                    if let Ok(cookie) = &result {
                        state.cached = Some(Session {
                            key,
                            cookie: cookie.clone(),
                            until: SystemTime::now() + TTL,
                        });
                        log(format_args!(
                            "{}",
                            if privileged {
                                "OFM 管理会话已刷新（特权）"
                            } else {
                                "OFM 管理会话已刷新（仅 forwardKey，部分页面会 403）"
                            }
                        ));
                    }
                    state.pending = None;
                    sender.send_replace(Some(result));
                });
                receiver
            }
        };
        drop(state);
        let result = pending
            .wait_for(Option::is_some)
            .await
            .map_err(|_| "OFM session task was dropped")?
            .clone();
        result.ok_or_else(|| Arc::from("OFM session task returned no result"))?
    }

    pub async fn invalidate(&self) {
        self.state.lock().await.cached = None;
    }
}

async fn create(key: &str) -> SessionResult {
    let data = Bytes::from(json!({"key": key}).to_string());
    let request = Request::post("/api/management/session")
        .header(header::HOST, AUTHORITY)
        .header(header::CONTENT_TYPE, "application/json")
        .header(header::CONTENT_LENGTH, data.len())
        .header(header::CONNECTION, "close")
        .body(Full::new(data))
        .map_err(|error| error.to_string())?;
    let response = http::send(request, TIMEOUT)
        .await
        .map_err(|error| error.message("OFM session request timed out"))?;
    let (parts, body) = response.into_parts();
    if http::drain(body).await.is_err() {
        // The reference only settles its login after the response's end event.
        std::future::pending::<()>().await;
    }
    if parts.status != StatusCode::OK {
        return Err(format!("OFM session request failed with {}", parts.status.as_u16()).into());
    }
    let prefix = concat!("ofm_management_18900", "=").as_bytes();
    let Some(value) = parts
        .headers
        .get(header::SET_COOKIE)
        .and_then(|header| header.as_bytes().strip_prefix(prefix))
        .and_then(|value| value.split(|&byte| byte == b';').next())
        .filter(|value| !value.is_empty())
    else {
        return Err("OFM session response has no management cookie".into());
    };
    let cookie = [COOKIE_NAME.as_bytes(), b"=", value].concat();
    HeaderValue::from_bytes(&cookie).map_err(|error| error.to_string().into())
}
