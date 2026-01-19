#!/usr/bin/env bash
# Take a snapshot from RTSP stream and save as PNG.

set -euo pipefail

# Debug mode flag
DEBUG_MODE=false

# Default RTSP URL (matches rtsp-server.py default)
RTSP_URL="${RTSP_URL:-rtsp://127.0.0.1:1234/hdmi}"

# Output directory for snapshots (defaults to current directory)
OUTPUT_DIR="${OUTPUT_DIR:-$(pwd)}"

# Logging functions
log() { 
  if [[ "$DEBUG_MODE" == "true" ]]; then
    echo "[INFO] $*"
  fi
}
err() { echo "[ERR] $*" >&2; }

# Help function
show_help() {
  cat << EOF
RTSP Screenshot Tool

USAGE:
    $0 [OPTIONS]

OPTIONS:
    -d, --debug          Enable debug mode (show GStreamer logs)
    -h, --help           Show this help message
    -o, --output DIR     Output directory for snapshot (default: current directory)
    -u, --url URL        RTSP URL (default: rtsp://127.0.0.1:1234/hdmi)

DESCRIPTION:
    Connects to an RTSP server and captures a single frame, saving it as
    a PNG file with timestamp. The script captures multiple frames to let
    the stream stabilize, then saves the last frame.

EXAMPLES:
    $0                                    # Capture from default RTSP URL
    $0 --debug                            # Capture with debug output enabled
    $0 -o ~/Pictures                      # Capture to ~/Pictures directory
    $0 -u rtsp://192.168.1.100:8554/live  # Capture from custom RTSP URL
    RTSP_URL=rtsp://server:8554/stream $0 # Using environment variable

OUTPUT:
    Screenshots are saved as: screenshot_YYYYMMDD_HHMMSS.png

REQUIREMENTS:
    - GStreamer 1.0 with RTSP support (gstreamer1.0-plugins-good)
    - RTSP server must be running and accessible

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
    -u|--url)
      RTSP_URL="$2"
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

# Validate RTSP URL format
if [[ ! "$RTSP_URL" =~ ^rtsp:// ]]; then
  err "Invalid RTSP URL: $RTSP_URL"
  err "URL must start with rtsp://"
  exit 1
fi

log "RTSP URL: ${RTSP_URL}"

# Test RTSP connectivity (quick check)
log "Testing RTSP connection..."
if ! timeout 5 gst-launch-1.0 rtspsrc location="${RTSP_URL}" protocols=tcp latency=0 ! fakesink num-buffers=1 >/dev/null 2>&1; then
  err "Failed to connect to RTSP server at ${RTSP_URL}"
  err "Make sure the RTSP server is running and accessible"
  exit 1
fi
log "RTSP connection successful"

# --- Capture snapshot ------------------------------------------------------
# Generate timestamp for filename
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
OUTPUT_FILE="${OUTPUT_DIR}/screenshot_${TIMESTAMP}.png"

log "Capturing snapshot to: ${OUTPUT_FILE}"

# Build GStreamer pipeline to capture multiple frames
# Capture enough frames to let the stream stabilize (typically 5-10 seconds)
# Save all frames to temp files, then keep only the last one
TEMP_DIR=$(mktemp -d)
TEMP_PATTERN="${TEMP_DIR}/frame_%05d.png"

# Capture frames from RTSP stream
# Use TCP transport for reliability, decodebin to handle any codec
# We'll capture for a few seconds then send SIGINT for graceful shutdown
GST_PIPELINE="rtspsrc location=${RTSP_URL} protocols=tcp latency=0 ! decodebin ! videoconvert ! pngenc ! multifilesink location=${TEMP_PATTERN} max-files=100"

log "GStreamer pipeline: gst-launch-1.0 ${GST_PIPELINE}"

# Cleanup function
cleanup_temp() {
  if [[ -d "${TEMP_DIR:-}" ]]; then
    rm -rf "$TEMP_DIR"
  fi
}

# Set up trap to ensure cleanup on exit
trap cleanup_temp EXIT

# Execute GStreamer command with timeout using SIGINT for graceful shutdown
# SIGINT allows gst-launch to send EOS and properly finalize files
# Capture for 5 seconds which should give us plenty of frames
if [[ "$DEBUG_MODE" == "true" ]]; then
  timeout --signal=INT 5 gst-launch-1.0 rtspsrc location="${RTSP_URL}" protocols=tcp latency=0 ! decodebin ! videoconvert ! pngenc ! multifilesink location="${TEMP_PATTERN}" max-files=100 || true
else
  timeout --signal=INT 5 gst-launch-1.0 rtspsrc location="${RTSP_URL}" protocols=tcp latency=0 ! decodebin ! videoconvert ! pngenc ! multifilesink location="${TEMP_PATTERN}" max-files=100 >/dev/null 2>&1 || true
fi

# Find and move the last frame to the output location
LAST_FRAME=$(ls -1 "${TEMP_DIR}"/frame_*.png 2>/dev/null | tail -1)
if [[ -n "$LAST_FRAME" ]]; then
  mv "$LAST_FRAME" "$OUTPUT_FILE"
  log "Moved last frame: $LAST_FRAME -> $OUTPUT_FILE"
else
  err "No frames captured from RTSP stream"
  exit 1
fi

# Clean up temporary directory
cleanup_temp

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

