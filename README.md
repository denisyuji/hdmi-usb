# HDMI USB Capture

Scripts to detect and preview MacroSilicon USB HDMI capture devices using GStreamer.

## Features

- **Auto-detection** of MacroSilicon USB Video devices
- **Background execution** - terminal remains free for other commands
- **Window state persistence** - remembers window position and size
- **Silent operation** - no output by default (use `--debug` for logs)
- **Audio support** - automatically detects and uses audio from capture device
- **Snapshot capture** - take single frame screenshots with timestamp
- **Debug mode** - verbose logging for troubleshooting
- **Help system** - built-in usage information

## Usage

### Live Preview (hdmi-usb.sh)

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

#### Command Line Options

- `-d, --debug` - Enable debug mode (show application and GStreamer logs)
- `-h, --help` - Show help message
- `--reset-window` - Reset saved window position and size

### Snapshot Capture (snapshot.sh)

```bash
# Capture snapshot to current directory
./snapshot.sh

# Capture to specific directory
./snapshot.sh -o ~/Pictures

# Capture with debug output
./snapshot.sh --debug

# Show help information
./snapshot.sh --help
```

#### Command Line Options

- `-d, --debug` - Enable debug mode (show GStreamer logs)
- `-h, --help` - Show help message
- `-o, --output DIR` - Output directory for snapshot (default: current directory)

#### Output

Screenshots are saved as: `screenshot_YYYYMMDD_HHMMSS.png`

On success, the script outputs only the file path to stdout, making it easy to use in scripts:

```bash
# Example: capture and open in image viewer
feh "$(./snapshot.sh)"

# Example: capture multiple snapshots
for i in {1..5}; do ./snapshot.sh -o ~/captures; sleep 2; done
```

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
