#!/usr/bin/env python3
"""Unified RTSP Server for HDMI USB Capture Devices

Merges the best features from rtsp-server.py and hdmi-usb.py:
- RTSP streaming with local preview window
- Device state reset and validation
- Instance management (kills existing instances)
- Enhanced device validation with better error handling
- Robust cleanup system for all termination scenarios
- Audio integration from USB capture devices

Key Features:
- Auto-detection of HDMI capture devices with state validation
- RTSP streaming with local preview window
- Video sharing between local display and RTSP clients
- Automatic recovery from device stuck states
- Prevents conflicts from multiple instances
- Comprehensive error messages with troubleshooting steps
"""
import gi
import argparse
import signal
import os
import re
import subprocess
import atexit
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

# Ensure log output is line-buffered even when stdout/stderr are piped
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(line_buffering=True)
except Exception:
    # Best-effort; fall back to default buffering on failure
    pass

# glimagesink (the preferred local-preview sink, see build_pipeline) opens a
# native Wayland surface when running under a Wayland session. That surface
# bypasses X11 entirely: it never requests window decorations, and none of
# wmctrl/xprop/xwininfo (used below for geometry restore, forced 16:9 sizing,
# and decoration hints) can see or manage it, since those are X11-only tools.
# Forcing GstGL's windowing backend to X11 makes it create a normal XWayland
# top-level window instead - decorated by the compositor like any other X11
# client, and manageable by the existing window-management code. This is a
# no-op on a plain X11 session, where X11 is already the only option.
os.environ.setdefault('GST_GL_WINDOW', 'x11')

gi.require_version('Gst', '1.0')
gi.require_version('GstRtsp', '1.0')
gi.require_version('GstRtspServer', '1.0')
from gi.repository import Gst, GstRtsp, GstRtspServer, GLib, GObject

# Configuration constants
DEFAULT_RTSP_PORT = "1234"
DEFAULT_RTSP_ENDPOINT = "/hdmi"
RTSP_LATENCY_MS = 200
SUBPROCESS_TIMEOUT_SECONDS = 5
AUDIO_SAMPLE_RATE_HZ = 48000
AUDIO_BITRATE_BPS = 128000
VIDEO_BITRATE_KBPS = 3000
VIDEO_KEYFRAME_INTERVAL_FRAMES = 30
VIDEO_CAPTURE_FPS = 60
# Window after the hardware-decode capture pipeline reaches PLAYING during which
# a pipeline error is treated as a hardware failure and retried in software.
HW_DECODE_PROBATION_SECONDS = 10


def _round_even(value: int) -> int:
    """Round down to the nearest even integer (some sinks expect even sizes)."""
    return value if value % 2 == 0 else value - 1


def _compute_height_for_16_9(width: int) -> int:
    """Compute a 16:9 height for the given width."""
    # Use rounding to preserve aspect ratio reasonably for arbitrary widths.
    height = int(round(width * 9 / 16))
    return _round_even(max(height, 2))


def _compute_width_for_16_9(height: int) -> int:
    """Compute a 16:9 width for the given height."""
    width = int(round(height * 16 / 9))
    return _round_even(max(width, 2))

def setup_gstreamer_debug():
    """Configure GStreamer logging.

    This project distinguishes between:
    - App debug logs (our `[INFO]`, `[LOCAL]`, etc.) via `--debug`
    - GStreamer debug logs via `--gst-debug`

    By default, we keep GStreamer logs quiet to avoid drowning out app logs.
    """
    import sys

    argv = set(sys.argv)

    # If the user explicitly requests GStreamer logs, enable them.
    if '--gst-debug' in argv:
        # Keep important pipeline diagnostics, but suppress recurring benign
        # warnings/FIXMEs from GStreamer internals and device drivers that are
        # expected on this capture stack and drown out actionable issues.
        os.environ['GST_DEBUG'] = os.environ.get(
            'GST_DEBUG',
            '3,default:2,videodecoder:1,rtspstream:1,rtpsession:1,'
            'rtspmedia:1,udpsrc:1,rtpjitterbuffer:1,GST_PADS:1,alsa:1,'
            'v4l2:1,v4l2bufferpool:1'
        )
        os.environ['GST_DEBUG_NO_COLOR'] = '1'
        return

    # If app debug is enabled (or even in normal mode), keep GStreamer quiet unless
    # the user explicitly opted in via --gst-debug.
    #
    # This also prevents an externally-set GST_DEBUG from spamming output when the
    # user just wants `[LOCAL]` debug messages.
    os.environ['GST_DEBUG'] = '0'
    os.environ['GST_DEBUG_NO_COLOR'] = '1'

# Setup debug environment before GStreamer initialization
setup_gstreamer_debug()

Gst.init(None)

# =============================================================================
# Global Cleanup System
# =============================================================================
# Registry for cleanup functions to ensure proper resource cleanup in all
# termination scenarios (normal exit, signals, exceptions, etc.)

_cleanup_registry = []


def register_cleanup(cleanup_func, *args, **kwargs):
    """Register a cleanup function to be called on exit."""
    _cleanup_registry.append((cleanup_func, args, kwargs))


def cleanup_all():
    """Execute all registered cleanup functions."""
    for cleanup_func, args, kwargs in _cleanup_registry:
        try:
            cleanup_func(*args, **kwargs)
        except Exception as e:
            print(f"⚠️  Cleanup error: {e}")


# Register global cleanup handler
atexit.register(cleanup_all)


# =============================================================================
# Utility Functions
# =============================================================================

def get_window_state_path() -> Path:
    """Return a fixed path for saving/loading window geometry (independent of cwd).
    Uses XDG config directory so the file is always in the same place."""
    xdg = os.environ.get('XDG_CONFIG_HOME')
    if xdg:
        xdg_path = Path(xdg).expanduser()
        # The XDG base dir spec requires an absolute path. If a relative path is
        # exported, falling back avoids making window state depend on the cwd.
        if xdg_path.is_absolute():
            config_home = xdg_path
        else:
            config_home = Path.home() / '.config'
    else:
        config_home = Path.home() / '.config'
    return config_home / 'hdmi-usb' / 'window-state'


def timestamp() -> str:
    """Return current timestamp in standard format."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def kill_existing_instances(script_name: str = "hdmi-rtsp-unified.py", debug_mode: bool = False):
    """Kill other instances of this script and their GStreamer processes.
    
    This prevents device conflicts from multiple instances trying to access
    the same video/audio device.
    """
    current_pid = os.getpid()
    killed_count = 0
    
    def log(message: str):
        if debug_mode:
            print(f"[INSTANCE] {message}")
    
    try:
        # Find all python processes running this script (excluding current process).
        #
        # Important: we anchor the regex to the beginning of the command line so
        # we do NOT match wrapper processes like `timeout 30 python3 ...`.
        # If we kill `timeout`, it will typically terminate *this* process.
        python_cmd_re = rf'(^|.*/)(python3?|python)\s+.*{re.escape(script_name)}'
        result = subprocess.run(
            ['pgrep', '-f', python_cmd_re],
            capture_output=True,
            text=True,
            timeout=2
        )
        
        if result.returncode == 0:
            pids = [int(pid.strip()) for pid in result.stdout.strip().split('\n') if pid.strip()]
            for pid in pids:
                if pid != current_pid:
                    try:
                        log(f"Killing existing instance (PID: {pid})")
                        os.kill(pid, signal.SIGTERM)
                        killed_count += 1
                        # Wait a bit for graceful shutdown
                        time.sleep(0.5)
                        # Force kill if still running
                        try:
                            os.kill(pid, 0)
                            os.kill(pid, signal.SIGKILL)
                        except OSError:
                            pass
                    except (OSError, ProcessLookupError):
                        pass
        
        # Also kill any orphaned gst-launch processes that might be using v4l2src
        time.sleep(0.5)  # Give processes time to exit
        result = subprocess.run(
            ['pgrep', '-f', 'gst-launch-1.0.*v4l2src'],
            capture_output=True,
            text=True,
            timeout=2
        )
        
        if result.returncode == 0:
            gst_pids = [int(pid.strip()) for pid in result.stdout.strip().split('\n') if pid.strip()]
            for pid in gst_pids:
                try:
                    log(f"Killing orphaned GStreamer process (PID: {pid})")
                    os.kill(pid, signal.SIGTERM)
                    time.sleep(0.2)
                    try:
                        os.kill(pid, 0)
                        os.kill(pid, signal.SIGKILL)
                    except OSError:
                        pass
                except (OSError, ProcessLookupError):
                    pass
        
        if killed_count > 0:
            log(f"Killed {killed_count} existing instance(s)")
            time.sleep(1)  # Give processes time to fully exit
            
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError, FileNotFoundError):
        # pgrep not available or failed, try alternative method
        pass


# =============================================================================
# Device Detection and Management
# =============================================================================

class HDMIDeviceDetector:
    """Detects and validates HDMI capture devices and associated audio cards.
    
    Enhanced with device state validation and better error handling from hdmi-usb.py
    """

    def __init__(self, debug_mode: bool = False):
        self.debug_mode = debug_mode
        self.audio_force_card = os.environ.get('AUDIO_FORCE_CARD', '')

    def log(self, message: str) -> None:
        """Print log message if debug mode is enabled."""
        if self.debug_mode:
            print(f"[INFO] {message}")

    def is_video_hdmi_usb(self, device: str) -> bool:
        """Check if device is a video HDMI capture device.
        
        Enhanced validation from hdmi-usb.py:
        - Checks file existence
        - Checks device accessibility/permissions
        - Better error logging
        - Multiple resolution pattern matching
        """
        # First check if device file exists and is accessible
        if not os.path.exists(device):
            self.log(f"Device {device} does not exist")
            return False
        
        # Check if device is readable (not locked by another process)
        try:
            with open(device, 'rb') as f:
                pass
        except PermissionError:
            self.log(f"Device {device} is not accessible (may be in use by another process)")
            return False
        except Exception as e:
            self.log(f"Cannot access device {device}: {e}")
            return False
        
        try:
            result = subprocess.run(
                ['v4l2-ctl', '-d', device, '--all'],
                capture_output=True,
                text=True,
                timeout=SUBPROCESS_TIMEOUT_SECONDS
            )
            
            # Log stderr if there are errors
            if result.stderr:
                self.log(f"v4l2-ctl stderr for {device}: {result.stderr}")
            
            # If command failed, log the error
            if result.returncode != 0:
                self.log(f"v4l2-ctl failed for {device} (return code: {result.returncode})")
                if result.stderr:
                    self.log(f"Error: {result.stderr}")
                return False
            
            info = result.stdout
            
            if not info:
                self.log(f"No output from v4l2-ctl for {device}")
                return False
            
            # Check for Video Capture capability
            if 'Video Capture' not in info:
                self.log(f"Device {device} does not have 'Video Capture' capability")
                if self.debug_mode:
                    lines = info.splitlines()[:10]
                    self.log(f"Sample output from {device}: {lines}")
                return False
            
            # Check for high resolution support (HDMI capture devices)
            # Try multiple patterns to catch different formats
            resolution_patterns = [
                r'1920.*1080',
                r'1280.*720',
                r'1920x1080',
                r'1280x720',
                r'Width/Height.*1920.*1080',
                r'Width/Height.*1280.*720'
            ]
            
            has_resolution = any(re.search(pattern, info, re.IGNORECASE) for pattern in resolution_patterns)
            
            if not has_resolution:
                self.log(f"Device {device} does not report expected HDMI resolutions")
                if self.debug_mode:
                    format_lines = [line for line in info.splitlines() 
                                  if 'Size:' in line or 'Width/Height' in line or 'fmt' in line.lower()]
                    if format_lines:
                        self.log(f"Available formats/resolutions for {device}: {format_lines[:5]}")
                # Still allow the device if it has Video Capture - resolution might be negotiated at runtime
                self.log(f"Warning: Device {device} has Video Capture but no expected HDMI resolutions found - will try anyway")
                return True  # Allow it - GStreamer can negotiate formats
            
            return True

        except subprocess.TimeoutExpired:
            self.log(f"Timeout querying device {device}")
            return False
        except subprocess.CalledProcessError as e:
            self.log(f"Error querying device {device}: {e}")
            if e.stderr:
                self.log(f"Error details: {e.stderr}")
            return False
        except FileNotFoundError:
            print("❌ ERROR: v4l2-ctl not found. Please install v4l-utils: sudo apt install v4l-utils", file=sys.stderr)
            return False
        except Exception as e:
            self.log(f"Unexpected error checking device {device}: {e}")
            return False

    def check_device_streaming(self, video_dev: str) -> bool:
        """Check if device can start streaming (detect bad state).
        
        From hdmi-usb.py - tests if device is in a usable state.
        """
        try:
            # Try a simple streaming test
            result = subprocess.run(
                ['v4l2-ctl', '-d', video_dev, '--stream-mmap', '--stream-count=1', '--stream-to=/dev/null'],
                capture_output=True,
                text=True,
                timeout=2
            )
            # If STREAMON fails, we'll get an error
            if 'STREAMON' in result.stderr and 'error' in result.stderr.lower():
                return False
            return True
        except Exception:
            return False

    def reset_device_state(self, video_dev: str) -> bool:
        """Validate that the v4l2 device is queryable and can stream.

        Note: USB-level reset is intentionally NOT performed here.  A USB reset
        de-asserts the HDMI Hot-Plug-Detect (HPD) signal briefly, which causes
        the HDMI source (e.g. Amlogic) to disable its transmitter — producing a
        black screen.  Instead, the app-owned capture pipeline keeps VIDIOC_STREAMON
        active continuously so HPD stays asserted.
        """
        try:
            result = subprocess.run(
                ['v4l2-ctl', '-d', video_dev, '--all'],
                capture_output=True,
                text=True,
                timeout=2
            )
            if result.returncode != 0:
                self.log(f"Warning: Cannot query device {video_dev}, may be in bad state")
                return False

            if not self.check_device_streaming(video_dev):
                print(f"❌ ERROR: Device {video_dev} is in a bad state (STREAMON fails)", file=sys.stderr)
                print("   This usually happens when a previous process didn't close the device properly.", file=sys.stderr)
                print("   Try one of these solutions:", file=sys.stderr)
                print("     1. Unplug and replug the USB device", file=sys.stderr)
                print("     2. Reset the USB device: sudo usb_modeswitch -v 0x534d -p 0x2109 -R", file=sys.stderr)
                print("     3. Reload the driver: sudo modprobe -r uvcvideo && sudo modprobe uvcvideo", file=sys.stderr)
                return False

            return True
        except Exception as e:
            self.log(f"Error checking device state: {e}")
            return False

    def _extract_usb_path_tail(self, device: str) -> Optional[str]:
        """Extract USB path tail for video device."""
        device_node = os.path.basename(device)
        sys_device_path = f"/sys/class/video4linux/{device_node}/device"

        if not os.path.exists(sys_device_path):
            return None

        try:
            real_path = os.path.realpath(sys_device_path)
            usb_path_matches = re.findall(r'\d+-[\d.]+', real_path)
            return usb_path_matches[-1] if usb_path_matches else None
        except Exception:
            return None

    def _find_alsa_card_by_usb_tail(self, usb_tail: str) -> Optional[str]:
        """Find ALSA card matching USB path tail."""
        sound_class_path = Path('/sys/class/sound')

        for card_path in sound_class_path.glob('card*'):
            if not card_path.is_dir():
                continue

            card_device_path = card_path / 'device'
            if not card_device_path.exists():
                continue

            try:
                real_device_path = os.path.realpath(card_device_path)
                audio_usb_matches = re.findall(r'\d+-[\d.]+', real_device_path)
                if not audio_usb_matches:
                    continue

                # Match must be exact on the USB device path
                if audio_usb_matches[-1] == usb_tail:
                    card_number = card_path.name.replace('card', '')

                    # Verify this card has a capture device
                    asound_card_path = Path(f"/proc/asound/card{card_number}")
                    if any(asound_card_path.glob('pcm*c')):
                        return card_number

                    self.log(f"Warning: Found audio card {card_number} on same "
                            f"USB device, but it has no capture devices")
                    return None
            except Exception:
                continue

        return None

    def verify_audio_card(self, card_num: str) -> bool:
        """Verify audio card is valid and has capture capability."""
        card_id_path = Path(f"/proc/asound/card{card_num}/id")
        card_info = "unknown"

        if card_id_path.exists():
            try:
                card_info = card_id_path.read_text().strip()
                self.log(f"Audio card {card_num} ID: {card_info}")
            except Exception:
                pass

        # Verify the card has capture capability
        asound_path = Path(f"/proc/asound/card{card_num}")
        if not any(asound_path.glob('pcm*c')):
            return False

        # Check if the card is USB-based
        card_path = Path(f"/sys/class/sound/card{card_num}/device")
        if card_path.exists():
            try:
                device_path = os.path.realpath(card_path)
                if 'usb' in device_path:
                    self.log(f"Verified: Audio card {card_num} ({card_info}) "
                            f"is a USB device with capture capability")
                    return True
            except Exception:
                pass

        self.log(f"Warning: Could not verify audio card {card_num} "
                f"as a USB capture device")
        return True

    def pick_nodes_by_name(self) -> list:
        """Get list of potential video devices."""
        try:
            result = subprocess.run(
                ['v4l2-ctl', '--list-devices'],
                capture_output=True,
                text=True,
                timeout=SUBPROCESS_TIMEOUT_SECONDS
            )

            devices = []
            in_block = False

            for line in result.stdout.splitlines():
                if 'USB Video: USB Video' in line:
                    in_block = True
                    continue

                if in_block:
                    if not line.strip():
                        in_block = False
                        continue

                    match = re.search(r'/dev/video\d+', line)
                    if match:
                        devices.append(match.group(0))

            return devices
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError, FileNotFoundError):
            return []

    def detect_video_device(self) -> Optional[str]:
        """Detect video HDMI capture device with state validation."""
        for node in self.pick_nodes_by_name():
            if node and self.is_video_hdmi_usb(node):
                # Validate the device can stream
                if self.reset_device_state(node):
                    return node
                else:
                    self.log(f"Device {node} failed state validation, trying next device...")
        return None

    def detect_audio_card(self, video_device: str) -> Optional[str]:
        """Detect audio card for the video device."""
        if self.audio_force_card:
            self.log(f"Forcing ALSA card: {self.audio_force_card}")
            return (self.audio_force_card 
                    if self.verify_audio_card(self.audio_force_card) 
                    else None)

        usb_tail = self._extract_usb_path_tail(video_device)
        if not usb_tail:
            self.log("Could not resolve USB path tail. Running video-only.")
            return None

        self.log(f"USB path for video device: {usb_tail}")
        audio_card = self._find_alsa_card_by_usb_tail(usb_tail)

        if audio_card:
            self.log(f"Matched ALSA card by USB path: card {audio_card}")
            if self.verify_audio_card(audio_card):
                self.log("Audio verification passed - audio is from the "
                        "USB HDMI capture device")
                return audio_card
            return None

        self.log(f"No ALSA card matched USB path ({usb_tail}). "
                f"Running video-only.")
        return None


# =============================================================================
# Local Display Pipeline Management
# =============================================================================

class LocalDisplayPipeline:
    """Manages local display pipeline as an RTSP client.
    
    Connects to the RTSP server as a client to display the stream locally.
    This approach avoids device sharing complexity and allows the local view
    to work just like any other RTSP client.
    """

    def __init__(
        self,
        debug_mode: bool = False,
        server=None,
        force_width: Optional[int] = None,
        has_audio: bool = False,
        av_offset_ms: float = 0.0,
    ):
        self.debug_mode = debug_mode
        self.has_audio = has_audio
        self.pipeline = None
        self.server = server
        self.owner_pid = os.getpid()
        self.force_width = force_width
        self.av_offset_ms = av_offset_ms
        
        # Window state management (fixed path so it works when started from any cwd)
        self.window_state_file = get_window_state_path()
        self.restore_x = None
        self.restore_y = None
        self.restore_width = None
        self.restore_height = None
        self._restore_applied = False
        self._restore_attempts = 0
        self._force_applied = False
        self._force_attempts = 0

        # Window auto-save (GLib timer based; no background threads)
        self._window_watch_id = None
        self._window_watch_window_id = None
        self._window_watch_last_geometry = None
        self._window_watch_ignore_until = 0.0
        # Last geometry seen at 16:9; the baseline for deciding which side the
        # user dragged.
        self._window_watch_ratio_w = None
        self._window_watch_ratio_h = None
        self._window_watch_adjusting_until = 0.0

        # Window IDs we've already asked the WM/compositor to decorate.
        self._decorated_window_ids = set()

        # Register cleanup function for robust cleanup
        register_cleanup(self.stop)

    def log(self, message: str) -> None:
        """Print log message if debug mode is enabled."""
        if self.debug_mode:
            print(f"[LOCAL] {message}")

    def on_bus_message(self, bus, message):
        """Handle bus messages for local display pipeline."""
        msg_type = message.type

        if msg_type == Gst.MessageType.ERROR:
            err, debug_info = message.parse_error()
            error_msg = err.message

            # Check if window was closed / user requested quit (sink-specific).
            #
            # - ximagesink often reports: "Output window was closed"
            # - glimagesink reports: "Quit requested"
            #
            # On some systems/messages, the exact text can vary, and we still
            # want a window close (or any fatal local display error) to shut
            # down the whole application instead of leaving the RTSP server
            # running in the background with no UI.
            is_close_request = (
                "Output window was closed" in error_msg or
                "Quit requested" in error_msg or
                "quit requested" in error_msg.lower()
            )

            if is_close_request:
                print("🔴 Local display window closed, shutting down gracefully...")
            else:
                print(f"❌ Local Display ERROR: {error_msg}")
                if self.debug_mode:
                    print(f"   Debug: {debug_info}")

            # Treat any local-display ERROR as fatal: trigger graceful shutdown
            # via the main loop so the process terminates when the user closes
            # the window (or when a fatal sink error occurs).
            if self.server:
                GLib.idle_add(self.server.shutdown)
            else:
                GLib.idle_add(self.stop)
        elif msg_type == Gst.MessageType.WARNING and self.debug_mode:
            warn, _ = message.parse_warning()
            print(f"⚠️  Local Display WARNING: {warn.message}")
        elif msg_type == Gst.MessageType.EOS:
            self.log("End of stream reached")
            # EOS can also indicate window closure, trigger shutdown
            if self.server:
                print("🔴 Local display stream ended, shutting down gracefully...")
                GLib.idle_add(self.server.shutdown)
        elif msg_type == Gst.MessageType.STATE_CHANGED:
            if message.src == self.pipeline:
                old_state, new_state, pending = message.parse_state_changed()
                if self.debug_mode:
                    self.log(f"State changed: {old_state.value_nick} -> "
                            f"{new_state.value_nick}")

                # Only attempt window operations once we are actually PLAYING.
                # Before that, the sink window often doesn't exist yet.
                if new_state == Gst.State.PLAYING:
                    GLib.idle_add(self._on_pipeline_playing)

        return True

    def _on_pipeline_playing(self):
        """Called once the pipeline reaches PLAYING.

        This is the earliest reliable point where the sink window exists.
        """
        if getattr(self, "_playing_init_done", False):
            return False
        self._playing_init_done = True

        # If the user requested a fixed window width, force a 16:9 size and
        # ignore saved window geometry (do not restore or overwrite it).
        if self.force_width:
            target_w = _round_even(max(int(self.force_width), 2))
            target_h = _compute_height_for_16_9(target_w)
            self.log(f"Forcing local window size: {target_w}x{target_h} (16:9)")

            self._force_applied = self.apply_forced_window_size(target_w, target_h)
            self._force_attempts = 1

            def retry_force():
                if self._force_applied:
                    return False
                if self._force_attempts >= 3:
                    return False
                self._force_attempts += 1
                self.log(f"Retrying forced window size (attempt {self._force_attempts}/3)...")
                self._force_applied = self.apply_forced_window_size(target_w, target_h)
                return not self._force_applied and self._force_attempts < 3

            GLib.timeout_add_seconds(2, retry_force)

            # Keep enforcing 16:9 on later manual resizes (geometry is still
            # never saved while --width is in effect).
            self._start_window_watch()
            return False

        if (not self._restore_applied and
            all([self.restore_x, self.restore_y, self.restore_width, self.restore_height])):
            # restore_x/restore_y may already include a sign (e.g. "-36", "+47").
            self.log(
                f"Applying saved window geometry after PLAYING: "
                f"{self.restore_width}x{self.restore_height}{self.restore_x}{self.restore_y}"
            )
            self._restore_applied = self.apply_window_state()

            # If it didn't stick immediately, retry a few times; WMs often
            # re-tile/re-maximize shortly after PLAYING.
            self._restore_attempts = 1

            def retry_restore():
                if self._restore_applied:
                    return False
                if self._restore_attempts >= 3:
                    return False
                self._restore_attempts += 1
                self.log(f"Retrying window restore (attempt {self._restore_attempts}/3)...")
                self._restore_applied = self.apply_window_state()
                return not self._restore_applied and self._restore_attempts < 3

            GLib.timeout_add_seconds(2, retry_restore)

        # Start 16:9 enforcement and auto-saving of window geometry changes.
        self._start_window_watch()

        return False

    def restore_window_state(self):
        """Restore window state from file."""
        if self.force_width:
            self.log("Ignoring saved window state due to --width override")
            return
        path = self.window_state_file
        if not path.exists():
            # One-time migration from legacy path
            legacy = Path.home() / '.hdmi-rtsp-unified-window-state'
            if legacy.exists():
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(legacy.read_text())
                    legacy.unlink()
                except OSError:
                    path = legacy
            else:
                self.log("No saved window state found")
                return
        try:
            geometry = path.read_text().strip()
            self.log(f"Restoring window state: {geometry}")
            
            # Parse geometry (format: WIDTHxHEIGHT+X+Y)
            match = re.match(r'^(\d+)x(\d+)([+-]-?\d+)([+-]-?\d+)$', geometry)
            if match:
                self.restore_width = match.group(1)
                self.restore_height = match.group(2)
                # State files written before the geometry reader was fixed may
                # hold doubled signs from xwininfo ("+-50", "--86"); both forms
                # mean a negative offset.
                offsets = []
                for group in (match.group(3), match.group(4)):
                    value = int(group.lstrip('+-'))
                    if '-' in group:
                        value = -value
                    offsets.append(f"{value:+d}")
                self.restore_x, self.restore_y = offsets

                # Enforce 16:9 on restore.
                #
                # Choose the adjustment that produces the smaller change from the
                # saved geometry: either keep width and adjust height, or keep
                # height and adjust width.
                try:
                    w = int(self.restore_width)
                    h = int(self.restore_height)
                    h_from_w = _compute_height_for_16_9(w)
                    w_from_h = _compute_width_for_16_9(h)

                    if abs(h_from_w - h) <= abs(w_from_h - w):
                        self.restore_height = str(h_from_w)
                    else:
                        self.restore_width = str(w_from_h)
                except Exception:
                    pass
                
                self.log(f"Will restore to: {self.restore_width}x{self.restore_height} "
                        f"at position {self.restore_x},{self.restore_y}")
            else:
                self.log(f"Invalid geometry format: {geometry}")
        except Exception as e:
            self.log(f"Failed to read window state: {e}")
    
    def _ensure_window_decorated(self, window_id: str) -> None:
        """Ask the WM/compositor to draw normal decorations on the sink window.

        ximagesink/xvimagesink/glimagesink open a bare Xlib window and never set
        _NET_WM_WINDOW_TYPE or Motif decoration hints. A WM/XWayland compositor
        that relies on those hints to decide whether to draw a title bar and
        border then leaves the window bare, which also makes it look like
        moving/resizing has no chrome to grab. Setting both hints once per
        window is enough to get normal borders back without touching the
        existing geometry logic below.
        """
        if window_id in self._decorated_window_ids:
            return
        self._decorated_window_ids.add(window_id)
        try:
            subprocess.run(
                ['xprop', '-id', window_id, '-f', '_NET_WM_WINDOW_TYPE', '32a',
                 '-set', '_NET_WM_WINDOW_TYPE', '_NET_WM_WINDOW_TYPE_NORMAL'],
                capture_output=True, text=True, timeout=1
            )
            # Motif hints: flags=MWM_HINTS_DECORATIONS(2), functions=0,
            # decorations=MWM_DECOR_ALL(1), input_mode=0, status=0.
            subprocess.run(
                ['xprop', '-id', window_id, '-f', '_MOTIF_WM_HINTS', '32c',
                 '-set', '_MOTIF_WM_HINTS', '0x2, 0x0, 0x1, 0x0, 0x0'],
                capture_output=True, text=True, timeout=1
            )
        except Exception as e:
            self.log(f"Could not set decoration hints on {window_id}: {e}")

    def get_window_id(self, timeout: float = 5.0) -> Optional[str]:
        """Get window ID for GStreamer window.

        When using Gst.parse_launch(), the window is named 'python3' with class 'GStreamer',
        not 'gst-launch-1.0' like when using the command-line tool.
        """
        start_time = time.time()
        
        while time.time() - start_time < timeout:
            try:
                # Method 0 (most reliable): score windows by PID, WM_CLASS and title.
                # `wmctrl -lp` reports the PID but its 4th column is the client
                # machine, not WM_CLASS; `wmctrl -lx` reports WM_CLASS but no PID.
                # Read both and merge them by window ID.
                try:
                    classes = {}
                    wmctrl_lx = subprocess.run(
                        ['wmctrl', '-lx'],
                        capture_output=True,
                        text=True,
                        timeout=1
                    )
                    if wmctrl_lx.returncode == 0:
                        for line in wmctrl_lx.stdout.splitlines():
                            parts = line.split(None, 4)
                            if len(parts) >= 3:
                                classes[parts[0].lower()] = parts[2]

                    wmctrl_lp = subprocess.run(
                        ['wmctrl', '-lp'],
                        capture_output=True,
                        text=True,
                        timeout=1
                    )
                    if wmctrl_lp.returncode == 0:
                        best_score = 0
                        best = None
                        for line in wmctrl_lp.stdout.splitlines():
                            parts = line.split(None, 4)
                            if len(parts) < 4:
                                continue
                            win_id, _desk, pid_str = parts[:3]
                            title = parts[4] if len(parts) >= 5 else ""
                            try:
                                pid = int(pid_str)
                            except ValueError:
                                continue
                            score = 0
                            wm_class_l = classes.get(win_id.lower(), "").lower()
                            title_l = title.lower()

                            # Prefer windows owned by this process, but don't require it:
                            # some sinks/window systems report a different PID (often 0).
                            if pid == self.owner_pid:
                                score += 3
                            elif pid == 0:
                                score += 1

                            if ('gstreamer' in wm_class_l or
                                'ximagesink' in wm_class_l or
                                'glimagesink' in wm_class_l):
                                score += 2
                            if ('gstreamer' in title_l or
                                'opengl' in title_l or
                                'python' in title_l):
                                score += 1
                            if score > best_score:
                                best_score = score
                                best = win_id
                        # Only accept a window we have real evidence for, and keep
                        # polling otherwise: the sink window appears a moment after
                        # PLAYING, and returning an unrelated window (a terminal, a
                        # browser) would move/resize it and pin the geometry
                        # auto-save to it, overwriting the saved state.
                        if best and best_score >= 2:
                            self.log(
                                f"Found window ID {best} (score {best_score}, "
                                f"owner pid {self.owner_pid})"
                            )
                            self._ensure_window_decorated(best)
                            return best
                except Exception:
                    # wmctrl may be missing; fall back to other methods below.
                    pass

                # Method 1: Look for window named "python3" (most common with Gst.parse_launch)
                result = subprocess.run(
                    ['xwininfo', '-name', 'python3'],
                    capture_output=True,
                    text=True,
                    timeout=1
                )
                
                if result.returncode == 0:
                    for line in result.stdout.splitlines():
                        if 'Window id:' in line:
                            parts = line.split()
                            if len(parts) >= 4:
                                window_id = parts[3]
                                self.log(f"Found window ID by name 'python3': {window_id}")
                                self._ensure_window_decorated(window_id)
                                return window_id
                
                # Method 2: Look for window with GStreamer class
                result2 = subprocess.run(
                    ['wmctrl', '-lx'],
                    capture_output=True,
                    text=True,
                    timeout=1
                )
                
                for line in result2.stdout.splitlines():
                    line_l = line.lower()
                    if ('gstreamer' in line_l or
                        'ximagesink' in line_l or
                        'glimagesink' in line_l or
                        'opengl' in line_l):
                        parts = line.split()
                        if len(parts) >= 1:
                            window_id = parts[0]
                            self.log(f"Found window ID by class: {window_id}")
                            self._ensure_window_decorated(window_id)
                            return window_id
                            
            except Exception as e:
                self.log(f"Error getting window ID: {e}")
            
            time.sleep(0.1)
        
        self.log(f"Window not found after {timeout} seconds")
        return None
    
    def get_window_geometry(self, window_id: str) -> Optional[str]:
        """Get window geometry as WIDTHxHEIGHT+X+Y.

        The offsets come from xwininfo's `-geometry` line, which is in the same
        coordinate space as `wmctrl -e`. For windows placed past the left or top
        edge that line carries doubled signs (`900x506+-50+-40`,
        `1400x540--28+200`); those are normalised here, otherwise the geometry
        parses nowhere and gets saved to the state file unreadable.
        """
        try:
            result = subprocess.run(
                ['xwininfo', '-id', window_id],
                capture_output=True,
                text=True,
                timeout=1
            )

            for line in result.stdout.splitlines():
                if '-geometry' in line:
                    parts = line.split()
                    if len(parts) < 2:
                        continue
                    match = re.match(r'^(\d+)x(\d+)([+-]-?\d+)([+-]-?\d+)$', parts[1])
                    if not match:
                        return parts[1]
                    offsets = []
                    for group in (match.group(3), match.group(4)):
                        value = int(group.lstrip('+-'))
                        if '-' in group:
                            value = -value
                        offsets.append(f"{value:+d}")
                    return f"{match.group(1)}x{match.group(2)}{offsets[0]}{offsets[1]}"
        except Exception:
            pass

        return None
    
    def _apply_window_state_to_window(self, window_id: str) -> bool:
        """Apply the saved window geometry to a specific window ID.

        Returns True if the geometry appears to have been applied.
        """
        # Check if wmctrl is available
        try:
            subprocess.run(['which', 'wmctrl'], capture_output=True,
                           check=True, timeout=1)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            self.log("wmctrl not available, window position not restored")
            return False

        try:
            import time

            target_x = int(self.restore_x)
            target_y = int(self.restore_y)
            target_w = int(self.restore_width)
            target_h = int(self.restore_height)
            # Some window managers behave poorly with negative positions.
            # Clamp to 0 so at least size restore is reliable.
            apply_x = target_x if target_x >= 0 else 0
            apply_y = target_y if target_y >= 0 else 0

            def _clear_wm_state() -> None:
                # If the WM creates the window maximized/fullscreen, -e may be ignored.
                # Clear those states first (and repeatedly, some WMs re-apply them).
                # Some WMs ignore a combined remove list; do it one-by-one.
                for state in ("fullscreen", "maximized_vert", "maximized_horz"):
                    subprocess.run(
                        ['wmctrl', '-i', '-r', window_id, '-b', f'remove,{state}'],
                        capture_output=True,
                        text=True,
                        timeout=1
                    )

            def _clear_size_hints() -> None:
                # Some sinks set WM_NORMAL_HINTS that effectively clamp the window size
                # (e.g., minimum width ~= negotiated video width). Removing these hints
                # lets WMs apply the requested geometry.
                try:
                    subprocess.run(
                        ['xprop', '-id', window_id, '-remove', 'WM_NORMAL_HINTS'],
                        capture_output=True,
                        text=True,
                        timeout=1
                    )
                except Exception:
                    pass

            def _apply_geometry() -> subprocess.CompletedProcess:
                return subprocess.run(
                    ['wmctrl', '-i', '-r', window_id, '-e',
                     f"0,{apply_x},{apply_y},{target_w},{target_h}"],
                    capture_output=True,
                    text=True,
                    timeout=1
                )

            self.log(f"Applying window geometry to {window_id}...")

            def _geometry_matches(geometry: Optional[str]) -> bool:
                if not geometry:
                    return False
                match = re.match(r'^(\d+)x(\d+)([+-]\d+)([+-]\d+)$', geometry)
                if not match:
                    return False
                current_w = int(match.group(1))
                current_h = int(match.group(2))
                current_x = int(match.group(3))
                current_y = int(match.group(4))
                return (
                    abs(current_x - apply_x) < 10 and
                    abs(current_y - apply_y) < 10 and
                    abs(current_w - target_w) < 10 and
                    abs(current_h - target_h) < 10
                )

            # Fast path: apply once and poll briefly. This avoids a race where callers
            # (e.g., integration tests) read window geometry immediately after PLAYING.
            try:
                _clear_wm_state()
                _clear_size_hints()
                _apply_geometry()
                fast_deadline = time.monotonic() + 1.5
                while time.monotonic() < fast_deadline:
                    current_geometry = self.get_window_geometry(window_id)
                    if _geometry_matches(current_geometry):
                        self.log(
                            f"Window geometry applied: {target_w}x{target_h} "
                            f"at {apply_x},{apply_y} (current={current_geometry})"
                        )
                        return True
                    time.sleep(0.05)
            except Exception:
                pass

            # Some window managers will re-apply maximize/tile state shortly
            # after mapping. Give it more time to settle.
            deadline = time.time() + 20.0
            last_geometry = None

            # Pre-shrink: some window managers won't release horizontal maximize/tile
            # unless the window first becomes clearly "non-maximized".
            try:
                pre_w = min(target_w, 640)
                pre_h = min(target_h, 360)
                if pre_w != target_w or pre_h != target_h:
                    _clear_wm_state()
                    _clear_size_hints()
                    subprocess.run(
                        ['wmctrl', '-i', '-r', window_id, '-e',
                         f"0,{apply_x},{apply_y},{pre_w},{pre_h}"],
                        capture_output=True,
                        text=True,
                        timeout=1
                    )
                    time.sleep(0.10)
            except Exception:
                pass

            while time.time() < deadline:
                _clear_wm_state()
                _clear_size_hints()
                time.sleep(0.05)

                result = _apply_geometry()
                if result.returncode != 0 and self.debug_mode:
                    self.log(f"wmctrl -e failed: {result.stderr.strip()}")

                time.sleep(0.15)
                current_geometry = self.get_window_geometry(window_id)
                if current_geometry:
                    last_geometry = current_geometry
                    if _geometry_matches(current_geometry):
                        self.log(
                            f"Window geometry applied: {target_w}x{target_h} "
                            f"at {apply_x},{apply_y} (current={current_geometry})"
                        )
                        return True

            if last_geometry:
                self.log(
                    f"Window geometry did not settle to saved state; last seen: {last_geometry}"
                )
                if self.debug_mode:
                    try:
                        state_line = subprocess.run(
                            ['xprop', '-id', window_id, '_NET_WM_STATE'],
                            capture_output=True,
                            text=True,
                            timeout=1
                        ).stdout.strip()
                        if state_line:
                            self.log(f"Window state: {state_line}")
                    except Exception:
                        pass
                    try:
                        hints = subprocess.run(
                            ['xprop', '-id', window_id, 'WM_NORMAL_HINTS'],
                            capture_output=True,
                            text=True,
                            timeout=1
                        ).stdout.strip()
                        if hints:
                            self.log(f"Window hints: {hints}")
                    except Exception:
                        pass
                    try:
                        info = subprocess.run(
                            ['xwininfo', '-id', window_id, '-wm'],
                            capture_output=True,
                            text=True,
                            timeout=2
                        ).stdout
                        for line in info.splitlines():
                            if 'Minimum Size' in line or 'Maximum Size' in line:
                                self.log(line.strip())
                    except Exception:
                        pass
            return False
        except Exception as e:
            self.log(f"Failed to apply window state: {e}")
            return False

    def _apply_window_size_to_window(self, window_id: str, width: int, height: int) -> bool:
        """Resize the window to (width, height) while keeping the current position."""
        # Check if wmctrl is available
        try:
            subprocess.run(['which', 'wmctrl'], capture_output=True,
                           check=True, timeout=1)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            self.log("wmctrl not available, window size not applied")
            return False

        try:
            import time

            # Keep current position if we can read it, otherwise default to 0,0.
            current_geometry = self.get_window_geometry(window_id)
            cur_x, cur_y = 0, 0
            if current_geometry:
                match = re.match(r'^(\d+)x(\d+)([+-]\d+)([+-]\d+)$', current_geometry)
                if match:
                    cur_x, cur_y = int(match.group(3)), int(match.group(4))

            target_w = _round_even(max(int(width), 2))
            target_h = _round_even(max(int(height), 2))

            def _clear_wm_state() -> None:
                # Some WMs ignore a combined remove list; do it one-by-one.
                for state in ("fullscreen", "maximized_vert", "maximized_horz"):
                    subprocess.run(
                        ['wmctrl', '-i', '-r', window_id, '-b', f'remove,{state}'],
                        capture_output=True,
                        text=True,
                        timeout=1
                    )

            def _clear_size_hints() -> None:
                # Some sinks set WM_NORMAL_HINTS that effectively clamp the window size
                # (e.g., minimum width ~= negotiated video width). Removing these hints
                # lets WMs apply the requested geometry.
                try:
                    subprocess.run(
                        ['xprop', '-id', window_id, '-remove', 'WM_NORMAL_HINTS'],
                        capture_output=True,
                        text=True,
                        timeout=1
                    )
                except Exception:
                    pass

            def _apply_geometry() -> subprocess.CompletedProcess:
                return subprocess.run(
                    ['wmctrl', '-i', '-r', window_id, '-e',
                     f"0,{cur_x},{cur_y},{target_w},{target_h}"],
                    capture_output=True,
                    text=True,
                    timeout=1
                )

            self.log(f"Applying forced window size to {window_id}...")

            def _size_matches(geometry: Optional[str]) -> bool:
                if not geometry:
                    return False
                match = re.match(r'^(\d+)x(\d+)([+-]\d+)([+-]\d+)$', geometry)
                if not match:
                    return False
                current_w = int(match.group(1))
                current_h = int(match.group(2))
                return abs(current_w - target_w) < 10 and abs(current_h - target_h) < 10

            # Fast path: apply once and poll briefly.
            try:
                _clear_wm_state()
                _clear_size_hints()
                _apply_geometry()
                fast_deadline = time.monotonic() + 1.5
                while time.monotonic() < fast_deadline:
                    current_geometry = self.get_window_geometry(window_id)
                    if _size_matches(current_geometry):
                        self.log(
                            f"Forced window size applied: {target_w}x{target_h} "
                            f"(current={current_geometry})"
                        )
                        print(f"[{timestamp()}] 🪟 Local window geometry: {current_geometry}")
                        return True
                    time.sleep(0.05)
            except Exception:
                pass

            deadline = time.time() + 20.0
            last_geometry = None
            while time.time() < deadline:
                _clear_wm_state()
                _clear_size_hints()
                time.sleep(0.05)

                result = _apply_geometry()
                if result.returncode != 0 and self.debug_mode:
                    self.log(f"wmctrl -e failed: {result.stderr.strip()}")

                time.sleep(0.15)
                current_geometry = self.get_window_geometry(window_id)
                if current_geometry:
                    last_geometry = current_geometry
                    if _size_matches(current_geometry):
                        self.log(f"Forced window size applied: {target_w}x{target_h} (current={current_geometry})")
                        print(f"[{timestamp()}] 🪟 Local window geometry: {current_geometry}")
                        return True

            if last_geometry:
                self.log(f"Forced window size did not settle; last seen: {last_geometry}")
                print(f"[{timestamp()}] 🪟 Local window geometry (last seen): {last_geometry}")
                if self.debug_mode:
                    try:
                        state_line = subprocess.run(
                            ['xprop', '-id', window_id, '_NET_WM_STATE'],
                            capture_output=True,
                            text=True,
                            timeout=1
                        ).stdout.strip()
                        if state_line:
                            self.log(f"Window state: {state_line}")
                    except Exception:
                        pass
                    try:
                        hints = subprocess.run(
                            ['xprop', '-id', window_id, 'WM_NORMAL_HINTS'],
                            capture_output=True,
                            text=True,
                            timeout=1
                        ).stdout.strip()
                        if hints:
                            self.log(f"Window hints: {hints}")
                    except Exception:
                        pass
                    try:
                        info = subprocess.run(
                            ['xwininfo', '-id', window_id, '-wm'],
                            capture_output=True,
                            text=True,
                            timeout=2
                        ).stdout
                        for line in info.splitlines():
                            if 'Minimum Size' in line or 'Maximum Size' in line:
                                self.log(line.strip())
                    except Exception:
                        pass
            return False
        except Exception as e:
            self.log(f"Failed to apply forced window size: {e}")
            return False

    def apply_window_state(self) -> bool:
        """Apply window state after GStreamer starts."""
        if not all([self.restore_x, self.restore_y, self.restore_width, 
                   self.restore_height]):
            return False

        # The window can take a few seconds to appear after the pipeline is set
        # to PLAYING (especially when using `playbin`). Be patient and retry.
        window_id = self.get_window_id(timeout=12.0)
        
        if not window_id:
            self.log("Window not found after waiting, position not restored")
            return False

        # Keep the window watch (auto-save / 16:9 enforcement) pinned to the same
        # window we just found for restore, to avoid mismatches when multiple
        # candidate windows exist.
        self._window_watch_window_id = window_id

        return self._apply_window_state_to_window(window_id)

    def apply_forced_window_size(self, width: int, height: int) -> bool:
        """Apply a forced window size after GStreamer starts."""
        window_id = self.get_window_id(timeout=12.0)
        if not window_id:
            self.log("Window not found after waiting, forced size not applied")
            return False

        # Keep the window watch pinned to this window so subsequent monitoring/
        # enforcement operates on the same target.
        self._window_watch_window_id = window_id
        applied = self._apply_window_size_to_window(window_id, width, height)
        # Always print the geometry we observe after the resize attempt.
        try:
            current_geometry = self.get_window_geometry(window_id)
            if current_geometry:
                print(f"[{timestamp()}] 🪟 Local window geometry (observed): {current_geometry}")
        except Exception:
            pass
        return applied

    def _start_window_watch(self) -> None:
        """Start a GLib timer that keeps the window at 16:9 and saves its geometry.

        With --width the size is imposed by the user, so geometry is never saved,
        but the aspect ratio is still enforced on later manual resizes.
        """
        # Avoid double-starting.
        if self._window_watch_id is not None:
            return

        # Ignore transient startup geometry (some WMs briefly report maximized/fullscreen).
        self._window_watch_ignore_until = time.time() + 5.0

        stable_ticks = [0]

        def _tick() -> bool:
            self._window_watch_id = None
            if not self.pipeline:
                return False

            delay_ms = 1000
            try:
                if not self._window_watch_window_id:
                    self._window_watch_window_id = self.get_window_id(timeout=0.2)
                    if not self._window_watch_window_id:
                        self._window_watch_id = GLib.timeout_add(delay_ms, _tick)
                        return False

                geometry = self.get_window_geometry(self._window_watch_window_id)
                if not geometry:
                    self._window_watch_id = GLib.timeout_add(delay_ms, _tick)
                    return False

                m = re.match(r'^(\d+)x(\d+)([+-]\d+)([+-]\d+)$', geometry)
                w = int(m.group(1)) if m else 0
                h = int(m.group(2)) if m else 0

                # Remember the last geometry that was already 16:9, including
                # during the startup grace period, so the very first manual
                # resize already has a baseline to compare against.
                if m and abs((w * 9) - (h * 16)) <= 32:  # ~2px tolerance
                    self._window_watch_ratio_w = w
                    self._window_watch_ratio_h = h

                if m and time.time() >= self._window_watch_ignore_until:
                    if time.time() >= self._window_watch_adjusting_until:
                        # Only enforce 16:9 once the geometry is stable across
                        # two ticks; enforcing on a freshly-read value races
                        # with concurrent external resizes (the read is ~1s
                        # stale by the time the resize is applied).
                        if (geometry == self._window_watch_last_geometry and
                                abs((w * 9) - (h * 16)) > 32):  # ~2px tolerance
                            # Adjust the side the user did not drag: a width
                            # change drives the height and vice versa. The
                            # comparison is against the last 16:9 geometry,
                            # because by the time this runs the previous tick
                            # already holds the new (off-ratio) size.
                            #
                            # When both sides changed (corner drag, maximize),
                            # keep the correction as small as possible.
                            smaller_change = (abs(_compute_height_for_16_9(w) - h) <=
                                              abs(_compute_width_for_16_9(h) - w))
                            if (self._window_watch_ratio_w is not None and
                                    self._window_watch_ratio_h is not None):
                                dw = abs(w - self._window_watch_ratio_w)
                                dh = abs(h - self._window_watch_ratio_h)
                                if dw > 2 and dh > 2:
                                    drive_width = smaller_change
                                else:
                                    drive_width = dw >= dh
                            else:
                                drive_width = smaller_change
                            if drive_width:
                                target_w, target_h = _round_even(w), _compute_height_for_16_9(w)
                            else:
                                target_h, target_w = _round_even(h), _compute_width_for_16_9(h)
                            if abs(target_w - w) >= 2 or abs(target_h - h) >= 2:
                                # Re-read right before applying: an external
                                # resize (user/wmctrl) may have landed since
                                # this tick's read, and applying a target
                                # computed from the stale value would stomp it.
                                fresh = self.get_window_geometry(self._window_watch_window_id)
                                if fresh != geometry:
                                    geometry = fresh or geometry
                                else:
                                    self.log(
                                        f"Enforcing 16:9: {target_w}x{target_h} (from {w}x{h}, "
                                        f"baseline {self._window_watch_ratio_w}x{self._window_watch_ratio_h}, "
                                        f"driving {'width' if drive_width else 'height'})"
                                    )
                                    self._window_watch_adjusting_until = time.time() + 2.0
                                    self._apply_window_size_to_window(self._window_watch_window_id, target_w, target_h)
                                    stable_ticks[0] = 0
                                    self._window_watch_id = GLib.timeout_add(1000, _tick)
                                    return False

                if geometry != self._window_watch_last_geometry:
                    self._window_watch_last_geometry = geometry
                    stable_ticks[0] = 0
                    if time.time() >= self._window_watch_ignore_until and not self.force_width:
                        try:
                            self.window_state_file.parent.mkdir(parents=True, exist_ok=True)
                            self.window_state_file.write_text(geometry)
                            self.log(f"Window geometry saved: {geometry}")
                        except OSError as e:
                            self.log(f"Could not save window state: {e}")
                else:
                    stable_ticks[0] += 1
                    if stable_ticks[0] >= 5:
                        delay_ms = 5000  # stable: back off subprocess polling
            except Exception as e:
                self.log(f"Window save error: {e}")

            self._window_watch_id = GLib.timeout_add(delay_ms, _tick)
            return False

        self._window_watch_id = GLib.timeout_add(1000, _tick)
    
    def _pick_audio_output_sink(self) -> str:
        """Return a GStreamer audio sink element string for local playback.

        pulsesink (used by PulseAudio and PipeWire's PulseAudio layer) needs
        explicit buffer-time / latency-time; without them the tiny default
        buffers cause underruns and silence.  autoaudiosink does not forward
        those properties to its child sink, so we prefer pulsesink directly.

        The buffer size sets the preview latency for video too: both local
        sinks sync to the clock, so the pipeline runs at the largest sink
        latency, which is this one.  60 ms is the smallest buffer that stayed
        underrun-free here; raise it toward 200 ms if audio crackles.

        provide-clock=false keeps pulsesink from becoming this pipeline's
        clock. Left at its default it would, so this pipeline's time base
        would be the playback device's hardware clock, unrelated to the
        capture pipeline's clock feeding it frames via intervideosrc/
        interaudiosrc. With no shared reference both pipelines free-run and
        drift apart. Falling back to GstSystemClock instead, which the
        capture pipeline also uses once alsasrc stops providing its own
        clock (see the capture pipeline's alsasrc), gives both pipelines the
        literal same clock object, since GstSystemClock is a process-wide
        singleton.
        """
        if Gst.ElementFactory.find("pulsesink"):
            self.log("Using pulsesink for local audio output")
            return "pulsesink buffer-time=60000 latency-time=20000 provide-clock=false"
        self.log("pulsesink not found, falling back to autoaudiosink")
        return "autoaudiosink"

    def build_pipeline(self):
        """Build local display pipeline consuming raw frames from the capture pipeline."""
        sink_name = next(
            (n for n in ("glimagesink", "xvimagesink", "ximagesink")
             if Gst.ElementFactory.find(n)),
            None,
        )
        if not sink_name:
            raise RuntimeError("No suitable video sink (need glimagesink, xvimagesink, or ximagesink)")
        self.log(f"Using local videosink: {sink_name}")

        # Both sinks must sync to the pipeline clock.  With sync=false the video
        # sink renders each frame on arrival and skips the ~420 ms of latency the
        # audio sink honours, so video ran a measured ~417 ms ahead of audio for
        # the whole session — roughly ten times the lip-sync threshold.
        # A synced sink enforces its processing deadline, so each branch needs a
        # queue to convert in: without one the sink warns "Pipeline construction
        # is invalid, please add queues." and shortens its own latency.
        #
        # Residual skew is trimmed with --av-offset.  Correct only by delaying
        # the early branch: a negative ts-offset asks the sink to render in the
        # past, which just makes every buffer late.  The remaining skew comes
        # from capture and playback latencies this code cannot query, so the
        # value has to be found by eye on the machine it runs on.
        video_offset, audio_offset = 0, 0
        if self.av_offset_ms and not self.has_audio:
            # Nothing to line up against, and delaying video alone would only
            # add latency.
            self.log("--av-offset ignored: no audio device in use")
        elif self.av_offset_ms > 0:
            audio_offset = int(self.av_offset_ms * Gst.MSECOND)
            self.log(f"Delaying local audio by {self.av_offset_ms:.0f} ms")
        elif self.av_offset_ms < 0:
            video_offset = int(-self.av_offset_ms * Gst.MSECOND)
            self.log(f"Delaying local video by {-self.av_offset_ms:.0f} ms")

        # The delayed branch needs a queue large enough to hold the offset.  A
        # default queue stops at 10 MB — about three 1080p frames — so the sink's
        # longer wait back-pressures intervideosrc, which timestamps from a frame
        # counter rather than from the clock.  Its PTS then slips back by exactly
        # the offset and cancels it: the picture delay saturated near 100 ms no
        # matter how large --av-offset was.  Sizing the queue by time, with the
        # byte and buffer caps off, keeps the source free-running so the offset
        # reaches the screen.  Holding the offset costs real memory — raw 1080p
        # runs about 78 MB per second — so the queue gets the offset plus enough
        # margin to absorb jitter and nothing more.  Below ~500 ms of margin the
        # source still slipped, and the offset landed short by a fixed ~90 ms.
        video_queue = audio_queue = 'queue'
        deep_queue = (
            'queue max-size-bytes=0 max-size-buffers=0 max-size-time={}'
        )
        if video_offset:
            video_queue = deep_queue.format(video_offset + 500 * Gst.MSECOND)
        elif audio_offset:
            audio_queue = deep_queue.format(audio_offset + 500 * Gst.MSECOND)

        video = (
            f'intervideosrc channel=hdmi-local-v ! {video_queue} ! '
            f'videoconvert ! videoscale ! '
            f'{sink_name} name=videosink force-aspect-ratio=false '
            f'ts-offset={video_offset}'
        )
        audio = (
            f' interaudiosrc channel=hdmi-local-a ! {audio_queue} ! '
            f'audioconvert ! audioresample ! {self._pick_audio_output_sink()} '
            f'ts-offset={audio_offset}'
        ) if self.has_audio else ''

        pipeline = Gst.parse_launch(video + audio)
        if not pipeline:
            raise RuntimeError("Failed to create local display pipeline")
        return pipeline

    def start(self) -> bool:
        """Start the local display pipeline."""
        self.restore_window_state()
        self._playing_init_done = False

        if self.debug_mode:
            print("[LOCAL] Building local display pipeline")

        try:
            self.pipeline = self.build_pipeline()
            if not self.pipeline:
                print("❌ ERROR: Failed to create local display pipeline")
                return False

            # Set up bus monitoring BEFORE starting pipeline
            bus = self.pipeline.get_bus()
            if bus:
                bus.add_signal_watch()
                bus.connect("message", self.on_bus_message)

            # Start playing
            ret = self.pipeline.set_state(Gst.State.PLAYING)
            if ret == Gst.StateChangeReturn.FAILURE:
                print("❌ ERROR: Unable to set local display pipeline to PLAYING")
                return False
            
            # Wait for state change to complete or for ASYNC result
            if ret == Gst.StateChangeReturn.ASYNC:
                ret, state, pending = self.pipeline.get_state(3 * Gst.SECOND)
                if ret == Gst.StateChangeReturn.FAILURE:
                    print("❌ ERROR: Pipeline failed to reach PLAYING state")
                    return False
                elif ret == Gst.StateChangeReturn.ASYNC:
                    print("⚠️  WARNING: Pipeline state change timed out, but continuing...")
                    self.log("Pipeline may still be initializing in background")
                else:
                    self.log(f"Pipeline state change completed: {state.value_nick}")
            elif ret == Gst.StateChangeReturn.SUCCESS:
                self.log("Pipeline started immediately")

            self.log("Local display pipeline started successfully")
            print(f"[{timestamp()}] 🖥️  Local display connected as RTSP client")

            return True

        except Exception as e:
            print(f"❌ ERROR: Failed to start local display: {e}")
            return False

    def stop(self):
        """Stop the local display pipeline."""
        # Prevent duplicate cleanup
        if not hasattr(self, '_cleanup_done'):
            self._cleanup_done = True
        else:
            return
        
        try:
            # Stop window watch timer
            try:
                if self._window_watch_id is not None:
                    GLib.source_remove(self._window_watch_id)
                    self._window_watch_id = None
            except Exception:
                pass

            if self.pipeline:
                self.log("Stopping local display pipeline")
                # Send EOS to gracefully stop the pipeline
                self.pipeline.send_event(Gst.Event.new_eos())
                
                # Wait for EOS to be processed
                time.sleep(0.5)
                
                # Set pipeline to NULL state
                self.pipeline.set_state(Gst.State.NULL)
                
                # Wait for state change to complete
                ret, state, pending = self.pipeline.get_state(2 * Gst.SECOND)
                if ret == Gst.StateChangeReturn.ASYNC:
                    self.log("Pipeline cleanup completed asynchronously")
                
                # Clean up bus
                bus = self.pipeline.get_bus()
                if bus:
                    bus.remove_signal_watch()
                
                # Clear pipeline reference
                self.pipeline = None
                
                # Give the device time to be released
                time.sleep(0.5)
        except Exception as e:
            print(f"⚠️  Error during local display cleanup: {e}")


# =============================================================================
# RTSP Media Factory and Server
# =============================================================================
class RTSPServer(GstRtspServer.RTSPServer):
    """RTSP Server for HDMI capture streaming."""

    def _pick_encoder(self) -> str:
        """Return the best available H.264 encoder pipeline fragment (videoconvert → encoder)."""
        if Gst.ElementFactory.find("vah264lpenc") and os.path.exists("/dev/dri/renderD128"):
            print(f"[{timestamp()}] ✅ Using hardware encoder: vah264lpenc")
            return (
                f'videoconvert ! video/x-raw,format=NV12 ! '
                f'vah264lpenc key-int-max={VIDEO_KEYFRAME_INTERVAL_FRAMES} '
                f'qpi=26 qpp=28 target-usage=6'
            )
        if Gst.ElementFactory.find("vah264enc") and os.path.exists("/dev/dri/renderD128"):
            print(f"[{timestamp()}] ✅ Using hardware encoder: vah264enc")
            return (
                f'videoconvert ! video/x-raw,format=NV12 ! '
                f'vah264enc key-int-max={VIDEO_KEYFRAME_INTERVAL_FRAMES} '
                f'qpi=26 qpp=28 target-usage=6'
            )
        return (
            f'videoconvert ! video/x-raw,format=I420 ! '
            f'x264enc tune=zerolatency key-int-max={VIDEO_KEYFRAME_INTERVAL_FRAMES} '
            f'bitrate={VIDEO_BITRATE_KBPS} speed-preset=veryfast byte-stream=true threads=1'
        )

    def _build_rtsp_launch_string(self, *, has_audio: bool) -> str:
        """Build the RTSP factory set_launch() string consuming inter* channels."""
        encoder = self._pick_encoder()
        video = (
            f'intervideosrc channel=hdmi-rtsp-v ! '
            f'{encoder} ! '
            f'h264parse config-interval=1 ! '
            f'video/x-h264,stream-format=avc,alignment=au ! '
            f'rtph264pay config-interval=1 pt=96 name=pay0'
        )
        if has_audio:
            audio = (
                f' interaudiosrc channel=hdmi-rtsp-a ! '
                f'audioconvert ! audioresample ! '
                f'audio/x-raw,format=S16LE,rate={AUDIO_SAMPLE_RATE_HZ},channels=2 ! '
                f'voaacenc bitrate={AUDIO_BITRATE_BPS} ! '
                f'rtpmp4gpay pt=97 name=pay1'
            )
            return video + audio
        return video

    def _start_capture_pipeline(
        self,
        video_device: str,
        audio_device_spec: Optional[str],
        use_mjpeg: bool,
        force_software_decode: bool = False,
    ) -> None:
        """Start the app-owned capture pipeline.

        Opens v4l2/ALSA exactly once, fans raw frames to inter* channels for the
        RTSP factory and local display, and keeps VIDIOC_STREAMON active so HDMI
        HPD stays asserted at all times (replacing the old keepalive RTSP client).

        Element presence does not guarantee a working VA-API driver, so a
        hardware-decode pipeline that fails to start — or that errors out during
        its probation window — is retried once with the software decoder via
        force_software_decode.
        """
        self._capture_args = (video_device, audio_device_spec, use_mjpeg)
        q = 'queue max-size-buffers=2 max-size-time=0 max-size-bytes=0 leaky=downstream'

        using_hw_decode = False
        if use_mjpeg:
            if (not force_software_decode
                    and Gst.ElementFactory.find("vajpegdec")
                    and Gst.ElementFactory.find("vapostproc")
                    and os.path.exists("/dev/dri/renderD128")):
                using_hw_decode = True
                # vajpegdec (GStreamer 1.24) only negotiates when the JPEG caps
                # carry explicit sof-marker/colorspace/sampling/interlace-mode
                # fields, which neither v4l2src nor jpegparse provide. The
                # decoder parses the real bitstream, so these values are
                # negotiation hints only — a sampling mismatch still decodes
                # correctly (verified empirically).
                #
                # vapostproc converts the decoder's native 4:2:2 output to I420
                # on the GPU before download; letting videoconvert do it on the
                # CPU costs more than software jpegdec. I420 also matches
                # jpegdec's output so downstream branches behave identically.
                print(f"[{timestamp()}] ✅ Using hardware JPEG decoder: vajpegdec")
                decoder = (
                    f'{q} ! image/jpeg,framerate={VIDEO_CAPTURE_FPS}/1,'
                    f'sof-marker=0,colorspace=sYUV,sampling=YCbCr-4:2:2,'
                    f'interlace-mode=progressive ! vajpegdec ! '
                    f'vapostproc ! video/x-raw,format=I420 ! '
                )
            else:
                decoder = (
                    f'{q} ! image/jpeg,framerate={VIDEO_CAPTURE_FPS}/1 ! jpegdec ! '
                )
        else:
            decoder = f'{q} ! decodebin ! '

        video = (
            f'v4l2src device={video_device} do-timestamp=true ! '
            f'{decoder}'
            f'videoconvert ! tee name=vtee '
            f'vtee. ! {q} ! intervideosink channel=hdmi-rtsp-v '
            f'vtee. ! {q} ! intervideosink channel=hdmi-local-v'
        )

        if audio_device_spec:
            device_q = audio_device_spec.replace('"', '\\"')
            # provide-clock=false keeps this alsasrc from becoming the capture
            # pipeline's clock, which is its default as the only live source
            # able to provide one. Left as the clock, the whole pipeline
            # would run on the MS2109's audio ADC crystal, which is about
            # 44 ppm off real time (measured on this hardware, ~0.16 s/hour).
            # v4l2src has no clock of its own (do-timestamp=true stamps
            # frames from the pipeline clock at arrival), so falling back to
            # GstSystemClock puts video, audio, and the local-display
            # pipeline (see pulsesink) all on the same accurate time base.
            # slave-method=resample then corrects the real ADC drift by
            # gently resampling the audio to that clock, instead of the
            # default "skew" method, which just re-timestamps and produces
            # periodic jumps.
            audio = (
                f' alsasrc device="{device_q}" '
                f'buffer-time=50000 latency-time=10000 '
                f'provide-clock=false slave-method=resample ! '
                f'queue max-size-time=1000000000 ! audioconvert ! audioresample ! tee name=atee '
                f'atee. ! queue ! interaudiosink channel=hdmi-rtsp-a '
                f'atee. ! queue ! interaudiosink channel=hdmi-local-a'
            )
        else:
            audio = ''

        pipeline_str = video + audio
        if self.debug_mode:
            print(f"[INFO] Capture pipeline: {pipeline_str}")

        pipeline = Gst.parse_launch(pipeline_str)
        if not pipeline:
            raise RuntimeError("Failed to create capture pipeline")

        bus = pipeline.get_bus()
        if bus:
            bus.add_signal_watch()
            bus.connect("message", self._on_capture_bus_message)

        failure = None
        ret = pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            failure = "Capture pipeline failed to start"
        elif ret == Gst.StateChangeReturn.ASYNC:
            ret, _, _ = pipeline.get_state(3 * Gst.SECOND)
            if ret == Gst.StateChangeReturn.FAILURE:
                failure = "Capture pipeline failed to reach PLAYING state"

        if failure:
            # The state-change return says nothing about the cause; the real
            # reason is the queued bus error (the main loop is not running yet,
            # so the signal watch has not consumed it).
            err = None
            if bus:
                error_msg = bus.timed_pop_filtered(0, Gst.MessageType.ERROR)
                if error_msg:
                    err, _ = error_msg.parse_error()
                    failure = f"{failure}: {err.message}"
            self._teardown_capture_pipeline(pipeline)
            if not using_hw_decode or self._is_device_error(err):
                raise RuntimeError(failure)
            print(f"[{timestamp()}] ⚠️  {failure} with hardware decode, "
                  f"retrying with software jpegdec")
            self._start_capture_pipeline(
                video_device=video_device,
                audio_device_spec=audio_device_spec,
                use_mjpeg=use_mjpeg,
                force_software_decode=True,
            )
            return

        self._capture_pipeline = pipeline
        # A broken VA-API driver can also fail asynchronously, after PLAYING.
        # Stay open to one software retry for a short probation window; past it,
        # errors are real capture failures and terminate the app as usual.
        self._hw_decode_on_probation = using_hw_decode
        if using_hw_decode:
            GLib.timeout_add_seconds(
                HW_DECODE_PROBATION_SECONDS, self._end_hw_decode_probation
            )
        print(f"[{timestamp()}] ✅ Capture pipeline started (HDMI HPD asserted)")

    def _teardown_capture_pipeline(self, pipeline) -> None:
        """Detach the bus watch and return a capture pipeline to NULL."""
        try:
            bus = pipeline.get_bus()
            if bus:
                bus.remove_signal_watch()
            pipeline.set_state(Gst.State.NULL)
        except Exception:
            pass

    def _is_device_error(self, err) -> bool:
        """True if err is a capture-device problem (busy, missing, unopenable).

        Such a failure has nothing to do with the decoder, so downgrading to
        software would fail identically — report the real error instead.
        """
        if err is None:
            return False
        domain = Gst.ResourceError.quark()
        return any(err.matches(domain, code) for code in (
            Gst.ResourceError.BUSY,
            Gst.ResourceError.OPEN_READ,
            Gst.ResourceError.OPEN_READ_WRITE,
            Gst.ResourceError.NOT_FOUND,
        ))

    def _end_hw_decode_probation(self) -> bool:
        """Stop treating capture errors as hardware-decode failures."""
        self._hw_decode_on_probation = False
        return False

    def _on_capture_bus_message(self, bus, message) -> bool:
        """Handle capture pipeline messages, downgrading to software decode
        when hardware decode fails inside its probation window."""
        if (message.type == Gst.MessageType.ERROR
                and getattr(self, '_hw_decode_on_probation', False)):
            err, _ = message.parse_error()
            if not self._is_device_error(err):
                self._hw_decode_on_probation = False
                print(f"[{timestamp()}] ⚠️  Hardware decode failed ({err.message}), "
                      f"retrying with software jpegdec")
                GLib.idle_add(self._restart_capture_in_software)
                return True

        return self._on_media_bus_message(bus, message)

    def _restart_capture_in_software(self) -> bool:
        """Replace the hardware-decode capture pipeline with a software one."""
        if self._capture_pipeline:
            self._teardown_capture_pipeline(self._capture_pipeline)
            self._capture_pipeline = None

        video_device, audio_device_spec, use_mjpeg = self._capture_args
        try:
            self._start_capture_pipeline(
                video_device=video_device,
                audio_device_spec=audio_device_spec,
                use_mjpeg=use_mjpeg,
                force_software_decode=True,
            )
        except Exception as e:
            self.on_pipeline_error(f"Software decode retry failed: {e}")
        return False

    def _on_media_configure(self, _factory, media) -> None:
        """Attach bus monitoring to each created media pipeline."""
        try:
            element = media.get_element()
        except Exception:
            element = None

        if not element:
            return

        try:
            bus = element.get_bus()
        except Exception:
            bus = None

        if not bus:
            return

        try:
            bus.add_signal_watch()
            bus.connect("message", self._on_media_bus_message)
        except Exception:
            # Best-effort; don't crash server for monitoring issues.
            return

    def _on_media_bus_message(self, _bus, message) -> bool:
        """Monitor bus messages for errors and warnings. Any pipeline error
        terminates the application."""
        msg_type = message.type

        if msg_type == Gst.MessageType.ERROR:
            err, debug_info = message.parse_error()
            error_msg = err.message
            if debug_info:
                error_msg = f"{error_msg} ({debug_info})"
            print(f"❌ GStreamer video capture pipeline ERROR: {error_msg}")
            self.on_pipeline_error(error_msg)

        elif msg_type == Gst.MessageType.WARNING and self.debug_mode:
            warn, _ = message.parse_warning()
            print(f"⚠️  Pipeline WARNING: {warn.message}")

        return True

    def test_audio_device_spec_availability(self, device_spec: str) -> bool:
        """Test if an ALSA capture device is available for RTSP streaming."""
        try:
            result = subprocess.run(
                ['arecord', '-D', device_spec, '-f', 'cd', '-d', '1', '/dev/null'],
                capture_output=True, text=True, timeout=3
            )
            return result.returncode == 0
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError, FileNotFoundError):
            return False

    def _pick_audio_device_spec(self, audio_card: str) -> Optional[str]:
        """Pick a good ALSA device string for capture.

        Prefer `dsnoop` (shareable) to avoid "Device or resource busy" when
        multiple RTSP sessions or other apps open the device. Fall back to
        plughw if dsnoop isn't available.
        """
        candidates = [
            f"dsnoop:CARD={audio_card},DEV=0",
            f"plughw:{audio_card},0",
        ]
        for spec in candidates:
            if self.test_audio_device_spec_availability(spec):
                return spec
        return None

    def __init__(self, debug_mode=False, headless=False, viewer_width: Optional[int] = None,
                 av_offset_ms: float = 0.0):
        super().__init__()
        self.port = DEFAULT_RTSP_PORT
        self.endpoint = DEFAULT_RTSP_ENDPOINT
        self.debug_mode = debug_mode
        self.headless = headless
        self.main_loop = None
        self.pipeline_errors = 0
        self.local_display = None
        self.viewer_width = viewer_width
        self.av_offset_ms = av_offset_ms
        self.audio_device_spec: Optional[str] = None
        self.set_address("0.0.0.0")
        self.set_service(self.port)
        self._capture_pipeline = None
        self._capture_args = None
        self._hw_decode_on_probation = False
        register_cleanup(self.shutdown)

        # Detect HDMI devices with enhanced validation
        self.detector = HDMIDeviceDetector(debug_mode=debug_mode)
        video_device = self.detector.detect_video_device()
        audio_card = None

        if not video_device:
            raise RuntimeError(
                "Could not find a MacroSilicon USB Video HDMI capture device"
            )

        if video_device:
            audio_card = self.detector.detect_audio_card(video_device)
            print(f"[{timestamp()}] ✅ Found video device: {video_device}")
            if audio_card:
                print(f"[{timestamp()}] ✅ Found audio card: {audio_card}")
                # Pick a capture device spec and verify availability.
                self.audio_device_spec = self._pick_audio_device_spec(audio_card)
                if not self.audio_device_spec:
                    print(f"[{timestamp()}] ⚠️  Audio device busy - using video-only mode")
                    audio_card = None
                else:
                    print(f"[{timestamp()}] ✅ Audio device available for streaming ({self.audio_device_spec})")
            else:
                print(f"[{timestamp()}] ⚠️  No audio device found - video only")

        use_local_display = not self.headless and video_device

        use_mjpeg = False
        if video_device:
            try:
                result = subprocess.run(
                    ['v4l2-ctl', '-d', video_device, '--list-formats-ext'],
                    capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT_SECONDS,
                )
                use_mjpeg = ('MJPG' in result.stdout) or ('MJPEG' in result.stdout)
            except Exception:
                use_mjpeg = True

        # Start the app-owned capture pipeline; fans raw frames via inter* channels
        # and keeps VIDIOC_STREAMON / HDMI HPD asserted for the server's lifetime.
        self._start_capture_pipeline(
            video_device=video_device,
            audio_device_spec=self.audio_device_spec if audio_card else None,
            use_mjpeg=use_mjpeg,
        )

        # RTSP factory reads from intervideosrc/interaudiosrc (not v4l2/alsa directly)
        # so multiple RTSP clients never contend on the device.
        self.factory = GstRtspServer.RTSPMediaFactory()
        self.factory.set_shared(True)
        if hasattr(self.factory, "set_reusable"):
            self.factory.set_reusable(True)

        launch = self._build_rtsp_launch_string(has_audio=bool(audio_card))
        self.factory.set_launch(launch)
        self.factory.connect("media-configure", self._on_media_configure)

        self.factory.set_eos_shutdown(False)
        self.factory.set_stop_on_disconnect(False)
        self.factory.set_transport_mode(GstRtspServer.RTSPTransportMode.PLAY)
        self.factory.set_latency(RTSP_LATENCY_MS)

        # Mount and attach server
        mount_points = self.get_mount_points()
        mount_points.add_factory(self.endpoint, self.factory)
        attach_id = self.attach(None)
        if attach_id == 0:
            raise RuntimeError(
                f"Failed to attach RTSP server to port {self.port}. "
                f"Port may be in use or permission denied."
            )
        self.connect("client-connected", self.on_client_connected)

        # Print server status
        mode_info = (
            "VIDEO+AUDIO 🎥🎵" if audio_card else
            "VIDEO-ONLY 🎥"
        )
        print(f"[{timestamp()}] 🚀 RTSP server is running at "
              f"rtsp://0.0.0.0:{self.port}{self.endpoint}")
        print(f"[{timestamp()}] 📡 Streaming mode: {mode_info}")
        if self.headless:
            print(f"[{timestamp()}] 🚫 Headless mode: local display disabled")
        
        if use_local_display:
            print(f"[{timestamp()}] 🖥️  Starting local display...")
            self.local_display = LocalDisplayPipeline(
                debug_mode=debug_mode,
                server=self,
                force_width=self.viewer_width,
                has_audio=bool(audio_card),
                av_offset_ms=self.av_offset_ms,
            )
            if not self.local_display.start():
                print(f"[{timestamp()}] ⚠️  Local display failed to start, continuing with RTSP server only")
                self.local_display = None

    def on_client_connected(self, server, client):
        """Handle client connection."""
        ip = client.get_connection().get_ip()
        print(f"[{timestamp()}] 📡 Client connected from {ip}")
        client.connect("closed", self.on_client_disconnected)

    def on_client_disconnected(self, client):
        """Handle client disconnection."""
        ip = client.get_connection().get_ip()
        print(f"[{timestamp()}] ❌ Client disconnected: {ip}")

    def on_pipeline_error(self, error_msg: str):
        """Handle video capture pipeline errors by terminating the application."""
        self.pipeline_errors += 1
        print(f"❌ Video capture pipeline error: {error_msg}")
        print(f"[{timestamp()}] 💥 Shutting down due to pipeline failure")

        if self.main_loop:
            GLib.idle_add(self.main_loop.quit)

    def set_main_loop(self, loop):
        """Set the main loop reference for error handling."""
        self.main_loop = loop

    def shutdown(self):
        """Shutdown server and clean up resources."""
        # Prevent duplicate cleanup
        if not hasattr(self, '_shutdown_done'):
            self._shutdown_done = True
        else:
            return
        
        try:
            if self.local_display:
                print(f"[{timestamp()}] 🖥️  Stopping local display...")
                self.local_display.stop()
                self.local_display = None

            if getattr(self, '_capture_pipeline', None):
                try:
                    bus = self._capture_pipeline.get_bus()
                    if bus:
                        bus.remove_signal_watch()
                    self._capture_pipeline.set_state(Gst.State.NULL)
                except Exception:
                    pass
                self._capture_pipeline = None

            if self.main_loop:
                GLib.idle_add(self.main_loop.quit)
        except Exception as e:
            print(f"⚠️  Error during server shutdown: {e}")


# =============================================================================
# Main Application Entry Point
# =============================================================================

def main():
    """Main entry point for the unified RTSP server."""
    parser = argparse.ArgumentParser(
        description='Unified HDMI USB Capture RTSP Server',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
DESCRIPTION:
    Automatically detects MacroSilicon USB Video HDMI capture devices and
    streams live video/audio over RTSP. The server will auto-detect both
    video and audio devices from the same USB HDMI capture adapter.
    
    Enhanced features from hdmi-usb.py:
    - Device state validation and automatic recovery
    - Instance management (kills existing instances)
    - Enhanced device validation with better error handling
    
    By default, displays a local preview window showing the captured audio
    and video. The window position and size are automatically saved and
    restored between sessions. The device is opened once in an app-owned
    capture pipeline which fans raw frames to the local display and RTSP
    clients via inter* channels. Use --headless to disable the local display.

    Default RTSP URL: rtsp://0.0.0.0:1234/hdmi

EXAMPLES:
    %(prog)s                     # Stream with local display (default)
    %(prog)s --headless          # Stream without local display window
    %(prog)s --debug             # Enable debug output
    %(prog)s --reset-window      # Reset saved window position
    AUDIO_FORCE_CARD=1 %(prog)s  # Force specific audio card

    # Connect with ffplay (recommended)
    ffplay -rtsp_transport tcp rtsp://127.0.0.1:1234/hdmi

    # Connect with GStreamer
    gst-launch-1.0 rtspsrc location=rtsp://127.0.0.1:1234/hdmi ! decodebin ! autovideosink

ENVIRONMENT VARIABLES:
    AUDIO_FORCE_CARD    Force specific ALSA audio card (e.g., AUDIO_FORCE_CARD=1)

COMPATIBILITY:
    ✅ Works with: ffplay, GStreamer, most RTSP clients
    ⚠️  Known issues: VLC may have compatibility issues with RTSP SETUP requests
                     (use ffplay or other RTSP clients instead)
    '''
    )
    parser.add_argument(
        '--headless',
        action='store_true',
        help='Disable local display window (RTSP server only)'
    )
    parser.add_argument(
        '--reset-window',
        action='store_true',
        help='Reset saved window position and size'
    )
    parser.add_argument(
        '--width',
        type=int,
        default=None,
        help='Force local viewer window width (16:9); ignores saved geometry'
    )
    parser.add_argument(
        '--av-offset',
        type=float,
        default=0.0,
        metavar='MS',
        help='Trim local preview lip-sync, in milliseconds. Negative delays '
             'video (use when video runs ahead of sound), positive delays '
             'audio. Find the value by eye; it depends on the capture and '
             'sound hardware'
    )
    parser.add_argument(
        '--debug',
        action='store_true',
        help='Enable debug output'
    )
    parser.add_argument(
        '--gst-debug',
        action='store_true',
        help='Enable GStreamer debug output (very verbose)'
    )
    args = parser.parse_args()
    
    # Handle reset-window option
    if args.reset_window:
        # Clear both the current XDG path and the legacy dotfile so a "reset"
        # doesn't get undone by the legacy-to-XDG migration on next launch.
        window_state_file = get_window_state_path()
        legacy_window_state_file = Path.home() / '.hdmi-rtsp-unified-window-state'

        cleared_paths = []
        for path in (window_state_file, legacy_window_state_file):
            try:
                if path.exists():
                    path.unlink()
                    cleared_paths.append(str(path))
            except OSError:
                # Best-effort; if we can't delete it, still continue.
                pass

        if cleared_paths:
            print("[INFO] Window state reset. Next launch will use default position.")
            if args.debug:
                print(f"[INFO] Cleared: {', '.join(cleared_paths)}")
        else:
            print("[INFO] No saved window state found.")
        return 0

    # Kill existing instances before starting (from hdmi-usb.py)
    script_name = os.path.basename(__file__)
    kill_existing_instances(script_name, debug_mode=args.debug)

    server = None
    try:
        if args.headless:
            print("\033[92m🎥🎵 Starting RTSP server in HEADLESS mode "
                  "(no local display)\033[0m")
        else:
            print("\033[92m🎥🎵 Starting unified RTSP server with local display "
                  "and HDMI capture\033[0m")

        server = RTSPServer(
            debug_mode=args.debug,
            headless=args.headless,
            viewer_width=args.width,
            av_offset_ms=args.av_offset,
        )
        loop = GLib.MainLoop()
        server.set_main_loop(loop)

        def _shutdown_and_quit() -> None:
            print(f"\n[{timestamp()}] 👋 Shutting down RTSP server gracefully...")
            try:
                server.shutdown()
            finally:
                loop.quit()

        # When the app is blocked in GLib.MainLoop().run(), Python-level signal
        # handlers (signal.signal) may not fire promptly because the interpreter
        # isn't regularly regaining control.
        #
        # Integrate SIGINT/SIGTERM with GLib so background runs stop cleanly.
        def _glib_shutdown_handler(*_args) -> bool:
            _shutdown_and_quit()
            return False  # GLib.SOURCE_REMOVE

        installed_glib_handlers = False
        try:
            unix_signal_add = getattr(GLib, "unix_signal_add", None)
            if unix_signal_add is not None:
                unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, _glib_shutdown_handler)
                unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, _glib_shutdown_handler)
                installed_glib_handlers = True
        except Exception:
            installed_glib_handlers = False

        if not installed_glib_handlers:
            # Fallback: best-effort Python signal handlers.
            def shutdown_handler(sig, frame):
                _shutdown_and_quit()

            signal.signal(signal.SIGINT, shutdown_handler)
            signal.signal(signal.SIGTERM, shutdown_handler)

        print(f"[{timestamp()}] 🎬 HDMI capture RTSP server ready for "
              f"connections")
        loop.run()

        # Clean up on exit
        server.shutdown()

        # Check if we exited due to video capture pipeline errors
        if server.pipeline_errors > 0:
            print(f"\n❌ Application terminated: video capture pipeline error(s) "
                  f"({server.pipeline_errors})")
            exit(1)

    except RuntimeError as e:
        print(f"❌ ERROR: {e}")
        print("\n💡 TROUBLESHOOTING:")
        print("   • Make sure your HDMI capture device is connected")
        print("   • Check that v4l2-ctl is installed: "
              "sudo apt install v4l-utils")
        print("   • For audio, set AUDIO_FORCE_CARD "
              "environment variable (optional)")
        print("   • Run with --debug for more detailed information")
        print("   • If device is stuck, try unplugging and replugging the USB device")
        print("\n📺 CLIENT COMPATIBILITY:")
        print("   ✅ Recommended: ffplay -rtsp_transport tcp "
              "rtsp://127.0.0.1:1234/hdmi")
        print("   ⚠️  VLC has known RTSP compatibility issues - "
              "use ffplay instead")
        exit(1)
    except KeyboardInterrupt:
        print(f"\n[{timestamp()}] 👋 Server stopped by user")
        exit(0)
    except Exception as e:
        print(f"❌ UNEXPECTED ERROR: {e}")
        if server:
            server.shutdown()
        exit(1)
    finally:
        # Final cleanup - this will be called even if exceptions occur
        # The atexit handlers will also run, but this provides immediate cleanup
        if server:
            try:
                server.shutdown()
            except Exception as e:
                print(f"⚠️  Error in final cleanup: {e}")


if __name__ == '__main__':
    main()
