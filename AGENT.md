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
