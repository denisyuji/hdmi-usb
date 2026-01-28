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

gi.require_version('Gst', '1.0')
gi.require_version('GstRtspServer', '1.0')
from gi.repository import Gst, GstRtspServer, GLib, GObject

# Configuration constants
DEFAULT_RTSP_PORT = "1234"
DEFAULT_RTSP_ENDPOINT = "/hdmi"
RTSP_LATENCY_MS = 200
SUBPROCESS_TIMEOUT_SECONDS = 5
AUDIO_SAMPLE_RATE_HZ = 48000
AUDIO_BITRATE_BPS = 128000
VIDEO_BITRATE_KBPS = 3000
VIDEO_KEYFRAME_INTERVAL_FRAMES = 30


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
        # Set general debug level to 3, but suppress videodecoder warnings (level 1 = errors only)
        os.environ['GST_DEBUG'] = os.environ.get('GST_DEBUG', '3,videodecoder:1')
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
        """Reset device state by closing any open streams.
        
        From hdmi-usb.py - recovers from stuck device states.
        """
        try:
            # Try to query the device - this will fail if device is truly broken
            result = subprocess.run(
                ['v4l2-ctl', '-d', video_dev, '--all'],
                capture_output=True,
                text=True,
                timeout=2
            )
            if result.returncode != 0:
                self.log(f"Warning: Cannot query device {video_dev}, may be in bad state")
                return False
            
            # Check if device can stream
            if not self.check_device_streaming(video_dev):
                print(f"❌ ERROR: Device {video_dev} is in a bad state (STREAMON fails)", file=sys.stderr)
                print("   This usually happens when a previous process didn't close the device properly.", file=sys.stderr)
                print("   Try one of these solutions:", file=sys.stderr)
                print("     1. Unplug and replug the USB device", file=sys.stderr)
                print("     2. Reset the USB device: sudo usb_modeswitch -v 0x534d -p 0x2109 -R", file=sys.stderr)
                print("     3. Reload the driver: sudo modprobe -r uvcvideo && sudo modprobe uvcvideo", file=sys.stderr)
                return False
            
            # Try to set format explicitly to reset device state
            subprocess.run(
                ['v4l2-ctl', '-d', video_dev, '--set-fmt-video=pixelformat=MJPG,width=640,height=480'],
                capture_output=True,
                timeout=2
            )
            
            # Small delay to let device settle
            time.sleep(0.2)
            return True
        except Exception as e:
            self.log(f"Error resetting device state: {e}")
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
                # Reset device state before returning
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
        rtsp_url: str,
        debug_mode: bool = False,
        server=None,
        force_width: Optional[int] = None,
    ):
        self.rtsp_url = rtsp_url
        self.debug_mode = debug_mode
        self.pipeline = None
        self.server = server  # Reference to RTSPServer for shutdown callback
        # Used to match the correct window in wmctrl output.
        self.owner_pid = os.getpid()
        self.force_width = force_width
        
        # Window state management
        self.window_state_file = Path.home() / '.hdmi-rtsp-unified-window-state'
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
        self._window_watch_last_w = None
        self._window_watch_last_h = None
        self._window_watch_adjusting_until = 0.0
        
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
            is_close_request = (
                "Output window was closed" in error_msg or
                "Quit requested" in error_msg or
                "quit requested" in error_msg.lower()
            )

            if is_close_request:
                print("🔴 Local display window closed, shutting down gracefully...")
                # Trigger graceful shutdown via the main loop to avoid blocking
                # inside the GStreamer bus callback.
                if self.server:
                    GLib.idle_add(self.server.shutdown)
                else:
                    GLib.idle_add(self.stop)
            else:
                print(f"❌ Local Display ERROR: {error_msg}")
                if self.debug_mode:
                    print(f"   Debug: {debug_info}")
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

        # Start auto-saving window geometry changes (unless --width is used).
        self._start_window_watch()

        return False

    def restore_window_state(self):
        """Restore window state from file."""
        if self.force_width:
            self.log("Ignoring saved window state due to --width override")
            return
        if not self.window_state_file.exists():
            self.log("No saved window state found")
            return
        
        try:
            geometry = self.window_state_file.read_text().strip()
            self.log(f"Restoring window state: {geometry}")
            
            # Parse geometry (format: WIDTHxHEIGHT+X+Y)
            match = re.match(r'^(\d+)x(\d+)([+-]\d+)([+-]\d+)$', geometry)
            if match:
                self.restore_width = match.group(1)
                self.restore_height = match.group(2)
                self.restore_x = match.group(3)
                self.restore_y = match.group(4)

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
    
    def get_window_id(self, timeout: float = 5.0) -> Optional[str]:
        """Get window ID for GStreamer window.
        
        When using Gst.parse_launch(), the window is named 'python3' with class 'GStreamer',
        not 'gst-launch-1.0' like when using the command-line tool.
        """
        start_time = time.time()
        
        while time.time() - start_time < timeout:
            try:
                # Method 0 (most reliable): match windows by PID via `wmctrl -lp`.
                # Output format: WIN_ID DESK PID WM_CLASS TITLE...
                try:
                    wmctrl_lp = subprocess.run(
                        ['wmctrl', '-lp'],
                        capture_output=True,
                        text=True,
                        timeout=1
                    )
                    if wmctrl_lp.returncode == 0:
                        candidates = []
                        for line in wmctrl_lp.stdout.splitlines():
                            parts = line.split(None, 4)
                            if len(parts) < 4:
                                continue
                            win_id, _desk, pid_str, wm_class = parts[:4]
                            title = parts[4] if len(parts) >= 5 else ""
                            try:
                                pid = int(pid_str)
                            except ValueError:
                                continue
                            score = 0
                            wm_class_l = wm_class.lower()
                            title_l = title.lower()

                            # Prefer windows owned by this process, but don't require it:
                            # some sinks/window systems report a different PID.
                            if pid == self.owner_pid:
                                score += 3
                            if pid == 0:
                                score += 1

                            if ('gstreamer' in wm_class_l or
                                'ximagesink' in wm_class_l or
                                'glimagesink' in wm_class_l):
                                score += 2
                            if ('gstreamer' in title_l or
                                'opengl' in title_l or
                                'python' in title_l):
                                score += 1
                            candidates.append((score, win_id))
                        if candidates:
                            candidates.sort(reverse=True)
                            best = candidates[0][1]
                            self.log(f"Found window ID by PID {self.owner_pid}: {best}")
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
                            return window_id
                            
            except Exception as e:
                self.log(f"Error getting window ID: {e}")
            
            time.sleep(0.1)
        
        self.log(f"Window not found after {timeout} seconds")
        return None
    
    def get_window_geometry(self, window_id: str) -> Optional[str]:
        """Get window geometry."""
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
                    if len(parts) >= 2:
                        return parts[1]
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
                    time.sleep(0.25)
            except Exception:
                pass

            while time.time() < deadline:
                _clear_wm_state()
                _clear_size_hints()
                time.sleep(0.15)

                result = _apply_geometry()
                if result.returncode != 0 and self.debug_mode:
                    self.log(f"wmctrl -e failed: {result.stderr.strip()}")

                time.sleep(0.35)
                current_geometry = self.get_window_geometry(window_id)
                if current_geometry:
                    last_geometry = current_geometry
                    match = re.match(r'^(\d+)x(\d+)([+-]\d+)([+-]\d+)$', current_geometry)
                    if match:
                        current_w = int(match.group(1))
                        current_h = int(match.group(2))
                        current_x = int(match.group(3))
                        current_y = int(match.group(4))

                        if (abs(current_x - apply_x) < 10 and
                            abs(current_y - apply_y) < 10 and
                            abs(current_w - target_w) < 10 and
                            abs(current_h - target_h) < 10):
                            self.log(
                                f"Window geometry applied: {target_w}x{target_h} "
                                f"at {apply_x},{apply_y} (current={current_geometry})"
                            )
                            return True

                time.sleep(0.25)

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

            deadline = time.time() + 20.0
            last_geometry = None
            while time.time() < deadline:
                _clear_wm_state()
                _clear_size_hints()
                time.sleep(0.15)

                result = _apply_geometry()
                if result.returncode != 0 and self.debug_mode:
                    self.log(f"wmctrl -e failed: {result.stderr.strip()}")

                time.sleep(0.35)
                current_geometry = self.get_window_geometry(window_id)
                if current_geometry:
                    last_geometry = current_geometry
                    match = re.match(r'^(\d+)x(\d+)([+-]\d+)([+-]\d+)$', current_geometry)
                    if match:
                        current_w = int(match.group(1))
                        current_h = int(match.group(2))
                        if abs(current_w - target_w) < 10 and abs(current_h - target_h) < 10:
                            self.log(f"Forced window size applied: {target_w}x{target_h} (current={current_geometry})")
                            print(f"[{timestamp()}] 🪟 Local window geometry: {current_geometry}")
                            return True

                time.sleep(0.25)

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
        """Start a GLib timer that saves window geometry whenever it changes."""
        if self.force_width:
            return

        # Avoid double-starting.
        if self._window_watch_id is not None:
            return

        # Ignore transient startup geometry (some WMs briefly report maximized/fullscreen).
        self._window_watch_ignore_until = time.time() + 5.0

        def _tick() -> bool:
            # Stop if pipeline is gone or we're shutting down.
            if not self.pipeline:
                self._window_watch_id = None
                return False

            try:
                # Cache window id once we can find it.
                if not self._window_watch_window_id:
                    self._window_watch_window_id = self.get_window_id(timeout=0.2)
                    if not self._window_watch_window_id:
                        return True  # keep retrying

                geometry = self.get_window_geometry(self._window_watch_window_id)
                if not geometry:
                    return True

                # Enforce a 16:9 window geometry: whenever the window becomes
                # non-16:9, snap it back by adjusting the opposite dimension.
                #
                # We choose which dimension "drives" based on what changed most
                # since the last tick (width vs height).
                if time.time() >= self._window_watch_ignore_until:
                    m = re.match(r'^(\d+)x(\d+)([+-]\d+)([+-]\d+)$', geometry)
                    if m:
                        w = int(m.group(1))
                        h = int(m.group(2))

                        # If we're in the middle of an adjustment we initiated,
                        # don't react to intermediate transient sizes.
                        if time.time() >= self._window_watch_adjusting_until:
                            # Determine if geometry is sufficiently close to 16:9.
                            # Use a small tolerance to avoid thrashing due to WM rounding.
                            off = abs((w * 9) - (h * 16))
                            if off > (16 * 2):  # ~2px height error tolerance
                                drive_width = True
                                if self._window_watch_last_w is not None and self._window_watch_last_h is not None:
                                    drive_width = abs(w - self._window_watch_last_w) >= abs(h - self._window_watch_last_h)

                                if drive_width:
                                    target_w = _round_even(w)
                                    target_h = _compute_height_for_16_9(target_w)
                                else:
                                    target_h = _round_even(h)
                                    target_w = _compute_width_for_16_9(target_h)

                                if abs(target_w - w) >= 2 or abs(target_h - h) >= 2:
                                    self.log(f"Enforcing 16:9 window geometry: {target_w}x{target_h} (from {w}x{h})")
                                    # Avoid re-entrancy for a short window while WM applies changes.
                                    self._window_watch_adjusting_until = time.time() + 2.0
                                    self._apply_window_size_to_window(
                                        self._window_watch_window_id,
                                        target_w,
                                        target_h,
                                    )
                                    # Re-sample on next tick after the WM applies.
                                    return True

                if geometry != self._window_watch_last_geometry:
                    self._window_watch_last_geometry = geometry
                    m = re.match(r'^(\d+)x(\d+)([+-]\d+)([+-]\d+)$', geometry)
                    if m:
                        self._window_watch_last_w = int(m.group(1))
                        self._window_watch_last_h = int(m.group(2))

                    # Do not write the transient initial geometry.
                    if time.time() < self._window_watch_ignore_until:
                        return True

                    self.window_state_file.write_text(geometry)
                    self.log(f"Window geometry saved: {geometry}")
            except Exception as e:
                # Best-effort; don't crash the pipeline for window tooling issues.
                self.log(f"Window save error: {e}")

            return True

        # Polling is acceptable here; window managers don't emit a reliable event
        # stream we can subscribe to in this script, and this avoids extra threads.
        self._window_watch_id = GLib.timeout_add_seconds(1, _tick)
    
    def build_pipeline(self):
        """Build local display pipeline as RTSP client.

        We use `playbin` instead of manually wiring `rtspsrc` pads.
        RTSP commonly exposes multiple RTP streams (audio + video), and
        trying to feed those multiple pads into a single decodebin sink
        can lead to `GST_PAD_LINK_WAS_LINKED` and “not-linked” failures.
        """
        playbin = Gst.ElementFactory.make("playbin", "playbin")
        if not playbin:
            raise RuntimeError("Failed to create playbin element")

        playbin.set_property("uri", self.rtsp_url)

        # Prefer explicit sinks so window behavior is stable.
        #
        # Also, build a small videosink bin that includes videoscale so the
        # window can be resized freely. Without an explicit videoscale element,
        # some setups end up effectively clamping the window width (you'll see
        # height changes apply but width won't).
        videoconvert = Gst.ElementFactory.make("videoconvert", "local_videoconvert")
        videoscale = Gst.ElementFactory.make("videoscale", "local_videoscale")

        # Prefer a sink that can scale to an arbitrarily-resized window without
        # requiring caps that force a specific width/height.
        #
        # If we fall back to sinks that effectively clamp the window width to the
        # negotiated frame width, WM-based resizing may not be able to shrink.
        videosink = (
            Gst.ElementFactory.make("glimagesink", "videosink") or
            Gst.ElementFactory.make("xvimagesink", "videosink") or
            Gst.ElementFactory.make("ximagesink", "videosink")
        )
        if not (videoconvert and videoscale and videosink):
            raise RuntimeError("Failed to create local video sink elements")
        if self.debug_mode:
            try:
                factory = videosink.get_factory()
                sink_name = factory.get_name() if factory else type(videosink).__name__
                self.log(f"Using local videosink: {sink_name}")
            except Exception:
                pass

        videosink.set_property("sync", False)
        # Allow arbitrary resizing; don't enforce original aspect ratio in caps negotiation.
        try:
            videosink.set_property("force-aspect-ratio", False)
        except Exception:
            pass

        video_bin = Gst.Bin.new("local_videosink_bin")
        video_bin.add(videoconvert)
        video_bin.add(videoscale)
        video_bin.add(videosink)
        if not Gst.Element.link(videoconvert, videoscale) or not Gst.Element.link(videoscale, videosink):
            raise RuntimeError("Failed to link local video sink bin elements")

        # Expose a 'sink' pad on the bin so playbin can connect to it.
        sink_pad = videoconvert.get_static_pad("sink")
        if not sink_pad:
            raise RuntimeError("Failed to get videoconvert sink pad for ghosting")
        ghost_pad = Gst.GhostPad.new("sink", sink_pad)
        video_bin.add_pad(ghost_pad)

        audiosink = Gst.ElementFactory.make("autoaudiosink", "audiosink")
        if not audiosink:
            raise RuntimeError("Failed to create autoaudiosink for local display")
        audiosink.set_property("sync", False)

        playbin.set_property("video-sink", video_bin)
        playbin.set_property("audio-sink", audiosink)

        return playbin

    def start(self) -> bool:
        """Start the local display pipeline as RTSP client."""
        # Restore window state before starting
        self.restore_window_state()
        self._playing_init_done = False
        
        if self.debug_mode:
            print(f"[LOCAL] Building RTSP client pipeline for: {self.rtsp_url}")

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

    def _build_rtsp_launch_string(
        self,
        *,
        video_device: Optional[str],
        audio_device_spec: Optional[str],
        audio_only: bool,
        use_mjpeg: bool,
    ) -> str:
        """Build a gst-rtsp-server `set_launch()` pipeline string.

        Note: We intentionally use `set_launch()` (static pipeline) instead of a
        dynamic `do_create_element()` implementation, because dynamic pipelines
        in gst-rtsp-server are prone to per-client pipeline instantiation and
        suspension quirks that can lead to v4l2 "Device is busy" and RTSP 503
        failures when multiple clients connect (e.g., local preview + screenshot).
        """

        def _build_audio(device_spec: str, payload_name: str) -> str:
            # Quote device spec because it may contain commas (e.g. dsnoop:CARD=1,DEV=0).
            device_spec_q = device_spec.replace('"', '\\"')
            return (
                f'alsasrc device="{device_spec_q}" ! '
                f'queue max-size-time=1000000000 ! '
                f'audioconvert ! audioresample ! '
                f'audio/x-raw,format=S16LE,rate={AUDIO_SAMPLE_RATE_HZ},channels=2 ! '
                f'voaacenc bitrate={AUDIO_BITRATE_BPS} ! '
                f'rtpmp4gpay pt=97 name={payload_name}'
            )

        def _build_video() -> str:
            if not video_device:
                raise RuntimeError("No video device specified for RTSP launch")

            source = f'v4l2src device={video_device} ! '
            decoder = 'image/jpeg ! jpegdec ! ' if use_mjpeg else 'queue ! decodebin ! '
            encoder = (
                f'videoconvert ! video/x-raw,format=I420 ! '
                f'x264enc tune=zerolatency key-int-max={VIDEO_KEYFRAME_INTERVAL_FRAMES} '
                f'bitrate={VIDEO_BITRATE_KBPS} speed-preset=veryfast '
                f'byte-stream=true threads=1 ! '
                f'h264parse config-interval=1 ! '
                f'video/x-h264,stream-format=avc,alignment=au ! '
                f'rtph264pay config-interval=1 pt=96 name=pay0'
            )
            return source + decoder + encoder

        if audio_only:
            if not audio_device_spec:
                raise RuntimeError("Audio-only mode requires an audio device spec")
            return _build_audio(audio_device_spec, "pay0")

        video_pipeline = _build_video()
        if audio_device_spec:
            return f'{video_pipeline} {_build_audio(audio_device_spec, "pay1")}'
        return video_pipeline

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
        """Monitor bus messages for errors and warnings."""
        msg_type = message.type

        if msg_type == Gst.MessageType.ERROR:
            err, debug_info = message.parse_error()
            error_msg = err.message
            print(f"❌ GStreamer Pipeline ERROR: {error_msg}")
            if self.debug_mode:
                print(f"   Debug: {debug_info}")

            # Report critical errors to server
            critical_keywords = ("resource busy", "failed to", "cannot")
            if any(kw in error_msg.lower() for kw in critical_keywords):
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

    def __init__(self, audio_only=False, debug_mode=False, headless=False, viewer_width: Optional[int] = None):
        super().__init__()
        self.port = DEFAULT_RTSP_PORT
        self.endpoint = DEFAULT_RTSP_ENDPOINT
        self.debug_mode = debug_mode
        self.headless = headless
        self.main_loop = None
        self.pipeline_errors = 0
        self.local_display = None
        self.viewer_width = viewer_width
        self.audio_device_spec: Optional[str] = None
        self.set_address("0.0.0.0")
        self.set_service(self.port)
        
        # Register cleanup function for robust cleanup
        register_cleanup(self.shutdown)

        # Detect HDMI devices with enhanced validation
        detector = HDMIDeviceDetector(debug_mode=debug_mode)
        video_device = detector.detect_video_device()
        audio_card = None

        if not video_device and not audio_only:
            raise RuntimeError(
                "Could not find a MacroSilicon USB Video HDMI capture device"
            )

        if video_device:
            audio_card = detector.detect_audio_card(video_device)
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
        elif audio_only:
            raise RuntimeError(
                "Audio-only mode requires manual audio card specification"
            )

        # Determine if we need local display (as RTSP client)
        use_local_display = not self.headless and video_device and not audio_only
        rtsp_url = f"rtsp://127.0.0.1:{self.port}{self.endpoint}"

        # Create and configure factory first (server must be ready before client connects)
        # Use a static `set_launch()` factory so gst-rtsp-server can properly
        # share a single capture pipeline across multiple RTSP clients.
        self.factory = GstRtspServer.RTSPMediaFactory()
        self.factory.set_shared(True)
        if hasattr(self.factory, "set_reusable"):
            self.factory.set_reusable(True)

        use_mjpeg = False
        if video_device and not audio_only:
            try:
                # Check if the video device supports MJPEG format.
                result = subprocess.run(
                    ['v4l2-ctl', '-d', video_device, '--list-formats-ext'],
                    capture_output=True,
                    text=True,
                    timeout=SUBPROCESS_TIMEOUT_SECONDS,
                )
                use_mjpeg = ('MJPG' in result.stdout) or ('MJPEG' in result.stdout)
            except Exception:
                use_mjpeg = True

        launch = self._build_rtsp_launch_string(
            video_device=video_device,
            audio_device_spec=self.audio_device_spec if audio_card else None,
            audio_only=audio_only,
            use_mjpeg=use_mjpeg,
        )
        if self.debug_mode:
            print(f"[DEBUG] RTSP launch: {launch}")
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
            "AUDIO-ONLY 🎵" if audio_only else
            "VIDEO+AUDIO 🎥🎵" if audio_card else
            "VIDEO-ONLY 🎥"
        )
        print(f"[{timestamp()}] 🚀 RTSP server is running at "
              f"rtsp://0.0.0.0:{self.port}{self.endpoint}")
        print(f"[{timestamp()}] 📡 Streaming mode: {mode_info}")
        if self.headless:
            print(f"[{timestamp()}] 🚫 Headless mode: local display disabled")
        
        # Start local display as RTSP client after server is ready
        if use_local_display:
            # Wait longer for server to be fully ready and accept connections
            # The server needs time to bind to the port and be ready
            print(f"[{timestamp()}] 🖥️  Waiting for RTSP server to be ready...")
            time.sleep(3)  # Increased wait time for server to be fully ready
            print(f"[{timestamp()}] 🖥️  Starting local display as RTSP client...")
            self.local_display = LocalDisplayPipeline(
                rtsp_url=rtsp_url,
                debug_mode=debug_mode,
                server=self,  # Pass server reference for shutdown callback
                force_width=self.viewer_width,
            )
            if not self.local_display.start():
                print(f"[{timestamp()}] ⚠️  Local display failed to start, "
                      f"continuing with RTSP server only")
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
        """Handle pipeline errors by shutting down the server."""
        self.pipeline_errors += 1
        print(f"❌ Pipeline Error #{self.pipeline_errors}: {error_msg}")
        print(f"[{timestamp()}] 💥 Critical pipeline failure - "
              f"shutting down server")

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
            
            # Quit the main loop to exit gracefully
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
    restored between sessions. The video source is shared between the local
    display and RTSP clients using intervideosink/src. Use --headless to
    disable the local display (RTSP server will access the device directly).

    Default RTSP URL: rtsp://0.0.0.0:1234/hdmi

EXAMPLES:
    %(prog)s                     # Stream with local display (default)
    %(prog)s --headless          # Stream without local display window
    %(prog)s --audio-only        # Stream audio only (requires AUDIO_FORCE_CARD)
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
        '--audio-only',
        action='store_true',
        help='Start RTSP server in audio-only mode'
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
        window_state_file = Path.home() / '.hdmi-rtsp-unified-window-state'
        if window_state_file.exists():
            window_state_file.unlink()
            print("[INFO] Window state reset. Next launch will use default position.")
        else:
            print("[INFO] No saved window state found.")
        return 0

    # Kill existing instances before starting (from hdmi-usb.py)
    script_name = os.path.basename(__file__)
    kill_existing_instances(script_name, debug_mode=args.debug)
    if args.debug:
        print(f"[INFO] Instance management: checked for existing instances")

    server = None
    try:
        if args.audio_only:
            print("\033[92m🎵 Starting RTSP server in AUDIO-ONLY mode\033[0m")
        elif args.headless:
            print("\033[92m🎥🎵 Starting RTSP server in HEADLESS mode "
                  "(no local display)\033[0m")
        else:
            print("\033[92m🎥🎵 Starting unified RTSP server with local display "
                  "and HDMI capture\033[0m")

        server = RTSPServer(
            audio_only=args.audio_only,
            debug_mode=args.debug,
            headless=args.headless,
            viewer_width=args.width,
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

        # Check if we exited due to pipeline errors
        if server.pipeline_errors > 0:
            print(f"\n❌ Server terminated due to {server.pipeline_errors} "
                  f"pipeline error(s)")
            exit(1)

    except RuntimeError as e:
        print(f"❌ ERROR: {e}")
        print("\n💡 TROUBLESHOOTING:")
        print("   • Make sure your HDMI capture device is connected")
        print("   • Check that v4l2-ctl is installed: "
              "sudo apt install v4l-utils")
        print("   • For audio-only mode, set AUDIO_FORCE_CARD "
              "environment variable")
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
