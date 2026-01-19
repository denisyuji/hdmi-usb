#!/usr/bin/env bash
# Take a snapshot from MacroSilicon USB Video HDMI capture device and save as PNG.

set -euo pipefail

# Debug mode flag
DEBUG_MODE=false

# Logging functions
log() { 
  if [[ "$DEBUG_MODE" == "true" ]]; then
    echo "[INFO] $*"
  fi
}
err() { echo "[ERR] $*" >&2; }

# Match this exact block name from `v4l2-ctl --list-devices`
MATCH_NAME="${MATCH_NAME:-MacroSilicon USB Video}"

# Output directory for snapshots (defaults to current directory)
OUTPUT_DIR="${OUTPUT_DIR:-$(pwd)}"

# Help function
show_help() {
  cat << EOF
HDMI USB Snapshot Tool

USAGE:
    $0 [OPTIONS]

OPTIONS:
    -d, --debug          Enable debug mode (show GStreamer logs)
    -h, --help           Show this help message
    -o, --output DIR     Output directory for snapshot (default: current directory)

DESCRIPTION:
    Automatically detects MacroSilicon USB Video HDMI capture devices and
    captures a single frame, saving it as a PNG file with timestamp.

EXAMPLES:
    $0                   # Capture snapshot to current directory
    $0 --debug           # Capture with debug output enabled
    $0 -o ~/Pictures     # Capture snapshot to ~/Pictures directory

OUTPUT:
    Screenshots are saved as: screenshot_YYYYMMDD_HHMMSS.png

EOF
}

# Check for command line options
while [[ $# -gt 0 ]]; do
  case $1 in
    -d|--debug)
      DEBUG_MODE=true
      shift
      ;;
    -h|--help)
      show_help
      exit 0
      ;;
    -o|--output)
      OUTPUT_DIR="$2"
      shift 2
      ;;
    *)
      err "Unknown option: $1"
      echo "Use --help for usage information."
      exit 1
      ;;
  esac
done

# Verify output directory exists or create it
if [[ ! -d "$OUTPUT_DIR" ]]; then
  log "Creating output directory: $OUTPUT_DIR"
  mkdir -p "$OUTPUT_DIR"
fi

is_video_hdmi_usb() {
  local dev="$1"
  local info
  # Capture stderr to detect busy state
  info="$(v4l2-ctl -d "$dev" --all 2>&1 || true)"
  if echo "$info" | grep -qi "Device or resource busy"; then
    return 2  # Device is busy but exists
  fi
  [[ -z "$info" ]] && return 1
  # Check for Video Capture capability (common for HDMI capture devices)
  echo "$info" | grep -q "Video Capture" || return 1
  # Additional check: ensure it's not a webcam (webcams typically have lower resolutions)
  # HDMI capture devices usually support higher resolutions like 1920x1080
  echo "$info" | grep -q "1920.*1080\|1280.*720" || return 1
  return 0
}

pick_nodes_by_name() {
  # Look for MacroSilicon devices (ID 534d:2109)
  # They typically show up as "USB Video: USB Video" in v4l2-ctl
  # We'll identify them by checking if they support high-resolution capture
  
  v4l2-ctl --list-devices | awk '
    /USB Video: USB Video/ { 
      device_line = $0
      inblk = 1
      next 
    }
    inblk && /^$/ { inblk = 0 }
    inblk && /\/dev\/video[0-9]+/ { 
      print $1
    }
  '
}

# --- Detect video node -----------------------------------------------------
VIDEO_DEV=""
# Retry detection briefly to handle transient BUSY states
for attempt in 1 2 3; do
  while read -r node; do
    [[ -z "$node" ]] && continue
    if is_video_hdmi_usb "$node"; then
      VIDEO_DEV="$node"
      break
    else
      rc=$?
      if [[ $rc -eq 2 ]]; then
        # Device is busy but exists, accept it and proceed
        VIDEO_DEV="$node"
        log "Device $node is busy but will be used"
        break
      fi
    fi
  done < <(pick_nodes_by_name)
  [[ -n "$VIDEO_DEV" ]] && break
  sleep 0.5
done

if [[ -z "$VIDEO_DEV" ]]; then
  err "Could not find a MacroSilicon USB Video HDMI capture device"
  exit 1
fi

log "Selected video node: ${VIDEO_DEV}"
if [[ "$DEBUG_MODE" == "true" ]]; then
  v4l2-ctl -d "${VIDEO_DEV}" --all | awk '
    /Card type/ {print "[INFO] " $0}
    /Bus info/ {print "[INFO] " $0}
    /Width\/Height/ {print "[INFO] " $0}
    /Pixel Format/ {print "[INFO] " $0}
    /Frames per second/ {print "[INFO] " $0}
  ' || true
fi

# --- Capture snapshot ------------------------------------------------------
# Generate timestamp for filename
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
OUTPUT_FILE="${OUTPUT_DIR}/screenshot_${TIMESTAMP}.png"

log "Capturing snapshot to: ${OUTPUT_FILE}"

# Build GStreamer pipeline to capture multiple frames
# Capture enough frames to let the device stabilize (typically 5-10 seconds)
# Save all frames to temp files, then keep only the last one
TEMP_DIR=$(mktemp -d)
TEMP_PATTERN="${TEMP_DIR}/frame_%05d.png"

# Capture 100 frames (10 seconds at 10fps, or ~1.6 seconds at 60fps)
GST_PIPELINE="v4l2src device=${VIDEO_DEV} num-buffers=100 ! image/jpeg ! jpegdec ! videoconvert ! pngenc ! multifilesink location=${TEMP_PATTERN}"

log "GStreamer pipeline: gst-launch-1.0 ${GST_PIPELINE}"

# Cleanup function to ensure device is released
cleanup_device() {
  if [[ -n "${VIDEO_DEV:-}" ]]; then
    # Force release the device by resetting v4l2 controls
    v4l2-ctl -d "${VIDEO_DEV}" --set-fmt-video=width=1920,height=1080,pixelformat=MJPG >/dev/null 2>&1 || true
    # Small delay to let device settle
    sleep 0.1
  fi
}

# Set up trap to ensure cleanup on exit
trap cleanup_device EXIT

# Execute GStreamer command with timeout
if [[ "$DEBUG_MODE" == "true" ]]; then
  timeout 10 gst-launch-1.0 ${GST_PIPELINE}
else
  timeout 10 gst-launch-1.0 ${GST_PIPELINE} >/dev/null 2>&1
fi

# Find and move the last frame to the output location
LAST_FRAME=$(ls -1 "${TEMP_DIR}"/frame_*.png 2>/dev/null | tail -1)
if [[ -n "$LAST_FRAME" ]]; then
  mv "$LAST_FRAME" "$OUTPUT_FILE"
  log "Moved last frame: $LAST_FRAME -> $OUTPUT_FILE"
fi

# Clean up temporary directory
rm -rf "$TEMP_DIR"

# Explicitly cleanup device before proceeding
cleanup_device

# Check if file was created successfully
if [[ -f "$OUTPUT_FILE" ]]; then
  # Create base64 file with same name but .base64 extension
  BASE64_FILE="${OUTPUT_FILE%.png}.base64"
  
  # Encode image as base64 and save to file (remove newlines for single-line output)
  base64 "$OUTPUT_FILE" | tr -d '\n' > "$BASE64_FILE"
  
  # Output in requested format
  echo "OK"
  echo "FILENAME=${OUTPUT_FILE}"
  echo "BASE64_FILE=${BASE64_FILE}"
  exit 0
else
  err "Failed to capture snapshot"
  exit 1
fi

