# Agent Documentation

## Overview

This project provides automated HDMI capture device detection and streaming for cheap MacroSilicon USB HDMI capture devices.

The current codebase centers around a single unified Python RTSP server (`hdmi-usb.py`) plus small shell helpers:
- A wrapper launcher (`hdmi-usb`) that can do device preflight/recovery and run the server in the background.
- A snapshot tool (`hdmi-usb-screenshot`) that captures a PNG (and base64) from the RTSP stream.

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
- `--reset-window`: clear saved window geometry (`~/.hdmi-rtsp-unified-window-state`)

### hdmi-usb (wrapper)
Launcher script that:
- Performs a quick device preflight and attempts recovery on bad STREAMON state (USB reset / `uvcvideo` reload).
- Translates wrapper-only `-d` into `hdmi-usb.py --debug`.
- Runs `hdmi-usb.py` in the background. If neither `--debug` nor `--gst-debug` is set, it runs silently (`>/dev/null`).

### hdmi-usb-screenshot
Snapshot tool for RTSP:
- Captures a frame from an RTSP stream to `screenshot_YYYYMMDD_HHMMSS.png`
- Writes a matching `.base64` file
- `--lowres` saves a 640x360 PNG and prints `BASE64=...`
- Prints:
  - `OK`
  - `FILENAME=...`
  - `BASE64_FILE=...`

**Important behavior:** it is **video-only** and explicitly rejects the RTSP audio stream **before SETUP** using `rtspsrc`’s `select-stream` signal.

### install.sh
- **System Installation**: Copies scripts to `~/.local/bin/` (`hdmi-usb.py`, `hdmi-usb`)
- **PATH Management**: Automatically adds `~/.local/bin` to shell PATH
- **Shell Detection**: Supports bash, zsh, fish, and other shells

## Technical Details

- **Window state**: saved to `~/.hdmi-rtsp-unified-window-state` as `WIDTHxHEIGHT+X+Y`
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

When making commits to this project, please generate commit messages that adhere to the Linux project commit guidelines with the following requirements:

- The commit title must be in the format 'subject: description'.
- The entire title (subject line) must be no more than 50 characters.
- Use the imperative mood in the title.
- Leave a blank line after the title.
- Format the commit description body with lines no more than 50 characters.
- Use clear and concise language that summarizes what was changed and why.
- Use bullet points in the message body to list multiple changes or details.
- Important: Do NOT wrap the message with triple backticks at the beginning or end. Use markdown formatting when necessary within the message body, but do NOT enclose the entire message in markdown code fences.

## Commit Behavior Guidelines

- **Only commit when explicitly requested**: Do not automatically commit changes unless the user specifically asks for a commit.

## Usage Guidelines

- **Always use a timeout**: when testing/troubleshooting, prefer `timeout ...` so the server doesn’t run forever.
- **Prefer `--debug` (and `--gst-debug` when needed)**: start with app logs, enable GStreamer logs only when diagnosing pipeline issues.
- **Default mode is quiet**: without `--debug`/`--gst-debug`, the wrapper runs the server silently unless there are errors.
- **Window state management**: Use `--reset-window` to clear saved window position/size if needed.
