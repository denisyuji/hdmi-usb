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
  - **Video path (MJPEG)**: prefers hardware **`vajpegdec ! vapostproc ! I420`** (VA-API) when available (`/dev/dri/renderD128`), falling back to software `jpegdec`. The caps filter must carry explicit `sof-marker`/`colorspace`/`sampling`/`interlace-mode` fields for `vajpegdec` to negotiate; the decoder parses the real bitstream, so mismatched hint values still decode correctly. `vapostproc` converts the decoder's native 4:2:2 output to I420 on the GPU — letting `videoconvert` do it on the CPU costs more than software `jpegdec`. Element presence does not prove the VA-API driver works, so a hardware pipeline that fails to reach PLAYING — or that posts an error within `HW_DECODE_PROBATION_SECONDS` of doing so — is torn down and retried once with software `jpegdec`. After that window, errors terminate the app as usual. Resource errors (device busy/missing) are excluded from the retry — software decode would fail identically — and are reported with the real bus error message instead.
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
- **Startup**: no background capture thread. The server enters the MCP loop immediately and only spawns `gst-launch-1.0` when a tool call asks for a frame.
- **Capture path**: each **`get_last_frame`** call runs a short **`gst-launch-1.0 uridecodebin`** burst (`source::protocols=tcp`, `source::latency=100`) through `videoconvert ! videoscale ! pngenc ! multifilesink`, then returns the newest **640×360 PNG** from that burst.
- **Failure mode**: if `gst-launch-1.0` fails or produces no PNGs yet, the tool returns an MCP error payload saying no frame is available. **Stderr** only for logs/errors.

**Video-only:** the capture burst follows the decoded video branch only; it does not expose audio through MCP.

**Automated check:** `test_hdmi_usb_screenshot_mcp.py` spawns the binary (NDJSON), validates handshake + PNG from `get_last_frame` (RTSP must already be running).

### install.sh
- **System Installation**: Copies scripts to `~/.local/bin/` (`hdmi-usb.py`, `hdmi-usb`, `hdmi-usb-screenshot-mcp`)
- **PATH Management**: Automatically adds `~/.local/bin` to shell PATH
- **Shell Detection**: Supports bash, zsh, fish, and other shells
- **Cursor MCP**: Merges `~/.cursor/mcp.json` entry **`hdmi-screenshot`** (`command` → `~/.local/bin/hdmi-usb-screenshot-mcp`, env `RTSP_URL`, `PYTHONUNBUFFERED=1`); skips on invalid JSON with a warning

## Technical Details

- **Window state**: saved to `${XDG_CONFIG_HOME:-~/.config}/hdmi-usb/window-state` as `WIDTHxHEIGHT+X+Y`
  - Legacy path (migrated automatically): `~/.hdmi-rtsp-unified-window-state`
  - With `--width` the geometry is neither restored nor saved, but 16:9 is still enforced on manual resizes
  - 16:9 correction adjusts the side the user did not drag (compared against the last 16:9 geometry); if both sides changed, the smaller correction wins
  - Offsets are normalised on read: xwininfo reports negative positions as `+-50` / `--28`, which used to be saved unparseable and silently dropped the whole restore
- **Window tooling**: uses `wmctrl`, `xwininfo`, and `xprop` (best-effort; missing tools shouldn’t crash the server)
  - The sink window is identified by scoring PID (`wmctrl -lp`), WM_CLASS (`wmctrl -lx`) and title; a window is only accepted above a confidence threshold, otherwise polling continues until timeout
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
- software fallback: server started against a plugin directory that symlinks
  every system plugin except `libgstva.so`/`libgstvaapi.so`/`libgstnvcodec.so`
  (path from `pkg-config --variable=pluginsdir gstreamer-1.0`), asserting it
  streams, serves a frame over MCP, and logs no `Using hardware` line

`test_hdmi_usb_screenshot_mcp.py` is also the focused unit for MCP; it does not start `hdmi-usb.py` itself.

Beyond hiding the hardware plugins, it does not force/fake hardware failure
states, so it does not exercise the runtime hardware-decode downgrade, wrapper
recovery paths, audio card matching, or instance-kill behavior.
