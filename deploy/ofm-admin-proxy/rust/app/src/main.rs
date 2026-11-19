mod http;
mod session;

use std::collections::HashSet;
use std::env;
use std::io;
use std::net::{Ipv4Addr, SocketAddrV4};
use std::path::PathBuf;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

use chrono::{SecondsFormat, Utc};
use tokio::net::TcpListener;

use crate::session::Sessions;

const AUTHORITY: &str = "127.0.0.1:18900";
const UPSTREAM: SocketAddrV4 = SocketAddrV4::new(Ipv4Addr::LOCALHOST, 18900);
const COOKIE_NAME: &str = "ofm_management_18900";

struct Proxy {
    peers: HashSet<String>,
    sessions: Sessions,
    exit_scheduled: AtomicBool,
}

impl Proxy {
    fn schedule_exit(&self, reason: &str) {
        if !self.exit_scheduled.swap(true, Ordering::Relaxed) {
            log(format_args!(
                "OFM 上游不可达，退出以便重新加入容器网络命名空间: {reason}"
            ));
            tokio::spawn(async {
                tokio::time::sleep(Duration::from_millis(1500)).await;
                std::process::exit(1);
            });
        }
    }
}

fn log(message: std::fmt::Arguments<'_>) {
    println!(
        "{} {message}",
        Utc::now().to_rfc3339_opts(SecondsFormat::Millis, true)
    );
}

fn node_trim(value: &str) -> &str {
    value.trim_matches(|ch| {
        matches!(ch, '\u{0009}'..='\u{000d}' | ' ' | '\u{00a0}' | '\u{1680}' |
            '\u{2000}'..='\u{200a}' | '\u{2028}' | '\u{2029}' | '\u{202f}' |
            '\u{205f}' | '\u{3000}' | '\u{feff}')
    })
}

fn parse_port(value: &str) -> Option<u16> {
    let value = node_trim(value);
    if value.is_empty() {
        return Some(0);
    }
    if let Some(prefix) = value.get(..2) {
        let radix = match prefix.to_ascii_lowercase().as_str() {
            "0x" => 16,
            "0o" => 8,
            "0b" => 2,
            _ => 0,
        };
        if radix != 0 {
            let digits = &value[2..];
            return (!digits.starts_with('+'))
                .then(|| u16::from_str_radix(digits, radix).ok())
                .flatten();
        }
    }
    let number = value.parse::<f64>().ok()?;
    (number.fract() == 0.0 && (0.0..=65535.0).contains(&number)).then_some(number as u16)
}

fn env_or(name: &str, default: &str) -> String {
    env::var(name)
        .ok()
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| default.into())
}

#[cfg(unix)]
fn install_shutdown_handlers() -> io::Result<()> {
    use tokio::signal::unix::{SignalKind, signal};

    // A container's PID 1 needs to subscribe explicitly to these signals.
    let mut terminate = signal(SignalKind::terminate())?;
    let mut interrupt = signal(SignalKind::interrupt())?;
    tokio::spawn(async move {
        let status = tokio::select! {
            _ = terminate.recv() => 143,
            _ = interrupt.recv() => 130,
        };
        std::process::exit(status);
    });
    Ok(())
}

#[tokio::main(flavor = "current_thread")]
async fn main() -> io::Result<()> {
    #[cfg(unix)]
    install_shutdown_handlers()?;

    let port = parse_port(&env_or("OFM_PROXY_PORT", "18905"))
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "Invalid OFM_PROXY_PORT"))?;
    let path = env::var_os("OFM_SETTINGS_PATH")
        .filter(|value| !value.is_empty())
        .map_or_else(|| PathBuf::from("/data/settings.json"), PathBuf::from);
    let proxy = Arc::new(Proxy {
        peers: env_or("OFM_PROXY_ALLOW", "172.30.91.254,127.0.0.1")
            .split(',')
            .map(node_trim)
            .filter(|part| !part.is_empty())
            .map(str::to_owned)
            .collect(),
        sessions: Sessions::new(path),
        exit_scheduled: AtomicBool::new(false),
    });
    let listener = TcpListener::bind((Ipv4Addr::UNSPECIFIED, port)).await?;
    log(format_args!("OFM 管理侧车已监听 :{port}"));

    loop {
        let (socket, addr) = listener.accept().await?;
        let _ = socket.set_nodelay(true);
        let proxy = Arc::clone(&proxy);
        let allowed = proxy.peers.contains(&addr.ip().to_string());
        tokio::spawn(async move {
            http::serve(&proxy, allowed, socket).await;
        });
    }
}

#[cfg(test)]
mod tests {
    use super::parse_port;

    #[test]
    fn port_uses_javascript_numeric_notation() {
        for value in [
            "18905",
            "+18905",
            "1.8905e4",
            "0x49d9",
            "0o44731",
            "\u{feff}18905 ",
        ] {
            assert_eq!(parse_port(value), Some(18905), "{value}");
        }
        assert_eq!(parse_port(" \t "), Some(0));
        for value in ["18905.5", "-1", "65536", "NaN", "0x+1"] {
            assert_eq!(parse_port(value), None, "{value}");
        }
    }
}
