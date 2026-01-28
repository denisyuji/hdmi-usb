#!/usr/bin/env bash
# Take a snapshot from RTSP stream and save as PNG.

set -euo pipefail

# Debug mode flag
DEBUG_MODE=false

# Default RTSP URL (matches rtsp-server.py default)
RTSP_URL="${RTSP_URL:-rtsp://127.0.0.1:1234/hdmi}"

# Connection retry knobs (useful while the server is starting up)
CONNECT_RETRIES="${CONNECT_RETRIES:-10}"
CONNECT_DELAY_SECONDS="${CONNECT_DELAY_SECONDS:-1}"

# Output directory for snapshots (defaults to current directory)
OUTPUT_DIR="${OUTPUT_DIR:-$(pwd)}"

# Logging functions
log() { 
  if [[ "$DEBUG_MODE" == "true" ]]; then
    echo "[INFO] $*"
  fi
}
err() { echo "[ERR] $*" >&2; }

# Python interpreter (override if needed)
PYTHON_BIN="${PYTHON_BIN:-python3}"

rtsp_video_only() {
  # Run an RTSP client that rejects audio at rtspsrc's "select-stream" signal,
  # so audio is not SETUP'd at all.
  #
  # Usage:
  #   rtsp_video_only test
  #   rtsp_video_only capture "/tmp/frame_%05d.png"
  local mode="${1:-test}"
  local temp_pattern="${2:-}"

  MODE="$mode" TEMP_PATTERN="$temp_pattern" RTSP_URL="$RTSP_URL" DEBUG_MODE="$DEBUG_MODE" \
    RTSP_CAPTURE_SECONDS="${RTSP_CAPTURE_SECONDS:-5}" RTSP_MAX_FILES="${RTSP_MAX_FILES:-200}" \
    GST_DEBUG_NO_COLOR=1 "$PYTHON_BIN" - <<'PY'
import os
import sys

import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst, GLib

Gst.init(None)

mode = os.environ.get("MODE", "test")
url = os.environ.get("RTSP_URL", "")
debug = os.environ.get("DEBUG_MODE", "false") == "true"

temp_pattern = os.environ.get("TEMP_PATTERN", "")
capture_seconds = int(os.environ.get("RTSP_CAPTURE_SECONDS", "5"))
max_files = int(os.environ.get("RTSP_MAX_FILES", "200"))

if not url:
    print("[ERR] RTSP_URL is empty", file=sys.stderr)
    sys.exit(2)

if mode not in ("test", "capture"):
    print(f"[ERR] Invalid MODE={mode!r}", file=sys.stderr)
    sys.exit(2)

if mode == "capture" and not temp_pattern:
    print("[ERR] TEMP_PATTERN is required for MODE=capture", file=sys.stderr)
    sys.exit(2)

pipeline = Gst.Pipeline.new(f"rtsp-{mode}")
src = Gst.ElementFactory.make("rtspsrc", "src")
if not src:
    print("[ERR] Failed to create rtspsrc", file=sys.stderr)
    sys.exit(2)

src.set_property("location", url)
src.set_property("protocols", 0x4)  # tcp
src.set_property("latency", 0)

def select_stream(_src, stream_id, caps):
    # Return True to accept this stream, False to reject it (prevents SETUP).
    media = None
    try:
        if caps and caps.get_size() > 0:
            s = caps.get_structure(0)
            media = s.get_string("media")
    except Exception:
        media = None

    accept = (media == "video")
    if debug:
        print(f"[INFO] select-stream stream={stream_id} media={media!r} -> {'ACCEPT' if accept else 'REJECT'}")
    return accept

src.connect("select-stream", select_stream)

pipeline.add(src)

loop = GLib.MainLoop()
bus = pipeline.get_bus()
bus.add_signal_watch()

state = {"ok": False}

def on_message(_bus, msg):
    t = msg.type
    if t == Gst.MessageType.ERROR:
        err, dbg = msg.parse_error()
        print(f"[ERR] {err.message}", file=sys.stderr)
        if debug and dbg:
            print(f"[ERR] debug: {dbg}", file=sys.stderr)
        loop.quit()
    elif t == Gst.MessageType.EOS:
        state["ok"] = True
        loop.quit()
    elif t == Gst.MessageType.STATE_CHANGED and msg.src == pipeline and mode == "test":
        old, new, pending = msg.parse_state_changed()
        if new == Gst.State.PLAYING:
            state["ok"] = True
            loop.quit()
    return True

bus.connect("message", on_message)

if mode == "test":
    sink = Gst.ElementFactory.make("fakesink", "sink")
    if not sink:
        print("[ERR] Failed to create fakesink", file=sys.stderr)
        sys.exit(2)

    pipeline.add(sink)

    def on_pad_added(_src, pad):
        sink_pad = sink.get_static_pad("sink")
        if sink_pad and not sink_pad.is_linked():
            pad.link(sink_pad)

    src.connect("pad-added", on_pad_added)

    pipeline.set_state(Gst.State.PLAYING)
    GLib.timeout_add_seconds(5, lambda: (loop.quit(), False)[1])
    loop.run()
    pipeline.set_state(Gst.State.NULL)
    sys.exit(0 if state["ok"] else 1)

# mode == "capture"
queue = Gst.ElementFactory.make("queue", "queue")
decode = Gst.ElementFactory.make("decodebin", "decode")
videoconvert = Gst.ElementFactory.make("videoconvert", "videoconvert")
pngenc = Gst.ElementFactory.make("pngenc", "pngenc")
sink = Gst.ElementFactory.make("multifilesink", "sink")

if not all([queue, decode, videoconvert, pngenc, sink]):
    print("[ERR] Failed to create capture elements", file=sys.stderr)
    sys.exit(2)

sink.set_property("location", temp_pattern)
sink.set_property("max-files", max_files)

for el in (queue, decode, videoconvert, pngenc, sink):
    pipeline.add(el)

if not queue.link(decode):
    print("[ERR] Failed to link queue->decodebin", file=sys.stderr)
    sys.exit(2)
if not videoconvert.link(pngenc) or not pngenc.link(sink):
    print("[ERR] Failed to link videoconvert/pngenc/sink", file=sys.stderr)
    sys.exit(2)

def on_src_pad_added(_src, pad):
    sink_pad = queue.get_static_pad("sink")
    if sink_pad and not sink_pad.is_linked():
        pad.link(sink_pad)

def on_decode_pad_added(_dec, pad):
    caps = pad.get_current_caps() or pad.query_caps(None)
    if not caps or caps.get_size() == 0:
        return
    s = caps.get_structure(0)
    if not s.get_name().startswith("video/"):
        return
    sink_pad = videoconvert.get_static_pad("sink")
    if sink_pad and not sink_pad.is_linked():
        pad.link(sink_pad)

src.connect("pad-added", on_src_pad_added)
decode.connect("pad-added", on_decode_pad_added)

def stop_capture():
    try:
        pipeline.send_event(Gst.Event.new_eos())
    except Exception:
        loop.quit()
    return False

pipeline.set_state(Gst.State.PLAYING)
GLib.timeout_add_seconds(max(1, capture_seconds), stop_capture)
loop.run()
pipeline.set_state(Gst.State.NULL)

sys.exit(0 if state["ok"] else 1)
PY
}

# Parse rtsp://HOST:PORT/PATH into HOST and PORT (PORT may be omitted).
parse_rtsp_host_port() {
  local url="$1"
  local re='^rtsp://([^/:]+)(:([0-9]+))?(/.*)?$'
  if [[ "$url" =~ $re ]]; then
    echo "${BASH_REMATCH[1]} ${BASH_REMATCH[3]:-554}"
    return 0
  fi
  return 1
}

# Best-effort TCP connect check before invoking GStreamer.
tcp_port_open() {
  local host="$1"
  local port="$2"

  # Prefer nc if available; fall back to bash /dev/tcp.
  if command -v nc >/dev/null 2>&1; then
    timeout 1 nc -z "$host" "$port" >/dev/null 2>&1
    return $?
  fi

  # /dev/tcp is a bash feature; this works for IPv4 hostnames.
  timeout 1 bash -lc "</dev/tcp/${host}/${port}" >/dev/null 2>&1
}

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
    - Optional: netcat (nc) for a quick TCP port check

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

# Quick TCP check (and optional retries) so we can show a clearer error when the
# server simply isn't listening yet.
read -r RTSP_HOST RTSP_PORT < <(parse_rtsp_host_port "$RTSP_URL" || echo "")
if [[ -z "${RTSP_HOST:-}" || -z "${RTSP_PORT:-}" ]]; then
  err "Invalid RTSP URL: ${RTSP_URL}"
  err "Expected format: rtsp://HOST:PORT/PATH"
  exit 1
fi

attempt=1
while true; do
  if tcp_port_open "$RTSP_HOST" "$RTSP_PORT"; then
    log "TCP port is open: ${RTSP_HOST}:${RTSP_PORT}"
    break
  fi

  if (( attempt >= CONNECT_RETRIES )); then
    err "RTSP server does not appear to be listening on ${RTSP_HOST}:${RTSP_PORT}"
    err "If you're starting the server and then taking a screenshot, try:"
    err "  CONNECT_RETRIES=20 CONNECT_DELAY_SECONDS=1 ./screenshot-rtsp.sh"
    exit 1
  fi

  log "Port not open yet (${RTSP_HOST}:${RTSP_PORT}); retrying (${attempt}/${CONNECT_RETRIES})..."
  attempt=$((attempt + 1))
  sleep "$CONNECT_DELAY_SECONDS"
done

# Test RTSP connectivity (quick check + retries)
log "Testing RTSP connection..."
connect_attempt=1
while true; do
  set +e
  rtsp_video_only test
  rc=$?
  set -e

  if [[ "$rc" == "0" ]]; then
    break
  fi

  if (( connect_attempt >= CONNECT_RETRIES )); then
    err "Failed to connect to RTSP server at ${RTSP_URL}"
    err "Make sure the RTSP server is running and accessible"
    if [[ "$DEBUG_MODE" != "true" ]]; then
      err "Re-run with --debug for stream selection details."
    fi
    exit 1
  fi

  log "RTSP connect failed (attempt ${connect_attempt}/${CONNECT_RETRIES}); retrying in ${CONNECT_DELAY_SECONDS}s..."
  connect_attempt=$((connect_attempt + 1))
  sleep "$CONNECT_DELAY_SECONDS"
done
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
# Video-only capture: use rtspsrc's "select-stream" signal to reject audio
# streams before RTSP SETUP happens.
log "Capturing video-only frames via Python/GStreamer (audio rejected at select-stream)"

# Cleanup function
cleanup_temp() {
  if [[ -d "${TEMP_DIR:-}" ]]; then
    rm -rf "$TEMP_DIR"
  fi
}

# Set up trap to ensure cleanup on exit
trap cleanup_temp EXIT

RTSP_CAPTURE_SECONDS="${RTSP_CAPTURE_SECONDS:-5}"
RTSP_MAX_FILES="${RTSP_MAX_FILES:-200}"

set +e
rtsp_video_only capture "$TEMP_PATTERN"
CAP_RC=$?
set -e

if [[ "$CAP_RC" != "0" ]]; then
  err "Capture failed (rc=${CAP_RC})"
  exit "$CAP_RC"
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

