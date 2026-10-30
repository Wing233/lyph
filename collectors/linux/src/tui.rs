use crate::daemon;
use anyhow::Result;
use crossterm::event::{self, Event, KeyCode, KeyEventKind};
use ratatui::{
    layout::{Constraint, Layout},
    style::{Color, Style},
    widgets::{Block, Paragraph, Wrap},
};
use serde_json::{Value, json};
use std::{
    path::Path,
    time::{Duration, Instant},
};

pub fn run(root: &Path) -> Result<()> {
    let mut terminal = ratatui::init();
    let result = draw_loop(&mut terminal, root);
    ratatui::restore();
    result
}

fn draw_loop(terminal: &mut ratatui::DefaultTerminal, root: &Path) -> Result<()> {
    let mut status = Value::Null;
    let mut updated = Instant::now() - Duration::from_secs(2);
    let mut notice = "关闭TUI不会停止daemon。截图只在拥有桌面权限时工作。".to_string();
    let mut note: Option<String> = None;
    loop {
        if updated.elapsed() >= Duration::from_secs(1) {
            match daemon::request(root, json!({"command":"status"})) {
                Ok(value) => status = value,
                Err(error) => notice = error.to_string(),
            };
            updated = Instant::now();
        }
        terminal.draw(|frame|{
            let panes=Layout::vertical([Constraint::Length(4),Constraint::Min(10),Constraint::Length(5),Constraint::Length(5)]).split(frame.area());
            let paper=Style::default().fg(Color::Rgb(232,220,198));
            frame.render_widget(Paragraph::new("lyph / 采集服务\n技术与生活 · 独立daemon · 本地持久队列")
                .style(paper).block(Block::bordered().title("Linux TUI")),panes[0]);
            let capture=match status["capture_status"].as_str().unwrap_or("unknown") {
                "capture_unavailable"=>"不可采集（无桌面或权限；可速记/选图）", "cache_full"=>"缓存已满（暂停新增采集）",
                "capturing"=>"采集中", "device_revoked"=>"设备已吊销",_=>"等待下一次采集",
            };
            let info=format!("状态：{}\n本地暂停：{} / 远程暂停：{}\n待上传：{} / 已确认：{}\n缓存：{} bytes / 上限：{} bytes\n采集间隔：{} 秒\n最近采集：{}\n最近错误：{}",
                capture,status["local_paused"],status["remote_paused"],status["pending_count"],status["confirmed_count"],
                status["cache_bytes"],status["cache_limit_bytes"],status["capture_interval_seconds"],
                status["last_capture_at"].as_str().unwrap_or("尚无"),status["last_error"].as_str().unwrap_or("无"));
            frame.render_widget(Paragraph::new(info).style(paper).wrap(Wrap{trim:false}).block(Block::bordered().title("实时队列")),panes[1]);
            frame.render_widget(Paragraph::new("s 暂停/恢复截图 · g 续传 · c 手动主屏截图 · n 速记\nq 关闭界面（服务继续）；显式 lyph-collector stop 停止服务\n选图：lyph-collector photo <路径>")
                .wrap(Wrap{trim:false}).block(Block::bordered().title("操作")),panes[2]);
            let text=note.as_ref().map(|value|format!("{}\nEnter保存 · Esc取消",value)).unwrap_or_else(||notice.clone());
            frame.render_widget(Paragraph::new(text).wrap(Wrap{trim:false}).block(Block::bordered().title(if note.is_some(){"写速记"}else{"消息"})),panes[3]);
        })?;
        if event::poll(Duration::from_millis(100))? {
            if let Event::Key(key) = event::read()? {
                if key.kind != KeyEventKind::Press {
                    continue;
                }
                if let Some(text) = note.as_mut() {
                    match key.code {
                        KeyCode::Esc => note = None,
                        KeyCode::Enter => {
                            let result =
                                daemon::request(root, json!({"command":"note","text":text}));
                            notice = match result {
                                Ok(_) => "速记已持久缓存，等待服务端确认。".into(),
                                Err(error) => error.to_string(),
                            };
                            if !notice.contains("失败") {
                                note = None;
                            }
                        }
                        KeyCode::Backspace => {
                            text.pop();
                        }
                        KeyCode::Char(value) if text.len() < 15996 => text.push(value),
                        _ => {}
                    }
                } else {
                    let command = match key.code {
                        KeyCode::Char('q') => break,
                        KeyCode::Char('n') => {
                            note = Some(String::new());
                            continue;
                        }
                        KeyCode::Char('s') => Some(if status["local_paused"] == true {
                            "resume"
                        } else {
                            "pause"
                        }),
                        KeyCode::Char('g') => Some("sync"),
                        KeyCode::Char('c') => Some("capture"),
                        _ => None,
                    };
                    if let Some(command) = command {
                        notice = match daemon::request(root, json!({"command":command})) {
                            Ok(_) => "命令已交给采集服务。".into(),
                            Err(error) => error.to_string(),
                        };
                    }
                }
            }
        }
    }
    Ok(())
}
