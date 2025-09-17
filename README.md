# HDMI USB Capture

A simple script to detect and preview MacroSilicon USB HDMI capture devices using GStreamer.

## Features

- **Auto-detection** of MacroSilicon USB Video devices
- **Background execution** - terminal remains free for other commands
- **Window state persistence** - remembers window position and size
- **Silent operation** - no output by default (use `--debug` for logs)
- **Audio support** - automatically detects and uses audio from capture device
- **Debug mode** - verbose logging for troubleshooting
- **Help system** - built-in usage information

## Usage

```bash
# Start HDMI capture preview (silent mode)
./hdmi-usb.sh

# Start with debug output
./hdmi-usb.sh --debug

# Show help information
./hdmi-usb.sh --help

# Reset window position to default
./hdmi-usb.sh --reset-window

# Install system-wide
./install.sh
```

### Command Line Options

- `-d, --debug` - Enable debug mode (show application and GStreamer logs)
- `-h, --help` - Show help message
- `--reset-window` - Reset saved window position and size

## Requirements

- Linux with X11
- GStreamer 1.0
- v4l2-ctl
- wmctrl (for window positioning)
- MacroSilicon USB HDMI capture device

### Installing Dependencies on Ubuntu

```bash
# Install all required dependencies
sudo apt update
sudo apt install gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly gstreamer1.0-libav v4l-utils wmctrl

# Optional: Install additional GStreamer plugins for better codec support
sudo apt install gstreamer1.0-vaapi gstreamer1.0-plugins-base-apps
```

## Installation

```bash
git clone <repository>
cd capture
./install.sh
```

After installation, use `hdmi-usb` command from anywhere.

## Window State

The script automatically saves window position and size to `~/.hdmi-usb-window-state`. Use `--reset-window` to clear saved state.
