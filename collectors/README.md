# lyph Collectors

Desktop/mobile capture clients for lyph.

| Component | Stack | Purpose |
|---|---|---|
| `core` | Rust | Shared queue, API client, screen capture model |
| `windows` | Rust + Tauri | Windows tray/window collector |
| `linux` | Rust + Ratatui | Linux daemon + TUI |
| PWA | Next.js | Mobile capture page |

## Pairing

1. In the admin console, create a device for the target platform.
2. Copy the device token shown once.
3. In the collector, run `lyph-collector init https://your-site.example` and paste the token.
4. Start the daemon/window.

The device token only grants device endpoints, not admin access. Production must use HTTPS; HTTP is allowed only for localhost development.

## Data directory

- Linux: `$XDG_DATA_HOME/lyph-collector` or `~/.local/share/lyph-collector`
- Windows: `%LOCALAPPDATA%/lyph-collector`
- Override: `LYPH_COLLECTOR_DIR`

The directory contains the queue, config, and screenshots. Do not put it in a synced/shared folder, and never commit it.

## Build

### Linux

Requires Rust 1.95, `pkg-config`, `libclang`, `libxcb`, `libxrandr`, `libdbus`, `libpipewire`, `libgbm`, `libegl`, and `libwayland`.

```bash
cargo build --locked -p lyph-collector-linux --release
install -D target/release/lyph-collector ~/.local/bin/lyph-collector
```

Optional user service:

```bash
install -D linux/lyph-collector.service ~/.config/systemd/user/lyph-collector.service
systemctl --user daemon-reload
systemctl --user enable --now lyph-collector
```

### Windows

Requires Rust 1.95, Visual Studio C++ Build Tools, Windows SDK, and WebView2.

```powershell
cargo build --locked -p lyph-collector-windows --release
```

## CLI

```text
lyph-collector init <origin>
lyph-collector daemon
lyph-collector tui
lyph-collector note "text"
lyph-collector photo <path>
lyph-collector status
lyph-collector pause
lyph-collector resume
lyph-collector sync
lyph-collector capture
lyph-collector stop
```

`q` exits the TUI only; the daemon keeps capturing. `s` pauses/resumes screenshots, `g` retries queued uploads, `c` captures the main screen, `n` writes a note.

## Tests

```bash
cargo test --locked
```

Some tests use virtual X11 (`Xvfb`) and do not prove physical desktop/Wayland compatibility.
