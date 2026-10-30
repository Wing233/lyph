use chrono::Utc;
use lyph_collector_core::{Config, Engine, Store, api::HttpApi, config::default_data_dir};
use serde_json::{Value, json};
use std::{
    path::PathBuf,
    sync::{
        Arc, Mutex,
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
struct Worker {
    handle: thread::JoinHandle<()>,
    sender: mpsc::Sender<Work>,
}
struct AppState {
    root: PathBuf,
    running: Arc<AtomicBool>,
    worker: Mutex<Option<Worker>>,
}

fn safe_error(error: impl std::fmt::Display) -> String {
    // Local errors only. DeviceApi deliberately never returns headers, URLs, bodies or bearer tokens.
    error.to_string()
}

#[tauri::command]
fn status(state: tauri::State<'_, AppState>) -> Result<Value, String> {
    let store = Store::open(&state.root).map_err(safe_error)?;
    Ok(
        json!({"paired":Config::load(&state.root).is_ok(),"running":state.running.load(Ordering::Relaxed),"status":store.status().map_err(safe_error)?}),
    )
}

#[tauri::command]
fn pair(
    base_url: String,
    device_token: String,
    state: tauri::State<'_, AppState>,
) -> Result<(), String> {
    if state.running.load(Ordering::Relaxed) {
        return Err("先停止采集再更换设备凭证".into());
    }
    Config::new(base_url, device_token)
        .map_err(safe_error)?
        .save(&state.root)
        .map_err(safe_error)?;
    let store = Store::open(&state.root).map_err(safe_error)?;
    store.set_state("revoked", "false").map_err(safe_error)?;
    Ok(())
}

#[tauri::command]
fn start(state: tauri::State<'_, AppState>) -> Result<(), String> {
    let mut guard = state.worker.lock().map_err(safe_error)?;
    if guard.is_some() {
        return Ok(());
    }
    let config = Config::load(&state.root).map_err(safe_error)?;
    let store = Store::open(&state.root).map_err(safe_error)?;
    let mut engine = Engine::new(
        store.clone(),
        config.clone(),
        HttpApi::new(config).map_err(safe_error)?,
    )
    .map_err(safe_error)?;
    let running = state.running.clone();
    running.store(true, Ordering::Relaxed);
    let (sender, receiver) = mpsc::channel();
    let handle = thread::spawn(move || {
        while running.load(Ordering::Relaxed) {
            match receiver.try_recv() {
                Ok(Work::Capture) => {
                    let _ = engine.capture_now();
                }
                Ok(Work::Sync) => {
                    let _ = store.make_due();
                    let _ = engine.refresh_config();
                }
                Err(_) => {}
            }
            if engine.tick().is_err() {
                let _ = store.set_state("last_error", "collector_task_failed");
            }
            thread::sleep(Duration::from_millis(500));
        }
    });
    *guard = Some(Worker { handle, sender });
    Ok(())
}

#[tauri::command]
async fn stop(state: tauri::State<'_, AppState>) -> Result<(), String> {
    state.running.store(false, Ordering::Relaxed);
    let worker = state.worker.lock().map_err(safe_error)?.take();
    if let Some(worker) = worker {
        tauri::async_runtime::spawn_blocking(move || worker.handle.join())
            .await
            .map_err(safe_error)?
            .map_err(|_| "采集线程异常停止".to_string())?;
    }
    Ok(())
}

#[tauri::command]
fn pause(paused: bool, state: tauri::State<'_, AppState>) -> Result<(), String> {
    Store::open(&state.root)
        .map_err(safe_error)?
        .set_state("local_paused", if paused { "true" } else { "false" })
        .map_err(safe_error)
}

#[tauri::command]
fn action(command: String, state: tauri::State<'_, AppState>) -> Result<(), String> {
    let guard = state.worker.lock().map_err(safe_error)?;
    let worker = guard
        .as_ref()
        .ok_or_else(|| "请先启动采集服务".to_string())?;
    worker
        .sender
        .send(match command.as_str() {
            "capture" => Work::Capture,
            "sync" => Work::Sync,
            _ => return Err("未知采集动作".into()),
        })
        .map_err(safe_error)
}

#[tauri::command]
fn note(text: String, state: tauri::State<'_, AppState>) -> Result<Value, String> {
    let config = Config::load(&state.root).map_err(safe_error)?;
    let store = Store::open(&state.root).map_err(safe_error)?;
    let status = store.status().map_err(safe_error)?;
    if status.revoked {
        return Err("设备已吊销".into());
    }
    let event = store
        .enqueue(
            "note",
            &text,
            None,
            &Utc::now().to_rfc3339(),
            status.cache_limit_bytes.min(config.cache_limit_bytes),
        )
        .map_err(safe_error)?;
    Ok(json!({"capture_id":event.id,"cached":true}))
}

pub fn run() {
    tauri::Builder::default()
        .manage(AppState {
            root: default_data_dir(),
            running: Arc::new(AtomicBool::new(false)),
            worker: Mutex::new(None),
        })
        .invoke_handler(tauri::generate_handler![
            status, pair, start, stop, pause, action, note
        ])
        .run(tauri::generate_context!())
        .expect("Tauri application failed");
}
