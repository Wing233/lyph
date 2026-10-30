use anyhow::Result;

pub fn desktop_available() -> bool {
    #[cfg(all(feature = "screen-capture", target_os = "linux"))]
    {
        return std::env::var_os("DISPLAY").is_some()
            || std::env::var_os("WAYLAND_DISPLAY").is_some();
    }
    #[cfg(all(feature = "screen-capture", not(target_os = "linux")))]
    {
        true
    }
    #[cfg(not(feature = "screen-capture"))]
    {
        false
    }
}

#[cfg(feature = "screen-capture")]
pub fn capture_png() -> Result<Vec<u8>> {
    #[cfg(target_os = "linux")]
    if std::env::var_os("DISPLAY").is_none() && std::env::var_os("WAYLAND_DISPLAY").is_none() {
        anyhow::bail!("capture_unavailable：没有桌面会话");
    }
    let mut monitors = xcap::Monitor::all()?;
    let primary = monitors
        .iter()
        .position(|monitor| monitor.is_primary().unwrap_or(false));
    let monitor = match primary {
        Some(index) => monitors.swap_remove(index),
        // Some single-screen X11 sessions omit the RandR primary flag. The sole
        // screen is unambiguous; never guess when multiple monitors are present.
        None if monitors.len() == 1 => monitors.pop().expect("one monitor"),
        None => anyhow::bail!("capture_unavailable：没有主屏或未获权限"),
    };
    let image = monitor.capture_image()?;
    let mut output = std::io::Cursor::new(Vec::new());
    image::DynamicImage::ImageRgba8(image).write_to(&mut output, image::ImageFormat::Png)?;
    Ok(output.into_inner())
}

#[cfg(not(feature = "screen-capture"))]
pub fn capture_png() -> Result<Vec<u8>> {
    anyhow::bail!("capture_unavailable：构建未包含screen-capture能力")
}
