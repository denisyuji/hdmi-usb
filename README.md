# HDMI USB Capture

Scripts to detect and preview using cheap USB HDMI capture devices using GStreamer. This was tested with a Macrosilicon-based device.

![Tested on a cheap hdmi capture card](cheap-hdmi-usb.webp)

## Features

- **Auto-detection** of MacroSilicon USB Video devices
- **Audio support** - automatically detects and uses audio from capture device
- **Local display window** - live preview with automatic video sharing
- **RTSP streaming** - stream live video/audio over network
- **Snapshot capture** - take single frame screenshots with timestamp
- **Debug mode** - verbose logging for troubleshooting
- **Help system** - built-in usage information

## Usage

### Live Preview

```bash
# Start HDMI capture preview (silent mode)
./hdmi-usb.py

# Start with debug output
./hdmi-usb.py --debug

# Show help information
./hdmi-usb.py --help

# Reset window position to default
./hdmi-usb.py --reset-window
```

#### Command Line Options

- `-d, --debug` - Enable debug mode (show application and GStreamer logs)
- `-h, --help` - Show help message
- `--reset-window` - Reset saved window position and size

### Snapshot Capture

#### Direct Device Capture (snapshot.sh)

Capture snapshots directly from the HDMI capture device.

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

#### RTSP Stream Capture (screenshot-rtsp.sh)

Capture snapshots from an active RTSP stream (requires RTSP server to be running).

```bash
# Capture from default RTSP stream
./screenshot-rtsp.sh

# Capture from custom RTSP URL
RTSP_URL=rtsp://192.168.1.100:1234/hdmi ./screenshot-rtsp.sh

# Capture to specific directory
OUTPUT_DIR=~/Pictures ./screenshot-rtsp.sh

# Capture with debug output
./screenshot-rtsp.sh --debug

# Show help information
./screenshot-rtsp.sh --help
```

#### Command Line Options

**snapshot.sh:**
- `-d, --debug` - Enable debug mode (show GStreamer logs)
- `-h, --help` - Show help message
- `-o, --output DIR` - Output directory for snapshot (default: current directory)

**screenshot-rtsp.sh:**
- `-d, --debug` - Enable debug mode (show GStreamer logs)
- `-h, --help` - Show help message

#### Environment Variables

**screenshot-rtsp.sh:**
- `RTSP_URL` - RTSP stream URL (default: `rtsp://127.0.0.1:1234/hdmi`)
- `OUTPUT_DIR` - Output directory for snapshots (default: current directory)

#### Output

Screenshots are saved as: `screenshot_YYYYMMDD_HHMMSS.png`

On success, the scripts output only the file path to stdout, making it easy to use in scripts:

```bash
# Example: capture and open in image viewer
feh "$(./snapshot.sh)"

# Example: capture multiple snapshots
for i in {1..5}; do ./snapshot.sh -o ~/captures; sleep 2; done

# Example: capture from RTSP stream
./screenshot-rtsp.sh -o ~/rtsp-captures
```

### RTSP Server (rtsp-server.py)

Stream HDMI capture over RTSP for remote viewing or recording. Features local display window with automatic video sharing between local preview and RTSP clients.

```bash
# Start RTSP server with local display (default)
python3 rtsp-server.py

# Start with debug output
python3 rtsp-server.py --debug

# Start in headless mode (no local display window)
python3 rtsp-server.py --headless

# Stream audio only (requires AUDIO_FORCE_CARD)
python3 rtsp-server.py --audio-only

# Reset saved window position
python3 rtsp-server.py --reset-window

# Force specific audio card
AUDIO_FORCE_CARD=1 python3 rtsp-server.py

# Show help information
python3 rtsp-server.py --help
```

#### Connecting to the RTSP Stream

**Default RTSP URL:** `rtsp://127.0.0.1:1234/hdmi`

**Recommended client (ffplay):**
```bash
ffplay -rtsp_transport tcp rtsp://127.0.0.1:1234/hdmi
```

**GStreamer:**
```bash
gst-launch-1.0 rtspsrc location=rtsp://127.0.0.1:1234/hdmi ! decodebin ! autovideosink
```

#### Client Compatibility

- ✅ **Works with:** ffplay, GStreamer, most standards-compliant RTSP clients
- ⚠️  **Known issues:** VLC may have compatibility issues with RTSP SETUP requests (use ffplay instead)

#### Command Line Options

- `--debug` - Enable debug output (shows GStreamer messages)
- `--headless` - Disable local display window (RTSP server only)
- `--audio-only` - Stream audio only (requires AUDIO_FORCE_CARD environment variable)
- `--reset-window` - Reset saved window position and size
- `-h, --help` - Show help message

#### Advanced Features

- **Local Display Window**: Shows live preview with automatic window state persistence
- **Video Sharing**: Uses intervideosink/src to share video between local display and RTSP clients
- **Robust Cleanup**: Comprehensive cleanup system handles all termination scenarios
- **Device Auto-detection**: Automatically finds and configures HDMI capture devices
- **Audio Integration**: Detects and streams audio from the same USB device

#### Environment Variables

- `AUDIO_FORCE_CARD` - Force specific ALSA audio card (e.g., `AUDIO_FORCE_CARD=1`)
- `RTSP_URL` - Custom RTSP stream URL for screenshot-rtsp.sh (default: `rtsp://127.0.0.1:1234/hdmi`)
- `OUTPUT_DIR` - Output directory for RTSP screenshots (default: current directory)

## Requirements

- Linux with X11
- GStreamer 1.0
- v4l2-ctl
- wmctrl (for window positioning)
- MacroSilicon USB HDMI capture device
- Python 3.6+ (for Python version and RTSP server)
- GStreamer RTSP Server library (for RTSP server only)

### Installing Dependencies on Ubuntu

```bash
# Install all required dependencies
sudo apt update
sudo apt install gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly gstreamer1.0-libav v4l-utils wmctrl

# For Python version (Python 3 is usually pre-installed on Ubuntu)
sudo apt install python3

# For RTSP server
sudo apt install gir1.2-gst-rtsp-server-1.0 python3-gi

# Optional: Install additional GStreamer plugins for better codec support
sudo apt install gstreamer1.0-vaapi gstreamer1.0-plugins-base-apps

# Optional: Install ffplay for RTSP client testing
sudo apt install ffmpeg
```

**Note:** The `hdmi-usb.py` script uses only Python standard library modules and requires no PyPI packages. See `requirements.txt` for details.


## Window State

The scripts automatically save window position and size for restoration between sessions:

- **Live Preview Scripts**: `~/.hdmi-usb-window-state`
- **RTSP Server**: `~/.hdmi-usb-rtsp-window-state`

Use `--reset-window` to clear saved state for any script.
