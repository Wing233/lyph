#[cfg(unix)]
mod daemon;
#[cfg(unix)]
mod tui;

#[cfg(unix)]
fn main() -> anyhow::Result<()> {
    use lyph_collector_core::{Config, config::default_data_dir};
    let mut args = std::env::args().skip(1);
    let command = args.next().unwrap_or_else(|| "tui".into());
    let root = default_data_dir();
    match command.as_str() {
        "init" => {
            let base_url = args
                .next()
                .ok_or_else(|| anyhow::anyhow!("用法：lyph-collector init https://site.example"))?;
            let token = rpassword::prompt_password("设备凭证（隐藏输入）：")?;
            Config::new(base_url, token)?.save(&root)?;
            println!("已保存独立设备配置；运行daemon才会开始采集。");
        }
        "daemon" => daemon::run(&root)?,
        "tui" => tui::run(&root)?,
        "status" | "pause" | "resume" | "capture" | "sync" | "stop" => {
            println!(
                "{}",
                serde_json::to_string_pretty(&daemon::request(
                    &root,
                    serde_json::json!({"command":command})
                )?)?
            );
        }
        "note" => {
            let text = args.collect::<Vec<_>>().join(" ");
            println!(
                "{}",
                daemon::request(&root, serde_json::json!({"command":"note","text":text}))?
            );
        }
        "photo" => {
            let path = args
                .next()
                .ok_or_else(|| anyhow::anyhow!("用法：lyph-collector photo <图片路径>"))?;
            println!(
                "{}",
                daemon::request(&root, serde_json::json!({"command":"photo","path":path}))?
            );
        }
        _ => anyhow::bail!(
            "命令：init / daemon / tui / status / pause / resume / capture / sync / note / photo / stop"
        ),
    }
    Ok(())
}

#[cfg(not(unix))]
fn main() {
    eprintln!("Linux daemon/TUI需要Unix环境；Windows使用Tauri客户端。");
}
