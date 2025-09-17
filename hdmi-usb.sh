#!/usr/bin/env bash
# Autodetect MacroSilicon USB Video HDMI hdmi-usb and preview with GStreamer.

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

# Optional override for ALSA card index (e.g., export AUDIO_FORCE_CARD=3)
AUDIO_FORCE_CARD="${AUDIO_FORCE_CARD:-}"

# Help function
show_help() {
  cat << EOF
HDMI USB Capture Device Preview Tool

USAGE:
    $0 [OPTIONS]

OPTIONS:
    -d, --debug          Enable debug mode (show application and GStreamer logs)
    -h, --help           Show this help message
        --reset-window   Reset saved window position and size

DESCRIPTION:
    Automatically detects MacroSilicon USB Video HDMI capture devices and
    launches a GStreamer preview window. The window position and size
    are automatically saved and restored between sessions.

EXAMPLES:
    $0                   # Launch with default settings (no debug output)
    $0 --debug           # Launch with debug output enabled
    $0 --reset-window    # Reset window state and exit

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
    --reset-window)
      WINDOW_STATE_FILE="${HOME}/.hdmi-usb-window-state"
      if [[ -f "$WINDOW_STATE_FILE" ]]; then
        rm "$WINDOW_STATE_FILE"
        echo "[INFO] Window state reset. Next launch will use default position."
      else
        echo "[INFO] No saved window state found."
      fi
      exit 0
      ;;
    *)
      err "Unknown option: $1"
      echo "Use --help for usage information."
      exit 1
      ;;
  esac
done

is_video_hdmi_usb() {
  local dev="$1"
  local info
  info="$(v4l2-ctl -d "$dev" --all 2>/dev/null || true)"
  [[ -z "$info" ]] && return 1
  # Check for Video Capture capability (common for HDMI capture devices)
  echo "$info" | grep -q "Video Capture" || return 1
  # Additional check: ensure it's not a webcam (webcams typically have lower resolutions)
  # HDMI capture devices usually support higher resolutions like 1920x1080
  echo "$info" | grep -q "1920.*1080\|1280.*720" || return 1
  return 0
}

usb_tail_for_video() {
  local dev="$1"
  local node="$(basename "$dev")"
  local sys="/sys/class/video4linux/$node/device"
  [[ -e "$sys" ]] || return 1
  local full
  full="$(readlink -f "$sys" || true)"
  [[ -z "$full" ]] && return 1
  echo "$full" | grep -oE 'usb-[^/ ]+' | tail -n1
}

alsa_card_for_usb_tail() {
  local usb_tail="$1"
  for path in /sys/class/sound/card*; do
    [[ -d "$path" ]] || continue
    local full
    full="$(readlink -f "$path/device" 2>/dev/null || true)"
    [[ -z "$full" ]] && continue
    if echo "$full" | grep -q "$usb_tail"; then
      basename "$path" | sed 's/^card//'
      return 0
    fi
  done
  return 1
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
while read -r node; do
  [[ -z "$node" ]] && continue
  if is_video_hdmi_usb "$node"; then
    VIDEO_DEV="$node"
    break
  fi
done < <(pick_nodes_by_name)

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

# --- Match ALSA card -------------------------------------------------------
AUDIO_CARD=""
if [[ -n "${AUDIO_FORCE_CARD}" ]]; then
  AUDIO_CARD="${AUDIO_FORCE_CARD}"
  log "Forcing ALSA card: ${AUDIO_CARD}"
else
  USB_TAIL="$(usb_tail_for_video "${VIDEO_DEV}" || true)"
  if [[ -n "${USB_TAIL:-}" ]]; then
    if AUDIO_CARD="$(alsa_card_for_usb_tail "$USB_TAIL" || true)"; then
      log "Matched ALSA card by USB path: card ${AUDIO_CARD}"
    else
      log "No ALSA card matched USB path (${USB_TAIL}). Running video-only."
    fi
  else
    log "Could not resolve USB path tail. Running video-only."
  fi
fi

# --- Build & run pipeline --------------------------------------------------
# Use ximagesink with automatic video scaling to fit window
GST_VIDEO="v4l2src device=${VIDEO_DEV} ! queue ! decodebin ! videoconvert ! videoscale ! ximagesink sync=false"

# Window state file for saving/restoring position and size
WINDOW_STATE_FILE="${HOME}/.hdmi-usb-window-state"

# Function to save window state
save_window_state() {
  local pid="$1"
  # Wait a moment for window to appear
  sleep 2
  
  # Try to get window ID from the GStreamer process
  local window_id
  window_id=$(xwininfo -name "gst-launch-1.0" 2>/dev/null | grep "Window id:" | awk '{print $4}' || true)
  
  if [[ -n "$window_id" ]]; then
    # Get window geometry
    local geometry
    geometry=$(xwininfo -id "$window_id" 2>/dev/null | grep -- "-geometry" | awk '{print $2}' || true)
    
    if [[ -n "$geometry" ]]; then
      echo "$geometry" > "$WINDOW_STATE_FILE"
      log "Window state saved: $geometry"
    fi
  fi
}

# Function to monitor window and save state on changes
monitor_window_state() {
  local pid="$1"
  sleep 3  # Wait for window to appear
  
  local last_geometry=""
  local window_id
  
  # Find the window ID
  window_id=$(xwininfo -name "gst-launch-1.0" 2>/dev/null | grep "Window id:" | awk '{print $4}' || true)
  
  if [[ -z "$window_id" ]]; then
    return 1
  fi
  
  log "Monitoring window $window_id for position changes..."
  
  # Monitor window position every 2 seconds
  while kill -0 "$pid" 2>/dev/null; do
    local current_geometry
    current_geometry=$(xwininfo -id "$window_id" 2>/dev/null | grep -- "-geometry" | awk '{print $2}' || true)
    
    if [[ -n "$current_geometry" && "$current_geometry" != "$last_geometry" ]]; then
      echo "$current_geometry" > "$WINDOW_STATE_FILE"
      log "Window moved, state updated: $current_geometry"
      last_geometry="$current_geometry"
    fi
    
    sleep 2
  done
  
  log "Window monitoring stopped"
}

# Function to restore window state
restore_window_state() {
  if [[ -f "$WINDOW_STATE_FILE" ]]; then
    local geometry
    geometry=$(cat "$WINDOW_STATE_FILE")
    log "Will restore window state: $geometry"
    
    # Parse geometry (format: WIDTHxHEIGHT+X+Y)
    if [[ "$geometry" =~ ^([0-9]+)x([0-9]+)\+([0-9]+)\+([0-9]+)$ ]]; then
      local width="${BASH_REMATCH[1]}"
      local height="${BASH_REMATCH[2]}"
      local x="${BASH_REMATCH[3]}"
      local y="${BASH_REMATCH[4]}"
      
      # Store the geometry for later use
      export RESTORE_X="$x"
      export RESTORE_Y="$y"
      export RESTORE_WIDTH="$width"
      export RESTORE_HEIGHT="$height"
      
      log "Will restore to: ${width}x${height} at position ${x},${y}"
    fi
  fi
}

# Function to apply window state after GStreamer starts
apply_window_state() {
  local pid="$1"
  
  if [[ -n "${RESTORE_X:-}" && -n "${RESTORE_Y:-}" ]]; then
    # Wait for window to appear with shorter intervals
    local window_id=""
    local attempts=0
    while [[ -z "$window_id" && $attempts -lt 10 ]]; do
      sleep 0.2
      window_id=$(xwininfo -name "gst-launch-1.0" 2>/dev/null | grep "Window id:" | awk '{print $4}' || true)
      ((attempts++))
    done
    
    if [[ -n "$window_id" ]]; then
      # Use wmctrl to move and resize the window
      if command -v wmctrl >/dev/null 2>&1; then
        # Apply position immediately
        wmctrl -i -r "$window_id" -e "0,${RESTORE_X},${RESTORE_Y},${RESTORE_WIDTH},${RESTORE_HEIGHT}" 2>/dev/null || true
        log "Window restored to saved position: ${RESTORE_X},${RESTORE_Y}"
        
        # Verify position was applied (optional, non-blocking)
        sleep 0.5
        local current_geometry
        current_geometry=$(xwininfo -id "$window_id" 2>/dev/null | grep -- "-geometry" | awk '{print $2}' || true)
        
        if [[ "$current_geometry" =~ ^([0-9]+)x([0-9]+)\+([0-9]+)\+([0-9]+)$ ]]; then
          local current_x="${BASH_REMATCH[3]}"
          local current_y="${BASH_REMATCH[4]}"
          
          # If position doesn't match, try once more
          if [[ $((current_x - RESTORE_X)) -ge 10 || $((current_y - RESTORE_Y)) -ge 10 ]]; then
            wmctrl -i -r "$window_id" -e "0,${RESTORE_X},${RESTORE_Y},${RESTORE_WIDTH},${RESTORE_HEIGHT}" 2>/dev/null || true
          fi
        fi
      else
        log "wmctrl not available, window position not restored"
      fi
    else
      log "Window not found after waiting, position not restored"
    fi
  fi
}

# Restore window state if available
restore_window_state

if [[ -n "${AUDIO_CARD}" ]]; then
  GST_AUDIO="alsasrc device=hw:${AUDIO_CARD},0 ! audioconvert ! audioresample ! autoaudiosink sync=false"
  log "Launching A/V preview in background (video=${VIDEO_DEV}, audio=hw:${AUDIO_CARD},0)"
  log "GStreamer command: gst-launch-1.0 ${GST_VIDEO} ${GST_AUDIO}"
  if [[ "$DEBUG_MODE" == "true" ]]; then
    gst-launch-1.0 ${GST_VIDEO} ${GST_AUDIO} &
  else
    gst-launch-1.0 ${GST_VIDEO} ${GST_AUDIO} >/dev/null 2>&1 &
  fi
  GST_PID=$!
  log "GStreamer started with PID: ${GST_PID}"
  log "To stop the preview, run: kill ${GST_PID}"
else
  log "Launching video-only preview in background (video=${VIDEO_DEV})"
  log "GStreamer command: gst-launch-1.0 ${GST_VIDEO}"
  if [[ "$DEBUG_MODE" == "true" ]]; then
    gst-launch-1.0 ${GST_VIDEO} &
  else
    gst-launch-1.0 ${GST_VIDEO} >/dev/null 2>&1 &
  fi
  GST_PID=$!
  log "GStreamer started with PID: ${GST_PID}"
  log "To stop the preview, run: kill ${GST_PID}"
fi

# Wait a moment to ensure GStreamer starts properly
sleep 1

# Check if GStreamer is still running
if kill -0 ${GST_PID} 2>/dev/null; then
  log "Preview is running successfully in the background"
  log "Terminal is now free for other commands"
  
  # Apply window state immediately with timeout and monitor window state changes
  if [[ -n "${RESTORE_X:-}" && -n "${RESTORE_Y:-}" ]]; then
    log "Restoring window to saved size: ${RESTORE_WIDTH}x${RESTORE_HEIGHT} at position: ${RESTORE_X},${RESTORE_Y}"
    
    # Create restoration script with timeout
    cat > /tmp/hdmi-restore-$$.sh << EOF
#!/bin/bash
window_id=""
attempts=0
while [[ -z "\$window_id" && \$attempts -lt 50 ]]; do
  sleep 0.1
  window_id=\$(xwininfo -name "gst-launch-1.0" 2>/dev/null | grep "Window id:" | awk '{print \$4}' || true)
  ((attempts++))
done

            if [[ -n "\$window_id" ]]; then
              wmctrl -i -r "\$window_id" -e "0,${RESTORE_X},${RESTORE_Y},${RESTORE_WIDTH},${RESTORE_HEIGHT}" 2>/dev/null || true
              if [[ "${DEBUG_MODE}" == "true" ]]; then
                echo "[INFO] Window restored to ${RESTORE_WIDTH}x${RESTORE_HEIGHT} at ${RESTORE_X},${RESTORE_Y}"
              fi
            else
              if [[ "${DEBUG_MODE}" == "true" ]]; then
                echo "[INFO] Window not found, restoration skipped"
              fi
            fi
EOF
    
    chmod +x /tmp/hdmi-restore-$$.sh
    timeout 3s /tmp/hdmi-restore-$$.sh || true
  fi
  
  # Start monitoring in background
  (monitor_window_state ${GST_PID}) &
else
  err "GStreamer failed to start properly"
  exit 1
fi
