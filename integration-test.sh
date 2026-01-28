#!/usr/bin/env bash
# Integration test runner for hdmi-usb.
#
# Tests (best-effort):
# - RTSP server can start and listen on 1234
# - Local preview window can be moved/resized
# - Window geometry is saved and restored across restarts
# - hdmi-usb-screenshot captures a frame while the server runs in background
#
# Notes:
# - Window tests require an X11 session with DISPLAY set and tools: wmctrl, xwininfo.
# - On headless systems, window tests are skipped automatically.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEST_LOG_DIR="${ROOT_DIR}/test-logs"
mkdir -p "$TEST_LOG_DIR"

TS="$(date +%Y%m%d_%H%M%S)"
LOG_FILE="${TEST_LOG_DIR}/integration_${TS}.log"

RTSP_URL_DEFAULT="rtsp://127.0.0.1:1234/hdmi"
RTSP_URL="${RTSP_URL:-$RTSP_URL_DEFAULT}"

# How long to wait for server/window operations.
START_TIMEOUT_SECONDS="${START_TIMEOUT_SECONDS:-20}"
WINDOW_TIMEOUT_SECONDS="${WINDOW_TIMEOUT_SECONDS:-20}"
SCREENSHOT_TIMEOUT_SECONDS="${SCREENSHOT_TIMEOUT_SECONDS:-30}"

# Window state file used by hdmi-usb.py
WINDOW_STATE_FILE="${WINDOW_STATE_FILE:-$HOME/.hdmi-rtsp-unified-window-state}"

info() { echo "[INFO] $*" | tee -a "$LOG_FILE" >&2; }
warn() { echo "[WARN] $*" | tee -a "$LOG_FILE" >&2; }
err() { echo "[ERR] $*" | tee -a "$LOG_FILE" >&2; }

COLOR_RED=$'\033[31m'
COLOR_GREEN=$'\033[32m'
COLOR_YELLOW=$'\033[33m'
COLOR_RESET=$'\033[0m'

pass_item() { printf "%s✅ [PASS]%s %s\n" "$COLOR_GREEN" "$COLOR_RESET" "$*" | tee -a "$LOG_FILE" >&2; }
fail_item() { printf "%s❌ [FAIL]%s %s\n" "$COLOR_RED" "$COLOR_RESET" "$*" | tee -a "$LOG_FILE" >&2; }
skip_item() { printf "%s⏭️  [SKIP]%s %s\n" "$COLOR_YELLOW" "$COLOR_RESET" "$*" | tee -a "$LOG_FILE" >&2; }

PASS_COUNT=0
FAIL_COUNT=0
SKIP_COUNT=0

mark_pass() { PASS_COUNT=$((PASS_COUNT + 1)); pass_item "$@"; }
mark_fail() { FAIL_COUNT=$((FAIL_COUNT + 1)); fail_item "$@"; }
mark_skip() { SKIP_COUNT=$((SKIP_COUNT + 1)); skip_item "$@"; }

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || { err "Missing required command: $1"; return 1; }
}

resolve_bin() {
  # Prefer installed binaries when USE_INSTALLED=1 is set.
  #
  # Usage:
  #   resolve_bin hdmi-usb "$ROOT_DIR/hdmi-usb"
  local name="$1"
  local fallback="$2"

  if [[ "${USE_INSTALLED:-0}" == "1" ]]; then
    local p
    p="$(command -v "$name" 2>/dev/null || true)"
    if [[ -n "$p" ]]; then
      echo "$p"
      return 0
    fi
    err "USE_INSTALLED=1 but '$name' not found on PATH"
    return 1
  fi

  echo "$fallback"
}

tcp_port_open() {
  local host="$1"
  local port="$2"

  if command -v nc >/dev/null 2>&1; then
    timeout 1 nc -z "$host" "$port" >/dev/null 2>&1
    return $?
  fi

  timeout 1 bash -lc "</dev/tcp/${host}/${port}" >/dev/null 2>&1
}

wait_for_tcp() {
  local host="$1"
  local port="$2"
  local deadline=$((SECONDS + START_TIMEOUT_SECONDS))
  while (( SECONDS < deadline )); do
    if tcp_port_open "$host" "$port"; then
      return 0
    fi
    sleep 1
  done
  return 1
}

kill_server() {
  local pid="${1:-}"
  [[ -n "${pid}" ]] || return 0

  if kill -0 "$pid" 2>/dev/null; then
    info "Stopping server (pid=$pid)"
    kill -INT "$pid" 2>/dev/null || true
    for _ in {1..10}; do
      kill -0 "$pid" 2>/dev/null || return 0
      sleep 1
    done
    kill -TERM "$pid" 2>/dev/null || true
  fi
}

SERVER_PID=""
cleanup() {
  kill_server "$SERVER_PID" || true
}
trap cleanup EXIT

start_server_bg() {
  info "Starting server in background: hdmi-usb.py --debug"
  # Use -u so logs flush promptly to file.
  python3 -u "${HDMI_USB_PY}" --debug >>"$LOG_FILE" 2>&1 &
  SERVER_PID="$!"
  info "Server pid=$SERVER_PID log=$LOG_FILE"
}

start_server_bg_headless() {
  info "Starting server in background (headless): hdmi-usb.py --debug --headless"
  # Use -u so logs flush promptly to file.
  python3 -u "${HDMI_USB_PY}" --debug --headless >>"$LOG_FILE" 2>&1 &
  SERVER_PID="$!"
  info "Server pid=$SERVER_PID log=$LOG_FILE"
}

parse_rtsp_host_port() {
  # echo "host port"
  local url="$1"
  local re='^rtsp://([^/:]+)(:([0-9]+))?(/.*)?$'
  if [[ "$url" =~ $re ]]; then
    echo "${BASH_REMATCH[1]} ${BASH_REMATCH[3]:-554}"
    return 0
  fi
  return 1
}

have_window_tools() {
  [[ -n "${DISPLAY:-}" ]] || return 1
  command -v wmctrl >/dev/null 2>&1 || return 1
  command -v xwininfo >/dev/null 2>&1 || return 1
  return 0
}

find_preview_window_id() {
  # Best-effort: try to find a window owned by the server PID.
  #
  # Output: window id (hex like 0x04600007)
  local pid="$1"

  local deadline=$((SECONDS + WINDOW_TIMEOUT_SECONDS))
  while (( SECONDS < deadline )); do
    # wmctrl -lp output: WIN_ID DESK PID WM_CLASS TITLE...
    local win_id
    win_id="$(wmctrl -lp 2>/dev/null | awk -v pid="$pid" '$3 == pid {print $1; exit}')"
    if [[ -n "$win_id" ]]; then
      echo "$win_id"
      return 0
    fi

    # Fallback: look for any GStreamer/OpenGL-ish window class.
    win_id="$(wmctrl -lx 2>/dev/null | awk 'tolower($0) ~ /(gstreamer|glimagesink|ximagesink|opengl)/ {print $1; exit}')"
    if [[ -n "$win_id" ]]; then
      echo "$win_id"
      return 0
    fi

    sleep 0.5
  done
  return 1
}

random_preview_geometry() {
  # Generate a single "random enough" 16:9 geometry that is likely to fit on
  # most desktops, with positive X/Y so WMs behave consistently.
  #
  # Output: "WxH+X+Y" (e.g. 824x464+137+241)
  local w h x y

  # Width range: 640..1040 in steps of 8 (even + friendly for scaling).
  w=$((640 + (RANDOM % 51) * 8))

  # 16:9 height, rounded to even.
  h=$(((w * 9 + 8) / 16))
  h=$((h - (h % 2)))

  # Place the window somewhere on-screen-ish.
  x=$((40 + (RANDOM % 401)))   # 40..440
  y=$((40 + (RANDOM % 301)))   # 40..340

  echo "${w}x${h}+${x}+${y}"
}

window_geometry() {
  local win_id="$1"
  # Extract "-geometry WxH+X+Y"
  xwininfo -id "$win_id" 2>/dev/null | awk '/-geometry/ {print $2; exit}'
}

wait_for_window_geometry() {
  local win_id="$1"
  local expect="$2"
  local deadline=$((SECONDS + WINDOW_TIMEOUT_SECONDS))
  while (( SECONDS < deadline )); do
    local g
    g="$(window_geometry "$win_id" || true)"
    if [[ "$g" == "$expect" ]]; then
      return 0
    fi
    sleep 0.5
  done
  return 1
}

wait_for_window_state_file() {
  local expect="$1"
  local deadline=$((SECONDS + WINDOW_TIMEOUT_SECONDS))
  while (( SECONDS < deadline )); do
    if [[ -f "$WINDOW_STATE_FILE" ]]; then
      local got
      got="$(cat "$WINDOW_STATE_FILE" 2>/dev/null || true)"
      if [[ "$got" == "$expect" ]]; then
        return 0
      fi
    fi
    sleep 0.5
  done
  return 1
}

main() {
  info "Integration test started (ts=$TS)"
  info "RTSP_URL=$RTSP_URL"
  info "USE_INSTALLED=${USE_INSTALLED:-0}"

  need_cmd python3

  local HDMI_USB_BIN HDMI_USB_PY HDMI_USB_SCREENSHOT
  HDMI_USB_BIN="$(resolve_bin hdmi-usb "${ROOT_DIR}/hdmi-usb")"
  HDMI_USB_PY="$(resolve_bin hdmi-usb.py "${ROOT_DIR}/hdmi-usb.py")"
  HDMI_USB_SCREENSHOT="$(resolve_bin hdmi-usb-screenshot "${ROOT_DIR}/hdmi-usb-screenshot")"

  # Ensure GStreamer GI is importable early so failures are clear.
  if python3 - <<'PY' >/dev/null
import gi
gi.require_version("Gst", "1.0")
gi.require_version("GstRtspServer", "1.0")
from gi.repository import Gst, GstRtspServer  # noqa: F401
PY
  then
    mark_pass "GStreamer GI imports"
  else
    mark_fail "GStreamer GI imports"
    goto_summary=true
  fi

  # --- CLI feature sanity checks (no device required) ---
  #
  # `hdmi-usb.py --reset-window` clears this file and exits. Validate it via the
  # wrapper script (`hdmi-usb`) too, since it has special-casing to skip device
  # preflight for print-and-exit flags.
  local window_state_file_real="$HOME/.hdmi-rtsp-unified-window-state"
  info "Testing --reset-window (via wrapper): ${window_state_file_real}"
  echo "800x450+10+10" >"$window_state_file_real"
  set +e
  "${HDMI_USB_BIN}" --reset-window >>"$LOG_FILE" 2>&1
  local reset_rc=$?
  set -e
  if [[ "$reset_rc" == "0" && ! -f "$window_state_file_real" ]]; then
    mark_pass "Reset-window: clears saved window state"
  else
    mark_fail "Reset-window: clears saved window state"
    goto_summary=true
  fi

  read -r RTSP_HOST RTSP_PORT < <(parse_rtsp_host_port "$RTSP_URL")

  # --- Start server and verify port is open ---
  local goto_summary=false
  start_server_bg
  if wait_for_tcp "$RTSP_HOST" "$RTSP_PORT"; then
    mark_pass "RTSP server creation (${RTSP_HOST}:${RTSP_PORT})"
  else
    mark_fail "RTSP server creation (${RTSP_HOST}:${RTSP_PORT})"
    goto_summary=true
  fi

  # --- Window tests (optional) ---
  local saved_geometry=""
  if have_window_tools && [[ "$goto_summary" != "true" ]]; then
    info "Window tools detected; running window geometry tests"

    local win_id
    win_id="$(find_preview_window_id "$SERVER_PID" || true)"
    if [[ -z "$win_id" ]]; then
      mark_fail "Window: find preview window"
    else
      mark_pass "Window: find preview window"
      info "Found preview window id: $win_id"
      local g0
      g0="$(window_geometry "$win_id" || true)"
      info "Initial window geometry: ${g0:-<unknown>}"

      # hdmi-usb.py intentionally ignores saving window geometry for a few
      # seconds after startup to avoid transient WM states. Wait for that
      # warmup window to pass so our resize gets persisted.
      info "Waiting for window geometry save warmup..."
      sleep 6

      # Move/resize to a deterministic geometry.
      saved_geometry="$(random_preview_geometry)"
      if [[ -n "$g0" && "$saved_geometry" == "$g0" ]]; then
        saved_geometry="$(random_preview_geometry)"
      fi
      info "Applying window geometry via wmctrl: $saved_geometry"
      IFS='x+' read -r w h x y <<<"$saved_geometry"
      if wmctrl -i -r "$win_id" -e "0,${x},${y},${w},${h}"; then
        if wait_for_window_geometry "$win_id" "$saved_geometry"; then
          mark_pass "Window: resize/move applied (${saved_geometry})"
        else
          mark_fail "Window: resize/move applied (${saved_geometry})"
        fi
      else
        mark_fail "Window: resize/move command (wmctrl)"
      fi

      if wait_for_window_state_file "$saved_geometry"; then
        mark_pass "Window: saved geometry file updated"
      else
        mark_fail "Window: saved geometry file updated"
      fi
    fi
  else
    mark_skip "Window: resize/move + save/restore (no X11 tools or earlier failure)"
  fi

  # --- Screenshot test (server in background) ---
  if [[ "$goto_summary" != "true" ]]; then
    info "Running screenshot tool against RTSP server"
    local shot_out
    set +e
    shot_out="$(timeout "$SCREENSHOT_TIMEOUT_SECONDS" "${HDMI_USB_SCREENSHOT}" -o "$TEST_LOG_DIR" -u "$RTSP_URL" 2>&1)"
    local shot_rc=$?
    set -e
    echo "$shot_out" >>"$LOG_FILE"
    if [[ "$shot_rc" != "0" ]]; then
      mark_fail "Screenshot: hdmi-usb-screenshot execution"
    else
      local png_file base64_file
      png_file="$(echo "$shot_out" | sed -n 's/^FILENAME=//p' | tail -1)"
      base64_file="$(echo "$shot_out" | sed -n 's/^BASE64_FILE=//p' | tail -1)"

      if [[ -n "$png_file" && -f "$png_file" && -s "$png_file" && -n "$base64_file" && -f "$base64_file" && -s "$base64_file" ]]; then
        mark_pass "Screenshot: hdmi-usb-screenshot execution"
        info "Screenshot OK: $png_file"
      else
        mark_fail "Screenshot: output files present/non-empty"
      fi
    fi

  else
    mark_skip "Screenshot: hdmi-usb-screenshot execution (server not ready)"
  fi

  # --- Restart server and verify restore (optional) ---
  if [[ -n "$saved_geometry" ]] && have_window_tools && [[ "$goto_summary" != "true" ]]; then
    info "Restarting server to validate window restore"
    kill_server "$SERVER_PID"
    SERVER_PID=""
    sleep 2

    start_server_bg
    if ! wait_for_tcp "$RTSP_HOST" "$RTSP_PORT"; then
      mark_fail "Window: restore (server restart + port open)"
    else
      local win_id2
      win_id2="$(find_preview_window_id "$SERVER_PID" || true)"
      if [[ -z "$win_id2" ]]; then
        mark_fail "Window: restore (find preview window)"
      else
        local g2
        g2="$(window_geometry "$win_id2" || true)"
        info "Window geometry after restart: ${g2:-<unknown>} (expected ~$saved_geometry)"
        if [[ -n "$g2" && "$g2" == "$saved_geometry" ]]; then
          mark_pass "Window: restore saved geometry"
        else
          mark_fail "Window: restore saved geometry"
        fi
      fi
    fi
  else
    mark_skip "Window: restore saved geometry (skipped)"
  fi

  # --- Headless mode sanity check ---
  if [[ "$goto_summary" != "true" ]]; then
    info "Running headless mode test (--headless): start server + screenshot"
    kill_server "$SERVER_PID"
    SERVER_PID=""
    sleep 2

    start_server_bg_headless
    if wait_for_tcp "$RTSP_HOST" "$RTSP_PORT"; then
      mark_pass "Headless: RTSP server creation (${RTSP_HOST}:${RTSP_PORT})"
    else
      mark_fail "Headless: RTSP server creation (${RTSP_HOST}:${RTSP_PORT})"
    fi

    local headless_shot_out
    set +e
    headless_shot_out="$(timeout "$SCREENSHOT_TIMEOUT_SECONDS" "${HDMI_USB_SCREENSHOT}" -o "$TEST_LOG_DIR" -u "$RTSP_URL" 2>&1)"
    local headless_shot_rc=$?
    set -e
    echo "$headless_shot_out" >>"$LOG_FILE"
    if [[ "$headless_shot_rc" != "0" ]]; then
      mark_fail "Headless: hdmi-usb-screenshot execution"
    else
      local headless_png_file headless_base64_file
      headless_png_file="$(echo "$headless_shot_out" | sed -n 's/^FILENAME=//p' | tail -1)"
      headless_base64_file="$(echo "$headless_shot_out" | sed -n 's/^BASE64_FILE=//p' | tail -1)"

      if [[ -n "$headless_png_file" && -f "$headless_png_file" && -s "$headless_png_file" && -n "$headless_base64_file" && -f "$headless_base64_file" && -s "$headless_base64_file" ]]; then
        mark_pass "Headless: hdmi-usb-screenshot execution"
        info "Headless screenshot OK: $headless_png_file"
      else
        mark_fail "Headless: screenshot output files present/non-empty"
      fi
    fi
  else
    mark_skip "Headless: start server + screenshot (skipped)"
  fi

  # --- Summary ---
  if [[ "$FAIL_COUNT" == "0" ]]; then
    pass_item "OVERALL: PASS (pass=$PASS_COUNT skip=$SKIP_COUNT)"
    info "Log: $LOG_FILE"
    return 0
  fi

  fail_item "OVERALL: FAIL (fail=$FAIL_COUNT pass=$PASS_COUNT skip=$SKIP_COUNT)"
  info "Log: $LOG_FILE"
  return 1
}

main "$@"

