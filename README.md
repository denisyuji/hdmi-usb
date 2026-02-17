# HDMI USB Capture

Scripts to detect and preview using cheap USB HDMI capture devices using GStreamer. Tested with MacroSilicon-based devices. You can either run a **live preview** locally on the machine connected to the capture device over USB, or run an **RTSP server** to stream the capture over the network (optionally with a local preview).

**AI Agent Integration**: The screenshot scripts enable AI agents to "see" the HDMI input by capturing frames as PNG and base64 files. This allows agents to verify that external devices connected to the capture card are providing the expected HDMI output.

![Tested on a cheap HDMI capture card](cheap-hdmi-usb.webp)

## Features

- **Auto-detection** of MacroSilicon USB Video devices
- **Audio support** - automatically detects and uses audio from capture device
- **Local display window** - live preview
- **RTSP streaming** - scripts to show live video capture on the screen of the local machine and/or to stream live video/audio over network
- **Snapshot capture** - take single frame screenshots from RTSP stream
- **Window state** - automatically saves and restores window position

## Usage

### Continuous Capture (Video Preview / Streaming)

#### Live Preview

Start a live preview window showing the HDMI capture. You can use either:

**Direct Python script:**
```bash
./hdmi-usb.py
```

**Wrapper script with automatic device recovery:**
```bash
./hdmi-usb
./hdmi-usb --debug
```

The `hdmi-usb` wrapper script automatically detects the capture device, attempts recovery if the device is in a bad state (USB reset or module reload), and runs the preview in the background. Use `--debug` to see output, otherwise it runs silently.

Use `--help` for more options.

#### RTSP Streaming

Stream HDMI capture over RTSP for remote viewing or recording using the unified server (`hdmi-usb.py`). The server includes a local preview window by default.

```bash
# Start RTSP server with local display (default)
python3 hdmi-usb.py

# Start without local display window
python3 hdmi-usb.py --headless

# Optional: force audio from a specific ALSA card (best-effort)
AUDIO_FORCE_CARD=1 python3 hdmi-usb.py

# Show app debug logs and/or GStreamer debug logs
python3 hdmi-usb.py --debug
python3 hdmi-usb.py --gst-debug
```

**Default RTSP URL:** `rtsp://127.0.0.1:1234/hdmi` (server listens on `0.0.0.0:1234`)

**Connect with ffplay (recommended):**
```bash
ffplay -rtsp_transport tcp rtsp://127.0.0.1:1234/hdmi
```

**Connect with GStreamer:**
```bash
gst-launch-1.0 rtspsrc location=rtsp://127.0.0.1:1234/hdmi ! decodebin ! autovideosink
```

**Note:** VLC may have compatibility issues with RTSP SETUP requests. Use ffplay or GStreamer instead.

Use `--help` for more options.

### Single Screenshot Capture

Capture a single PNG frame from an active RTSP stream. On success, the script creates two files and prints:

```text
OK
FILENAME=<absolute path to the PNG file>
BASE64_FILE=<absolute path to the base64-encoded image file>
```

The base64-encoded image is saved to a file with the same name as the PNG but with a `.base64` extension (e.g., `screenshot_20240101_120000.base64`).

#### From RTSP stream (`hdmi-usb-screenshot`)

Start the server first (for example, `./hdmi-usb --debug` or `python3 hdmi-usb.py`), then run:

```bash
./hdmi-usb-screenshot
./hdmi-usb-screenshot --output ~/Pictures

# Low-res mode: save a 640x360 PNG and print BASE64 to stdout
./hdmi-usb-screenshot --lowres
```

In `--lowres` mode, the script prints:

```text
OK
FILENAME=<absolute path to the PNG file>
WIDTH=640
HEIGHT=360
BASE64=<base64-encoded PNG>
```

Use `--help` on each script for more options.

## Installation

### Dependencies

Install required packages on Ubuntu:

```bash
sudo apt update
sudo apt install gstreamer1.0-tools gstreamer1.0-plugins-base \
  gstreamer1.0-plugins-good gstreamer1.0-plugins-bad \
  gstreamer1.0-plugins-ugly gstreamer1.0-libav v4l-utils wmctrl \
  xwininfo xprop alsa-utils python3 gir1.2-gst-rtsp-server-1.0 python3-gi

# Optional: Install ffplay for RTSP client testing
sudo apt install ffmpeg
```

**Note:** The scripts use only Python standard library modules and require no PyPI packages.

## Testing

`integration-test.sh` is a best-effort integration test that installs the scripts into `~/.local/bin`, then exercises the most important user-facing flows.

**Covered:**
- Installation via `install.sh`
- Python GI imports for GStreamer / RTSP server
- `--reset-window` behavior (clears saved window state)
- RTSP server starts and listens on `127.0.0.1:1234`
- Local preview window move/resize and window state save/restore (requires X11 + `wmctrl` + `xwininfo`)
- `hdmi-usb-screenshot` works against the running RTSP server (normal + `--lowres`)
- Headless mode (`--headless`) + screenshot

**Not covered (by design):**
- Wrapper preflight/recovery (`hdmi-usb` USB reset / `uvcvideo` reload paths)
- Audio device matching and ALSA sharing (`dsnoop`/`plughw`)
- Instance-kill behavior and orphan process cleanup
- `--gst-debug` log behavior
- `--width` forced local viewer sizing (window managers vary; can be flaky to assert)

## Window State

Window position and size are automatically saved and restored between sessions. Use `--reset-window` to clear saved state.

Window state is stored at `${XDG_CONFIG_HOME:-~/.config}/hdmi-usb/window-state` (legacy installs may have `~/.hdmi-rtsp-unified-window-state`, which is migrated automatically).
