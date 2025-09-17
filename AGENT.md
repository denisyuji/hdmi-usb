# Agent Documentation

## Overview

This project provides automated HDMI capture device detection and preview functionality for MacroSilicon USB devices.

## Key Components

### hdmi-usb.sh
- **Device Detection**: Uses `v4l2-ctl` to identify MacroSilicon USB Video devices
- **Resolution Filtering**: Ensures device supports high-resolution capture (1920x1080/1280x720)
- **GStreamer Pipeline**: `v4l2src → decodebin → videoconvert → ximagesink`
- **Window Management**: Automatic position/size saving and restoration using `wmctrl`
- **Background Execution**: Runs GStreamer silently without blocking terminal

### install.sh
- **System Installation**: Copies script to `~/.local/bin/hdmi-usb`
- **PATH Management**: Automatically adds `~/.local/bin` to shell PATH
- **Shell Detection**: Supports bash, zsh, fish, and other shells

## Technical Details

- **Device Identification**: Looks for "USB Video: USB Video" devices with high-resolution support
- **Window State**: Saved to `~/.hdmi-usb-window-state` in format `WIDTHxHEIGHT+X+Y`
- **Audio Detection**: Attempts to match ALSA cards by USB device path
- **Error Handling**: Graceful fallbacks for missing dependencies

## Dependencies

- `v4l2-ctl` - Video device enumeration
- `gst-launch-1.0` - GStreamer pipeline execution
- `wmctrl` - Window positioning
- `xwininfo` - Window information
- `lsusb` - USB device listing
