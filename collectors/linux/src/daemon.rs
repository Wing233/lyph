use anyhow::{Context, Result};
use chrono::Utc;
use lyph_collector_core::{
    Config, Engine, Store,
    api::{HttpApi, image_type},
};
use serde_json::{Value, json};
use std::{
    fs,
    io::{BufRead, BufReader, Read, Write},
    os::unix::{
        fs::PermissionsExt,
        net::{UnixListener, UnixStream},
    },
    path::Path,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
        mpsc,
    },
    thread,
    time::Duration,
};

enum Work {
    Capture,
    Sync,
}

pub fn request(root: &Path, message: Value) -> Result<Value> {
    let mut socket =
        UnixStream::connect(root.join("daemon.sock")).context("采集服务未运行；先运行daemon")?;
    socket.set_read_timeout(Some(Duration::from_secs(5)))?;
    socket.set_write_timeout(Some(Duration::from_secs(5)))?;
    writeln!(socket, "{}", message)?;
    let mut line = String::new();
    BufReader::new(socket.take(65536)).read_line(&mut line)?;
    let response: Value = serde_json::from_str(&line)?;
    if let Some(error) = response.get("error") {
        anyhow::bail!("{}", error.as_str().unwrap_or("服务操作失败"));
    }
    Ok(response)
}

pub fn run(root: &Path) -> Result<()> {
    let config = Config::load(root)?;
    let store = Store::open(root)?;
    let engine = Engine::new(store.clone(), config.clone(), HttpApi::new(config.clone())?)?;
    let socket_path = root.join("daemon.sock");
    if socket_path.exists() {
        if UnixStream::connect(&socket_path).is_ok() {
            anyhow::bail!("采集daemon已运行");
        }
        fs::remove_file(&socket_path)?;
    }
    let listener = UnixListener::bind(&socket_path)?;
    fs::set_permissions(&socket_path, fs::Permissions::from_mode(0o600))?;
    listener.set_nonblocking(true)?;
    let running = Arc::new(AtomicBool::new(true));
    let alive = running.clone();
    let queue_store = store.clone();
    let (sender, receiver) = mpsc::channel::<Work>();
    let worker = thread::spawn(move || {
        let mut engine = engine;
        while alive.load(Ordering::Relaxed) {
            match receiver.try_recv() {
                Ok(Work::Capture) => {
                    if engine.capture_now().is_err() {
                        let _ = queue_store.set_state("last_error", "capture_failed");
                    }
                }
                Ok(Work::Sync) => {
                    let _ = queue_store.make_due();
                    let _ = engine.refresh_config();
                }
                Err(_) => {}
            }
            if engine.tick().is_err() {
                let _ = queue_store.set_state("last_error", "collector_task_failed");
            }
            thread::sleep(Duration::from_millis(500));
        }
    });
    println!(
        "lyph采集服务已启动；Ctrl+C或显式stop停止服务，关闭TUI不会停止。未获得桌面权限时仅速记/选图可用。"
    );
    while running.load(Ordering::Relaxed) {
        match listener.accept() {
            Ok((mut stream, _)) => {
                stream.set_read_timeout(Some(Duration::from_secs(2)))?;
                stream.set_write_timeout(Some(Duration::from_secs(2)))?;
                let result = handle(&mut stream, &store, &config, &sender, &running);
                let response = match result {
                    Ok(value) => value,
                    Err(_) => json!({"error":"操作失败；检查采集状态、文本/文件和缓存空间"}),
                };
                let _ = writeln!(stream, "{}", response);
            }
            Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                thread::sleep(Duration::from_millis(50))
            }
            Err(error) => return Err(error.into()),
        }
    }
    drop(sender);
    let _ = worker.join();
    fs::remove_file(socket_path)?;
    Ok(())
}

fn handle(
    stream: &mut UnixStream,
    store: &Store,
    config: &Config,
    sender: &mpsc::Sender<Work>,
    running: &AtomicBool,
) -> Result<Value> {
    let mut line = String::new();
    BufReader::new((&mut *stream).take(65536)).read_line(&mut line)?;
    let message: Value = serde_json::from_str(&line)?;
    match message["command"].as_str().unwrap_or("") {
        "status" => Ok(serde_json::to_value(store.status()?)?),
        "pause" => {
            store.set_state("local_paused", "true")?;
            Ok(json!({"paused":true}))
        }
        "resume" => {
            store.set_state("local_paused", "false")?;
            Ok(json!({"paused":false}))
        }
        "capture" => {
            sender.send(Work::Capture)?;
            Ok(json!({"queued":true}))
        }
        "sync" => {
            sender.send(Work::Sync)?;
            Ok(json!({"queued":true}))
        }
        "note" => {
            let status = store.status()?;
            if status.revoked {
                anyhow::bail!("设备已吊销");
            }
            let event = store.enqueue(
                "note",
                message["text"].as_str().unwrap_or(""),
                None,
                &Utc::now().to_rfc3339(),
                status.cache_limit_bytes.min(config.cache_limit_bytes),
            )?;
            Ok(json!({"capture_id":event.id,"cached":true}))
        }
        "photo" => {
            let status = store.status()?;
            if status.revoked {
                anyhow::bail!("设备已吊销");
            }
            let path = message["path"].as_str().context("图片路径为空")?;
            if fs::metadata(path)?.len() > 20 * 1024 * 1024 {
                anyhow::bail!("图片超过20MiB");
            }
            let bytes = fs::read(path)?;
            image_type(&bytes)?;
            let event = store.enqueue(
                "photo",
                "",
                Some(&bytes),
                &Utc::now().to_rfc3339(),
                status.cache_limit_bytes.min(config.cache_limit_bytes),
            )?;
            Ok(json!({"capture_id":event.id,"cached":true}))
        }
        "stop" => {
            running.store(false, Ordering::Relaxed);
            Ok(json!({"stopping":true}))
        }
        _ => anyhow::bail!("未知命令"),
    }
}
