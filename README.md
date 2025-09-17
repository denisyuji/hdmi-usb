# HDMI USB Capture

A simple script to detect and preview MacroSilicon USB HDMI capture devices using GStreamer.

## Features

- **Auto-detection** of MacroSilicon USB Video devices
- **Background execution** - terminal remains free for other commands
- **Window state persistence** - remembers window position and size
- **Silent operation** - no GStreamer log spam
- **Audio support** - automatically detects and uses audio from capture device

## Usage

```bash
# Start HDMI capture preview
./hdmi-usb.sh

# Reset window position to default
./hdmi-usb.sh --reset-window

# Install system-wide
./install.sh
```

## Requirements

- Linux with X11
- GStreamer 1.0
- v4l2-ctl
- wmctrl (for window positioning)
- MacroSilicon USB HDMI capture device

## Installation

```bash
git clone <repository>
cd capture
./install.sh
```

After installation, use `hdmi-usb` command from anywhere.

## Window State

The script automatically saves window position and size to `~/.hdmi-usb-window-state`. Use `--reset-window` to clear saved state.
