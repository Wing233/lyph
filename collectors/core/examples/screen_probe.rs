//! Explicit desktop verification tool. It never uploads or prints image content.
#[cfg(feature = "screen-capture")]
fn main() -> anyhow::Result<()> {
    let monitors = xcap::Monitor::all()?;
    for monitor in monitors {
        println!(
            "monitor primary={:?} dimensions={:?}x{:?}",
            monitor.is_primary(),
            monitor.width(),
            monitor.height()
        );
    }
    let bytes = lyph_collector_core::screen::capture_png()?;
    let decoded = image::load_from_memory(&bytes)?;
    println!(
        "valid_png bytes={} width={} height={}",
        bytes.len(),
        decoded.width(),
        decoded.height()
    );
    Ok(())
}

#[cfg(not(feature = "screen-capture"))]
fn main() {
    eprintln!("Enable --features screen-capture to run this desktop verification tool");
    std::process::exit(1);
}
