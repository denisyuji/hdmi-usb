# Agent Documentation

## Overview

This project provides automated HDMI capture device detection and streaming for cheap MacroSilicon USB HDMI capture devices.

The current codebase centers around a single unified Python RTSP server (`hdmi-usb.py`) plus helpers:
- A wrapper launcher (`hdmi-usb`) that can do device preflight/recovery and run the server in the background.
- An MCP-only helper (`hdmi-usb-screenshot-mcp`, Python 3 + GStreamer GI) that exposes the RTSP video frame over **MCP stdio** (`get_last_frame`).

## Key Components

### hdmi-usb.py
Unified HDMI USB RTSP server (and local preview).

- **Implementation**: Python 3 using GStreamer via GI (`gi.repository.Gst`, `GstRtspServer`, `GLib`)
- **Dependencies**: no PyPI deps, but requires system packages for GStreamer + GI bindings

**Core behavior:**
- **Device detection**: uses `v4l2-ctl` to find likely MacroSilicon devices (by name and capabilities) and validates device state (STREAMON test).
- **Instance management**: kills other `hdmi-usb.py` instances and orphaned `gst-launch-1.0 ... v4l2src` processes to avoid device conflicts.
- **RTSP server**:
  - Serves RTSP at `rtsp://0.0.0.0:1234/hdmi` (default).
  - Uses a **static `RTSPMediaFactory.set_launch()` pipeline** so multiple RTSP clients don’t trigger multiple `v4l2src` opens (prevents `Device is busy` / RTSP `503` issues).
  - **Video path (non-MJPEG)**: **`queue max-size-buffers=2 max-size-time=0 leaky=downstream ! decodebin !`** before encoding so the H.264 stream tracks **live** HDMI with minimal backlog (restart server after code updates).
- **Local preview**:
  - By default, the local preview is an **RTSP client** (`playbin`) connecting to the local server.
  - Window geometry is saved/restored and the window is kept at 16:9.
- **Audio**:
  - Attempts to match an ALSA capture card to the same USB device path as the video node.
  - Uses `arecord` to probe whether a capture device is available (prefers shareable `dsnoop`, falls back to `plughw`).
  - Can be forced via `AUDIO_FORCE_CARD=<n>` (best-effort).

**CLI flags (see `--help`):**
- `--headless`: disable local preview window
- `--width <px>`: force local viewer window width (16:9)
- `--debug`: enable app logs (`[INFO]`, `[LOCAL]`, etc.)
- `--gst-debug`: enable GStreamer logs (very verbose)
- `--reset-window`: clear saved window geometry (XDG: `~/.config/hdmi-usb/window-state`)

### hdmi-usb (wrapper)
Launcher script that:
- Performs a quick device preflight and attempts recovery on bad STREAMON state (USB reset / `uvcvideo` reload).
- Translates wrapper-only `-d` into `hdmi-usb.py --debug`.
- Runs `hdmi-usb.py` in the background. If neither `--debug` nor `--gst-debug` is set, it runs silently (`>/dev/null`).

### hdmi-usb-screenshot-mcp
**MCP-only** RTSP client (default URL `rtsp://127.0.0.1:1234/hdmi`, overridable via `RTSP_URL` / `--url`):

- **Stdio**: JSON-RPC 2.0. **NDJSON** (one JSON object per line) for Cursor / MCP 2025-03-26; **Content-Length** framing still supported for older clients. Replies match the client’s framing. **Initialize** echoes the client’s **`protocolVersion`** when provided.
- **Startup**: MCP read/write on the **main thread** immediately; **PyGI + `Gst.init()`** load only in the **capture thread** so Cursor does not time out during import.
- **Pipeline**: leaky bounded **queues** after `rtspsrc`, after `decodebin`, and before **`pngenc`**; **`rtspsrc`** **`drop-on-latency`** + small **`latency`** where supported. Continuous **640×360 PNG** into **`appsink`**.
- **`get_last_frame`**: uses **`try-pull-sample`** to **drain** pending buffers and waits up to **~500 ms** for the next frame, then returns MCP **`image`** content (base64). **Stderr** only for logs/errors.

**Video-only:** rejects the RTSP audio stream **before SETUP** via `rtspsrc`’s `select-stream` signal.

**Automated check:** `test_hdmi_usb_screenshot_mcp.py` spawns the binary (NDJSON), validates handshake + PNG from `get_last_frame` (RTSP must already be running).

### install.sh
- **System Installation**: Copies scripts to `~/.local/bin/` (`hdmi-usb.py`, `hdmi-usb`, `hdmi-usb-screenshot-mcp`)
- **PATH Management**: Automatically adds `~/.local/bin` to shell PATH
- **Shell Detection**: Supports bash, zsh, fish, and other shells
- **Cursor MCP**: Merges `~/.cursor/mcp.json` entry **`hdmi-screenshot`** (`command` → `~/.local/bin/hdmi-usb-screenshot-mcp`, env `RTSP_URL`, `PYTHONUNBUFFERED=1`); skips on invalid JSON with a warning

## Technical Details

- **Window state**: saved to `${XDG_CONFIG_HOME:-~/.config}/hdmi-usb/window-state` as `WIDTHxHEIGHT+X+Y`
  - Legacy path (migrated automatically): `~/.hdmi-rtsp-unified-window-state`
- **Window tooling**: uses `wmctrl`, `xwininfo`, and `xprop` (best-effort; missing tools shouldn’t crash the server)
- **RTSP multi-client robustness**: static server pipeline avoids per-client capture opens
- **Audio matching**: prefers ALSA card on same USB path as the video device
- **Shutdown/cleanup**: robust cleanup via `atexit` registry + GLib signal integration

## Dependencies

- `v4l2-ctl` - Video device enumeration
- `gstreamer1.0-*` and `gir1.2-gst-rtsp-server-1.0` - RTSP server and plugins
- `python3-gi` - GI bindings
- `arecord` (alsa-utils) - audio device probe
- `wmctrl`, `xwininfo`, `xprop` - optional window positioning/inspection
- `lsusb` - USB device listing

## Commit Message Guidelines

- Title format: `subject: description` (imperative, ≤50 chars).
- Blank line after title; keep body concise and wrapped ~50 chars.

## Commit Behavior Guidelines

- **Only commit when explicitly requested**: Do not automatically commit changes unless the user specifically asks for a commit.

## Usage Guidelines

- **Always use a timeout**: when testing/troubleshooting, prefer `timeout ...` so the server doesn’t run forever.
- **Prefer `--debug` (and `--gst-debug` when needed)**: start with app logs, enable GStreamer logs only when diagnosing pipeline issues.
- **Default mode is quiet**: without `--debug`/`--gst-debug`, the wrapper runs the server silently unless there are errors.
- **Window state management**: Use `--reset-window` to clear saved window position/size if needed.

## Integration Test Coverage

`integration-test.sh` covers the core end-to-end flows:
- install + PATH usage (`~/.local/bin`)
- RTSP server starts/listens
- local preview window save/restore (when X11 tooling exists)
- `test_hdmi_usb_screenshot_mcp.py` (MCP stdio against running RTSP)
- headless RTSP + same MCP test

`test_hdmi_usb_screenshot_mcp.py` is also the focused unit for MCP; it does not start `hdmi-usb.py` itself.

It does not attempt to force/fake hardware failure states, so it does not
exercise wrapper recovery paths, audio card matching, or instance-kill
behavior.
