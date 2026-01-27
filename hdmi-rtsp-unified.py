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

def setup_gstreamer_debug():
    """Setup GStreamer debug environment variables if debug mode is requested."""
    import sys
    if '--debug' in sys.argv:
        # Set general debug level to 3, but suppress videodecoder warnings (level 1 = errors only)
        os.environ['GST_DEBUG'] = '3,videodecoder:1'
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
        # Find all python processes running this script (excluding current process)
        result = subprocess.run(
            ['pgrep', '-f', f'python.*{script_name}'],
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
    """Manages local display pipeline for live preview.
    
    When share_video=True, uses a tee element to split the video stream:
    - One branch goes to local display (ximagesink)
    - Another branch goes to intervideosink for sharing with RTSP clients
    This solves the problem of v4l2src devices only supporting single access.
    """

    def __init__(self, video_device: str, audio_card: Optional[str] = None,
                 debug_mode: bool = False, share_video: bool = False,
                 server=None):
        self.video_device = video_device
        self.audio_card = audio_card
        self.debug_mode = debug_mode
        self.share_video = share_video
        self.pipeline = None
        self.shared_memory_socket_path = "/tmp/hdmi-usb-video-shm"
        self.intervideo_channel_name = "hdmi-usb-channel"
        self.server = server  # Reference to RTSPServer for shutdown callback
        
        # Window state management
        self.window_state_file = Path.home() / '.hdmi-rtsp-unified-window-state'
        self.restore_x = None
        self.restore_y = None
        self.restore_width = None
        self.restore_height = None
        self.monitor_thread = None
        self.monitor_running = False
        
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
            
            # Check if window was closed
            if "Output window was closed" in error_msg:
                print("🔴 Local display window closed, shutting down gracefully...")
                # Trigger graceful shutdown
                if self.server:
                    self.server.shutdown()
                else:
                    # If no server reference, just stop the pipeline
                    self.stop()
                    GLib.idle_add(lambda: self.main_loop.quit() if hasattr(self, 'main_loop') else None)
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
                self.server.shutdown()
        elif msg_type == Gst.MessageType.STATE_CHANGED and self.debug_mode:
            if message.src == self.pipeline:
                old_state, new_state, pending = message.parse_state_changed()
                self.log(f"State changed: {old_state.value_nick} -> "
                        f"{new_state.value_nick}")

        return True

    def restore_window_state(self):
        """Restore window state from file."""
        if not self.window_state_file.exists():
            self.log("No saved window state found")
            return
        
        try:
            geometry = self.window_state_file.read_text().strip()
            self.log(f"Restoring window state: {geometry}")
            
            # Parse geometry (format: WIDTHxHEIGHT+X+Y)
            match = re.match(r'^(\d+)x(\d+)\+(\d+)\+(\d+)$', geometry)
            if match:
                self.restore_width = match.group(1)
                self.restore_height = match.group(2)
                self.restore_x = match.group(3)
                self.restore_y = match.group(4)
                
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
                    if 'GStreamer' in line or 'ximagesink' in line:
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
    
    def apply_window_state(self):
        """Apply window state after GStreamer starts."""
        if not all([self.restore_x, self.restore_y, self.restore_width, 
                   self.restore_height]):
            return
        
        window_id = self.get_window_id(timeout=2.0)
        
        if window_id:
            # Check if wmctrl is available
            try:
                subprocess.run(['which', 'wmctrl'], capture_output=True, 
                             check=True, timeout=1)
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                self.log("wmctrl not available, window position not restored")
                return
            
            # Apply position immediately
            try:
                result = subprocess.run(
                    ['wmctrl', '-i', '-r', window_id, '-e', 
                     f"0,{self.restore_x},{self.restore_y},"
                     f"{self.restore_width},{self.restore_height}"],
                    capture_output=True,
                    text=True,
                    timeout=1
                )
                if result.returncode == 0:
                    self.log(f"Window geometry applied: {self.restore_width}x{self.restore_height} "
                             f"at {self.restore_x},{self.restore_y}")
                else:
                    self.log(f"Failed to apply window geometry: {result.stderr}")
                
                # Verify position and size were applied
                time.sleep(0.5)
                current_geometry = self.get_window_geometry(window_id)
                
                if current_geometry:
                    match = re.match(r'^(\d+)x(\d+)\+(\d+)\+(\d+)$', 
                                   current_geometry)
                    if match:
                        current_width = int(match.group(1))
                        current_height = int(match.group(2))
                        current_x = int(match.group(3))
                        current_y = int(match.group(4))
                        
                        # If position or size doesn't match, try once more
                        if (abs(current_x - int(self.restore_x)) >= 10 or 
                            abs(current_y - int(self.restore_y)) >= 10 or
                            abs(current_width - int(self.restore_width)) >= 10 or
                            abs(current_height - int(self.restore_height)) >= 10):
                            self.log(f"Geometry mismatch, retrying: "
                                   f"got {current_width}x{current_height}+{current_x}+{current_y}, "
                                   f"want {self.restore_width}x{self.restore_height}+"
                                   f"{self.restore_x}+{self.restore_y}")
                            subprocess.run(
                                ['wmctrl', '-i', '-r', window_id, '-e', 
                                 f"0,{self.restore_x},{self.restore_y},"
                                 f"{self.restore_width},{self.restore_height}"],
                                capture_output=True,
                                timeout=1
                            )
            except Exception as e:
                self.log(f"Failed to apply window state: {e}")
        else:
            self.log("Window not found after waiting, position not restored")
    
    def monitor_window_state(self):
        """Monitor window state and save changes."""
        time.sleep(3)  # Wait for window to appear
        
        last_geometry = ""
        last_width, last_height, last_x, last_y = 0, 0, 0, 0
        window_id = self.get_window_id(timeout=2.0)
        
        if not window_id:
            self.log("Failed to find window for monitoring")
            return
        
        self.log(f"Monitoring window geometry (ID: {window_id})")
        
        # Monitor window position and size every 2 seconds
        while self.monitor_running and self.pipeline:
            try:
                current_geometry = self.get_window_geometry(window_id)
                
                if current_geometry and current_geometry != last_geometry:
                    # Parse to detect what changed
                    match = re.match(r'^(\d+)x(\d+)\+(\d+)\+(\d+)$', current_geometry)
                    if match:
                        width, height = int(match.group(1)), int(match.group(2))
                        x, y = int(match.group(3)), int(match.group(4))
                        
                        changes = []
                        if last_geometry:
                            if width != last_width or height != last_height:
                                changes.append(f"resized to {width}x{height}")
                            if x != last_x or y != last_y:
                                changes.append(f"moved to {x},{y}")
                        
                        self.window_state_file.write_text(current_geometry)
                        if changes:
                            self.log(f"Window {' and '.join(changes)} - saved")
                        else:
                            self.log(f"Window geometry saved: {current_geometry}")
                        
                        last_width, last_height, last_x, last_y = width, height, x, y
                        last_geometry = current_geometry
            except Exception as e:
                self.log(f"Monitor error: {e}")
            
            time.sleep(2)
        
        self.log("Window monitoring stopped")

    def build_pipeline(self) -> str:
        """Build local display pipeline string."""
        # Video pipeline with tee for sharing if needed
        if self.share_video:
            # Decode ONCE, then use tee to split to display and intervideosink
            # intervideosink/src properly handles caps negotiation
            video_pipeline = (
                f'v4l2src device={self.video_device} ! '
                'jpegdec ! videoconvert ! tee name=t '
                't. ! queue ! videoscale ! ximagesink sync=false '
                't. ! queue ! '
                f'intervideosink channel={self.intervideo_channel_name}'
            )
        else:
            # Simple pipeline without sharing
            video_pipeline = (
                f'v4l2src device={self.video_device} ! '
                'queue ! decodebin ! videoconvert ! videoscale ! '
                'ximagesink sync=false'
            )

        # Add audio if available
        if self.audio_card:
            audio_device = f'plughw:{self.audio_card},0'
            audio_pipeline = (
                f'alsasrc device={audio_device} ! '
                'audioconvert ! audioresample ! autoaudiosink sync=false'
            )
            return f'{video_pipeline} {audio_pipeline}'
        else:
            return video_pipeline

    def _is_audio_device_available(self) -> bool:
        """Check if the audio device is available."""
        if not self.audio_card:
            return False
        
        try:
            result = subprocess.run(
                ['arecord', '-l'],
                capture_output=True,
                text=True,
                timeout=2
            )
            return f'card {self.audio_card}:' in result.stdout
        except Exception:
            return False

    def start(self) -> bool:
        """Start the local display pipeline."""
        # Check audio device availability
        if self.audio_card and not self._is_audio_device_available():
            self.log(f"Audio card {self.audio_card} not available, running video-only")
            self.audio_card = None
        
        # Restore window state before starting
        self.restore_window_state()
        
        pipeline_str = self.build_pipeline()

        if self.debug_mode:
            print(f"[LOCAL] Pipeline: {pipeline_str}")

        try:
            self.pipeline = Gst.parse_launch(pipeline_str)
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
            
            # Show info to user
            if self.audio_card:
                card_id_path = Path(f"/proc/asound/card{self.audio_card}/id")
                audio_card_name = "unknown"
                if card_id_path.exists():
                    try:
                        audio_card_name = card_id_path.read_text().strip()
                    except Exception:
                        pass
                print(f"[{timestamp()}] 🖥️  Local display showing video+audio "
                      f"(card {self.audio_card}: {audio_card_name})")
            else:
                print(f"[{timestamp()}] 🖥️  Local display showing video only")

            # Apply window state if we have saved position
            if all([self.restore_x, self.restore_y, self.restore_width, 
                   self.restore_height]):
                self.log(f"Restoring window to saved size: "
                        f"{self.restore_width}x{self.restore_height} "
                        f"at position: {self.restore_x},{self.restore_y}")
                time.sleep(0.5)
                self.apply_window_state()
            
            # Start monitoring window position in background thread
            import threading
            self.monitor_running = True
            self.monitor_thread = threading.Thread(
                target=self.monitor_window_state, 
                daemon=True
            )
            self.monitor_thread.start()

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
            # Stop monitoring thread
            self.monitor_running = False
            if self.monitor_thread and self.monitor_thread.is_alive():
                self.log("Stopping window monitoring thread...")
                self.monitor_thread.join(timeout=3)
            
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

class RTSPMediaFactory(GstRtspServer.RTSPMediaFactory):
    """Factory for creating RTSP media pipelines."""

    def __init__(self, video_device=None, audio_card=None, audio_only=False,
                 debug_mode=False, server=None, use_intervideo=False,
                 intervideo_channel=None):
        super().__init__()
        self.video_device = video_device
        self.audio_card = audio_card
        self.audio_only = audio_only
        self.debug_mode = debug_mode
        self.server = server
        self.use_intervideo = use_intervideo
        self.intervideo_channel = intervideo_channel or "hdmi-usb-channel"
        self.set_shared(True)

    def check_mjpeg_support(self) -> bool:
        """Check if the video device supports MJPEG format."""
        try:
            result = subprocess.run(
                ['v4l2-ctl', '-d', self.video_device, '--list-formats-ext'],
                capture_output=True,
                text=True,
                timeout=SUBPROCESS_TIMEOUT_SECONDS
            )
            return 'MJPG' in result.stdout or 'MJPEG' in result.stdout
        except Exception:
            return True

    def _build_audio_pipeline(self, device_spec: str, payload_name: str) -> str:
        """Build audio pipeline string."""
        return (
            f'alsasrc device={device_spec} ! '
            f'queue max-size-time=1000000000 ! '
            f'audioconvert ! audioresample ! '
            f'audio/x-raw,format=S16LE,rate={AUDIO_SAMPLE_RATE_HZ},channels=2 ! '
            f'voaacenc bitrate={AUDIO_BITRATE_BPS} ! '
            f'rtpmp4gpay pt=97 name={payload_name}'
        )

    def _build_video_pipeline(self, use_mjpeg: bool) -> str:
        """Build video pipeline string."""
        if self.use_intervideo:
            # Use intervideosrc (already decoded, caps handled automatically)
            if self.debug_mode:
                print(f"[RTSP] Using intervideosrc channel={self.intervideo_channel}")
            source = (
                f'intervideosrc channel={self.intervideo_channel} ! '
            )
            decoder = ''
        else:
            # Use direct v4l2 source
            if self.debug_mode:
                print(f"[RTSP] Using v4l2src device={self.video_device}, "
                      f"mjpeg={use_mjpeg}")
            source = f'v4l2src device={self.video_device} ! '
            
            if use_mjpeg:
                decoder = 'image/jpeg ! jpegdec ! '
            else:
                decoder = 'queue ! decodebin ! '

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

    def do_create_element(self, url):
        """Create GStreamer pipeline element."""
        if not self.use_intervideo and not self.video_device and not self.audio_only:
            print("❌ ERROR: No video device specified!")
            return None

        if self.audio_only and not self.audio_card:
            print("❌ ERROR: Audio-only mode requires audio card!")
            return None

        # Build pipeline based on mode
        if self.audio_only:
            pipeline_str = self._build_audio_pipeline(
                f'plughw:{self.audio_card},0', 'pay0'
            )
        else:
            mjpeg_supported = (self.check_mjpeg_support() 
                             if not self.use_intervideo else False)
            video_pipeline = self._build_video_pipeline(mjpeg_supported)

            if self.audio_card:
                audio_pipeline = self._build_audio_pipeline(
                    f'plughw:{self.audio_card},0', 'pay1'
                )
                pipeline_str = f'{video_pipeline} {audio_pipeline}'
            else:
                pipeline_str = video_pipeline

        if self.debug_mode:
            print(f"[DEBUG] Pipeline: {pipeline_str}")

        try:
            element = Gst.parse_launch(pipeline_str)
            if not element:
                error_msg = "Pipeline is NULL after parse_launch"
                print(f"❌ ERROR: {error_msg}!")
                if self.server:
                    self.server.on_pipeline_error(error_msg)
                return None

            return element
        except Exception as e:
            error_msg = f"Failed to create pipeline: {e}"
            print(f"❌ ERROR: {error_msg}")
            if self.server:
                self.server.on_pipeline_error(error_msg)
            return None

    def do_configure(self, media):
        """Configure media and set up monitoring."""
        media.connect("prepared", self.on_media_prepared)
        media.connect("target-state", self.on_target_state)
        media.connect("new-state", self.on_new_state)

    def on_media_prepared(self, media):
        """Set up bus monitoring when media is prepared."""
        element = media.get_element()
        if not element:
            if self.server:
                self.server.on_pipeline_error(
                    "Media element is NULL after preparation"
                )
            return

        bus = element.get_bus()
        if bus:
            bus.add_signal_watch()
            bus.connect("message", self.on_bus_message, media)

    def on_target_state(self, media, state):
        """Monitor target state changes."""
        if self.debug_mode:
            print(f"[DEBUG] Media target state: {state}")
        return True

    def on_new_state(self, media, state):
        """Monitor state changes and detect failures."""
        if self.debug_mode:
            print(f"[DEBUG] Media new state: {state}")

        if state == Gst.State.NULL:
            element = media.get_element()
            if element:
                bus = element.get_bus()
                if bus:
                    msg = bus.pop_filtered(Gst.MessageType.ERROR)
                    if msg and self.server:
                        err, _ = msg.parse_error()
                        self.server.on_pipeline_error(
                            f"Media failed to start: {err.message}"
                        )

        return True

    def on_bus_message(self, bus, message, media):
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
            if self.server and any(kw in error_msg.lower()
                                  for kw in critical_keywords):
                self.server.on_pipeline_error(error_msg)

        elif msg_type == Gst.MessageType.WARNING and self.debug_mode:
            warn, _ = message.parse_warning()
            print(f"⚠️  Pipeline WARNING: {warn.message}")

        return True


class RTSPServer(GstRtspServer.RTSPServer):
    """RTSP Server for HDMI capture streaming."""

    def _test_audio_device_availability(self, audio_card):
        """Test if audio device is available for RTSP streaming."""
        try:
            result = subprocess.run(
                ['arecord', '-D', f'plughw:{audio_card},0', '-f', 'cd', '-d', '1', '/dev/null'],
                capture_output=True, text=True, timeout=3
            )
            return result.returncode == 0
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError, FileNotFoundError):
            return False

    def __init__(self, audio_only=False, debug_mode=False, headless=False):
        super().__init__()
        self.port = DEFAULT_RTSP_PORT
        self.endpoint = DEFAULT_RTSP_ENDPOINT
        self.debug_mode = debug_mode
        self.headless = headless
        self.main_loop = None
        self.pipeline_errors = 0
        self.local_display = None
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
                # Test if audio device is available for RTSP streaming
                if not self._test_audio_device_availability(audio_card):
                    print(f"[{timestamp()}] ⚠️  Audio device busy - using video-only mode")
                    audio_card = None
                else:
                    print(f"[{timestamp()}] ✅ Audio device available for streaming")
            else:
                print(f"[{timestamp()}] ⚠️  No audio device found - video only")
        elif audio_only:
            raise RuntimeError(
                "Audio-only mode requires manual audio card specification"
            )

        # Determine if we need to share video source
        use_local_display = not self.headless and video_device and not audio_only
        intervideo_channel_name = "hdmi-usb-channel"

        # Start local display first if not in headless mode
        if use_local_display:
            print(f"[{timestamp()}] 🖥️  Starting local display...")
            self.local_display = LocalDisplayPipeline(
                video_device=video_device,
                audio_card=audio_card,
                debug_mode=debug_mode,
                share_video=True,  # Enable video sharing for RTSP
                server=self  # Pass server reference for shutdown callback
            )
            if not self.local_display.start():
                print(f"[{timestamp()}] ⚠️  Local display failed to start, "
                      f"continuing with RTSP server only")
                self.local_display = None
                use_local_display = False

        # Create and configure factory
        self.factory = RTSPMediaFactory(
            video_device=video_device,
            audio_card=audio_card,
            audio_only=audio_only,
            debug_mode=debug_mode,
            server=self,
            use_intervideo=use_local_display,  # Use intervideo if local display is running
            intervideo_channel=intervideo_channel_name
        )
        self.factory.set_eos_shutdown(False)
        self.factory.set_stop_on_disconnect(False)
        self.factory.set_transport_mode(GstRtspServer.RTSPTransportMode.PLAY)
        self.factory.set_latency(RTSP_LATENCY_MS)

        # Mount and attach server
        mount_points = self.get_mount_points()
        mount_points.add_factory(self.endpoint, self.factory)
        self.attach(None)
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
        '--debug',
        action='store_true',
        help='Enable debug output'
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

        server = RTSPServer(audio_only=args.audio_only,
                           debug_mode=args.debug,
                           headless=args.headless)
        loop = GLib.MainLoop()
        server.set_main_loop(loop)

        def shutdown_handler(sig, frame):
            print(f"\n[{timestamp()}] 👋 Shutting down RTSP server "
                  f"gracefully...")
            server.shutdown()
            loop.quit()

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
