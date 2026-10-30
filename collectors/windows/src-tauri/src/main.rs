#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]
#[cfg(windows)]
mod app;

#[cfg(windows)]
fn main() {
    app::run();
}

#[cfg(not(windows))]
fn main() {
    eprintln!("Windows Tauri客户端需要Windows/MSVC/WebView2；Linux使用独立daemon/TUI。");
}
