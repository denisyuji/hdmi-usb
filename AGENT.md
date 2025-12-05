# Agent Documentation

## Overview

This project provides automated HDMI capture device detection and preview functionality for MacroSilicon USB devices.

## Key Components

### hdmi-usb.py
Python implementation with the following features:

- **Implementation**: Object-oriented Python 3 implementation
- **Dependencies**: Uses only Python standard library (no external PyPI dependencies)
- **Code Organization**: HDMICapture class with clean separation of concerns
- **Threading**: Background window monitoring using threading

**Features:**
- **Device Detection**: Uses `v4l2-ctl` to identify MacroSilicon USB Video devices
- **Resolution Filtering**: Ensures device supports high-resolution capture (1920x1080/1280x720)
- **GStreamer Pipeline**: `v4l2src → decodebin → videoconvert → videoscale → ximagesink`
- **Window Management**: Automatic position/size saving and restoration using `wmctrl`
- **Instance Cleanup**: Automatically kills previous instances and orphaned GStreamer processes on startup
- **Background Execution**: Runs GStreamer silently without blocking terminal
- **Process Monitoring**: Improved PID detection finds actual gst-launch process (not shell PID)
- **Window Monitoring**: Robust monitoring checks both process PID and window existence
- **Debug Mode**: `--debug` flag enables verbose logging for troubleshooting
- **Help System**: `--help` flag provides usage information
- **Window State Persistence**: Monitors and saves window position/size changes in real-time (every 2 seconds)

### snapshot.sh
- **Device Detection**: Reuses same device detection logic as hdmi-usb.py
- **Single Frame Capture**: Uses `num-buffers=1` to capture exactly one frame
- **GStreamer Pipeline**: `v4l2src → decodebin → videoconvert → pngenc → filesink`
- **Timestamp Naming**: Saves files as `snapshot_YYYYMMDD_HHMMSS.png`
- **Clean Output**: Returns only file path on success (stdout)
- **Output Directory**: Configurable via `-o` flag (default: current directory)
- **Debug Mode**: `--debug` flag enables verbose logging
- **Help System**: `--help` flag provides usage information

### install.sh
- **System Installation**: Copies script to `~/.local/bin/hdmi-usb`
- **PATH Management**: Automatically adds `~/.local/bin` to shell PATH
- **Shell Detection**: Supports bash, zsh, fish, and other shells

## Technical Details

- **Device Identification**: Looks for "USB Video: USB Video" devices with high-resolution support
- **Window State**: Saved to `~/.hdmi-usb-window-state` in format `WIDTHxHEIGHT+X+Y`
- **Window Monitoring**: Monitors window geometry every 2 seconds, saves on any change
- **Instance Management**: Uses `pgrep` to find and kill existing Python/GStreamer processes
- **PID Detection**: When using `shell=True`, finds actual gst-launch process via process tree traversal
- **Audio Detection**: Attempts to match ALSA cards by USB device path
- **Error Handling**: Graceful fallbacks for missing dependencies, robust monitoring continues even if PID detection fails

## Dependencies

- `v4l2-ctl` - Video device enumeration
- `gst-launch-1.0` - GStreamer pipeline execution
- `wmctrl` - Window positioning
- `xwininfo` - Window information
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

- **Always use a timeout**: When testing or troubleshooting, always run the script with a timeout to avoid it runs forever.
- **Always use --debug flag**: When testing or troubleshooting, always run the script with the `--debug` flag to see detailed logs and GStreamer output.
- **Default mode is silent**: Without `--debug`, the script runs silently with no output unless there are errors.
- **Window state management**: Use `--reset-window` to clear saved window position/size if needed.
