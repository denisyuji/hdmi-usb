#!/usr/bin/env python3
"""
Autodetect MacroSilicon USB Video HDMI capture device and preview with GStreamer.
"""

import sys
import os
import re
import subprocess
import argparse
import time
import signal
from pathlib import Path
from typing import Optional, Tuple


class HDMICapture:
    """Main class for HDMI capture device detection and preview."""
    
    def __init__(self, debug_mode: bool = False):
        self.debug_mode = debug_mode
        self.match_name = os.environ.get('MATCH_NAME', 'MacroSilicon USB Video')
        self.audio_force_card = os.environ.get('AUDIO_FORCE_CARD', '')
        self.window_state_file = Path.home() / '.hdmi-usb-window-state'
        self.restore_x = None
        self.restore_y = None
        self.restore_width = None
        self.restore_height = None
        self.gst_pid = None
        
    def log(self, message: str):
        """Print log message if debug mode is enabled."""
        if self.debug_mode:
            print(f"[INFO] {message}")
    
    def err(self, message: str):
        """Print error message to stderr."""
        print(f"[ERR] {message}", file=sys.stderr)
    
    def is_video_hdmi_usb(self, dev: str) -> bool:
        """Check if device is a video HDMI capture device."""
        # First check if device file exists and is accessible
        if not os.path.exists(dev):
            self.log(f"Device {dev} does not exist")
            return False
        
        # Check if device is readable (not locked by another process)
        try:
            with open(dev, 'rb') as f:
                pass
        except PermissionError:
            self.log(f"Device {dev} is not accessible (may be in use by another process)")
            return False
        except Exception as e:
            self.log(f"Cannot access device {dev}: {e}")
            return False
        
        try:
            result = subprocess.run(
                ['v4l2-ctl', '-d', dev, '--all'],
                capture_output=True,
                text=True,
                timeout=5
            )
            
            # Log stderr if there are errors
            if result.stderr:
                self.log(f"v4l2-ctl stderr for {dev}: {result.stderr}")
            
            # If command failed, log the error
            if result.returncode != 0:
                self.log(f"v4l2-ctl failed for {dev} (return code: {result.returncode})")
                if result.stderr:
                    self.log(f"Error: {result.stderr}")
                return False
            
            info = result.stdout
            
            if not info:
                self.log(f"No output from v4l2-ctl for {dev}")
                return False
            
            # Check for Video Capture capability
            if 'Video Capture' not in info:
                self.log(f"Device {dev} does not have 'Video Capture' capability")
                # Log a sample of the output for debugging
                if self.debug_mode:
                    lines = info.splitlines()[:10]
                    self.log(f"Sample output from {dev}: {lines}")
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
                self.log(f"Device {dev} does not report expected HDMI resolutions")
                # In debug mode, show what resolutions are available
                if self.debug_mode:
                    # Try to extract available formats
                    format_lines = [line for line in info.splitlines() if 'Size:' in line or 'Width/Height' in line or 'fmt' in line.lower()]
                    if format_lines:
                        self.log(f"Available formats/resolutions for {dev}: {format_lines[:5]}")
                    else:
                        self.log(f"Could not find format information in output for {dev}")
                # Still allow the device if it has Video Capture - resolution might be negotiated at runtime
                # But log a warning
                self.log(f"Warning: Device {dev} has Video Capture but no expected HDMI resolutions found - will try anyway")
                return True  # Allow it - GStreamer can negotiate formats
            
            return True
        except subprocess.TimeoutExpired:
            self.log(f"Timeout querying device {dev}")
            return False
        except subprocess.CalledProcessError as e:
            self.log(f"Error querying device {dev}: {e}")
            if e.stderr:
                self.log(f"Error details: {e.stderr}")
            return False
        except FileNotFoundError:
            self.err("v4l2-ctl not found. Please install v4l-utils.")
            return False
        except Exception as e:
            self.log(f"Unexpected error checking device {dev}: {e}")
            return False
    
    def usb_tail_for_video(self, dev: str) -> Optional[str]:
        """Extract USB path for video device."""
        node = os.path.basename(dev)
        sys_path = f"/sys/class/video4linux/{node}/device"
        
        if not os.path.exists(sys_path):
            return None
        
        try:
            full_path = os.path.realpath(sys_path)
            # Extract USB path like "3-8.3.3"
            match = re.findall(r'\d+-[\d.]+', full_path)
            if match:
                return match[-1]
        except Exception:
            pass
        
        return None
    
    def alsa_card_for_usb_tail(self, usb_tail: str) -> Optional[str]:
        """Find ALSA card matching USB tail."""
        sound_path = Path('/sys/class/sound')
        
        for card_path in sound_path.glob('card*'):
            if not card_path.is_dir():
                continue
            
            device_path = card_path / 'device'
            if not device_path.exists():
                continue
            
            try:
                full_path = os.path.realpath(device_path)
                # Extract USB device path from audio card path
                audio_usb_matches = re.findall(r'\d+-[\d.]+', full_path)
                if not audio_usb_matches:
                    continue
                
                audio_usb_tail = audio_usb_matches[-1]
                
                # Match must be exact on the USB device path
                if audio_usb_tail == usb_tail:
                    card_num = card_path.name.replace('card', '')
                    
                    # Verify this card has a capture device
                    asound_path = Path(f"/proc/asound/card{card_num}")
                    if any(asound_path.glob('pcm*c')):
                        return card_num
                    else:
                        self.log(f"Warning: Found audio card {card_num} on same USB device, but it has no capture devices")
                        return None
            except Exception:
                continue
        
        return None
    
    def verify_audio_card(self, card_num: str) -> bool:
        """Verify audio card is valid and has capture capability."""
        # Get card name/description
        card_id_path = Path(f"/proc/asound/card{card_num}/id")
        if card_id_path.exists():
            try:
                card_info = card_id_path.read_text().strip()
                self.log(f"Audio card {card_num} ID: {card_info}")
            except Exception:
                card_info = "unknown"
        else:
            card_info = "unknown"
        
        # Verify the card has capture capability
        asound_path = Path(f"/proc/asound/card{card_num}")
        if not any(asound_path.glob('pcm*c')):
            self.err(f"Audio card {card_num} does not support capture")
            return False
        
        # Check if the card is USB-based
        card_path = Path(f"/sys/class/sound/card{card_num}/device")
        if card_path.exists():
            try:
                device_path = os.path.realpath(card_path)
                if 'usb' in device_path:
                    self.log(f"Verified: Audio card {card_num} ({card_info}) is a USB device with capture capability")
                    return True
            except Exception:
                pass
        
        self.log(f"Warning: Could not verify audio card {card_num} as a USB capture device")
        return True  # Still allow it to work
    
    def pick_nodes_by_name(self) -> list:
        """Get list of potential video devices."""
        try:
            result = subprocess.run(
                ['v4l2-ctl', '--list-devices'],
                capture_output=True,
                text=True,
                timeout=5
            )
            
            if result.returncode != 0:
                self.log(f"v4l2-ctl --list-devices failed (return code: {result.returncode})")
                if result.stderr:
                    self.log(f"Error: {result.stderr}")
                return []
            
            devices = []
            in_block = False
            
            for line in result.stdout.splitlines():
                if 'USB Video: USB Video' in line:
                    in_block = True
                    self.log(f"Found USB Video device block: {line.strip()}")
                    continue
                
                if in_block:
                    if not line.strip():
                        in_block = False
                        continue
                    
                    match = re.search(r'/dev/video\d+', line)
                    if match:
                        device = match.group(0)
                        devices.append(device)
                        self.log(f"Found potential device: {device}")
            
            if not devices:
                self.log("No USB Video devices found in v4l2-ctl output")
                if self.debug_mode:
                    self.log(f"Full v4l2-ctl output:\n{result.stdout}")
            
            return devices
        except subprocess.TimeoutExpired:
            self.log("Timeout running v4l2-ctl --list-devices")
            return []
        except subprocess.CalledProcessError as e:
            self.log(f"Error running v4l2-ctl --list-devices: {e}")
            if e.stderr:
                self.log(f"Error details: {e.stderr}")
            return []
        except FileNotFoundError:
            self.err("v4l2-ctl not found. Please install v4l-utils.")
            return []
    
    def detect_video_device(self) -> Optional[str]:
        """Detect video HDMI capture device."""
        devices = self.pick_nodes_by_name()
        
        if not devices:
            self.log("No USB Video devices found matching the expected pattern")
            return None
        
        self.log(f"Found {len(devices)} potential device(s), checking capabilities...")
        
        for node in devices:
            if not node:
                continue
            self.log(f"Checking device {node}...")
            if self.is_video_hdmi_usb(node):
                self.log(f"Device {node} passed all checks")
                return node
            else:
                self.log(f"Device {node} did not pass capability checks")
        
        self.log("No devices passed the capability checks")
        return None
    
    def get_video_info(self, video_dev: str):
        """Display video device information in debug mode."""
        if not self.debug_mode:
            return
        
        try:
            result = subprocess.run(
                ['v4l2-ctl', '-d', video_dev, '--all'],
                capture_output=True,
                text=True,
                timeout=5
            )
            
            for line in result.stdout.splitlines():
                if any(keyword in line for keyword in ['Card type', 'Bus info', 'Width/Height', 'Pixel Format', 'Frames per second']):
                    print(f"[INFO] {line.strip()}")
        except Exception:
            pass
    
    def detect_audio_card(self, video_dev: str) -> Optional[str]:
        """Detect audio card for the video device."""
        if self.audio_force_card:
            self.log(f"Forcing ALSA card: {self.audio_force_card}")
            if self.verify_audio_card(self.audio_force_card):
                return self.audio_force_card
            else:
                self.err(f"Forced audio card {self.audio_force_card} failed verification")
                return None
        
        usb_tail = self.usb_tail_for_video(video_dev)
        if usb_tail:
            self.log(f"USB path for video device: {usb_tail}")
            audio_card = self.alsa_card_for_usb_tail(usb_tail)
            if audio_card:
                self.log(f"Matched ALSA card by USB path: card {audio_card}")
                if self.verify_audio_card(audio_card):
                    self.log("Audio verification passed - audio is from the USB HDMI capture device")
                    return audio_card
                else:
                    self.err("Audio card verification failed")
                    return None
            else:
                self.log(f"No ALSA card matched USB path ({usb_tail}). Running video-only.")
        else:
            self.log("Could not resolve USB path tail. Running video-only.")
        
        return None
    
    def restore_window_state(self):
        """Restore window state from file."""
        if not self.window_state_file.exists():
            return
        
        try:
            geometry = self.window_state_file.read_text().strip()
            self.log(f"Will restore window state: {geometry}")
            
            # Parse geometry (format: WIDTHxHEIGHT+X+Y)
            match = re.match(r'^(\d+)x(\d+)\+(\d+)\+(\d+)$', geometry)
            if match:
                self.restore_width = match.group(1)
                self.restore_height = match.group(2)
                self.restore_x = match.group(3)
                self.restore_y = match.group(4)
                
                self.log(f"Will restore to: {self.restore_width}x{self.restore_height} at position {self.restore_x},{self.restore_y}")
        except Exception as e:
            self.log(f"Failed to read window state: {e}")
    
    def get_window_id(self, timeout: float = 5.0) -> Optional[str]:
        """Get window ID for GStreamer window."""
        start_time = time.time()
        
        while time.time() - start_time < timeout:
            try:
                result = subprocess.run(
                    ['xwininfo', '-name', 'gst-launch-1.0'],
                    capture_output=True,
                    text=True,
                    timeout=1
                )
                
                for line in result.stdout.splitlines():
                    if 'Window id:' in line:
                        parts = line.split()
                        if len(parts) >= 4:
                            return parts[3]
            except Exception:
                pass
            
            time.sleep(0.1)
        
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
        if not all([self.restore_x, self.restore_y, self.restore_width, self.restore_height]):
            return
        
        window_id = self.get_window_id(timeout=2.0)
        
        if window_id:
            # Check if wmctrl is available
            try:
                subprocess.run(['which', 'wmctrl'], capture_output=True, check=True)
            except subprocess.CalledProcessError:
                self.log("wmctrl not available, window position not restored")
                return
            
            # Apply position immediately
            try:
                subprocess.run(
                    ['wmctrl', '-i', '-r', window_id, '-e', 
                     f"0,{self.restore_x},{self.restore_y},{self.restore_width},{self.restore_height}"],
                    capture_output=True,
                    timeout=1
                )
                self.log(f"Window restored to saved position: {self.restore_x},{self.restore_y}")
                
                # Verify position was applied
                time.sleep(0.5)
                current_geometry = self.get_window_geometry(window_id)
                
                if current_geometry:
                    match = re.match(r'^(\d+)x(\d+)\+(\d+)\+(\d+)$', current_geometry)
                    if match:
                        current_x = int(match.group(3))
                        current_y = int(match.group(4))
                        
                        # If position doesn't match, try once more
                        if abs(current_x - int(self.restore_x)) >= 10 or abs(current_y - int(self.restore_y)) >= 10:
                            subprocess.run(
                                ['wmctrl', '-i', '-r', window_id, '-e', 
                                 f"0,{self.restore_x},{self.restore_y},{self.restore_width},{self.restore_height}"],
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
        window_id = self.get_window_id(timeout=5.0)
        
        if not window_id:
            self.log("Window not found for monitoring")
            return
        
        self.log(f"Monitoring window {window_id} for position changes...")
        
        # Monitor window position every 2 seconds
        # Check both PID and window existence for robustness
        while True:
            # Check if process is still running
            pid_running = self.gst_pid and self.is_process_running(self.gst_pid)
            
            # Check if window still exists
            try:
                window_exists = self.get_window_geometry(window_id) is not None
            except Exception:
                window_exists = False
            
            # Exit if both process and window are gone
            if not pid_running and not window_exists:
                self.log("Process and window no longer exist, stopping monitoring")
                break
            
            # Continue monitoring if either exists
            try:
                current_geometry = self.get_window_geometry(window_id)
                
                if current_geometry and current_geometry != last_geometry:
                    self.window_state_file.write_text(current_geometry)
                    self.log(f"Window moved/resized, state updated: {current_geometry}")
                    last_geometry = current_geometry
            except Exception:
                pass
            
            time.sleep(2)
        
        self.log("Window monitoring stopped")
    
    def is_process_running(self, pid: int) -> bool:
        """Check if process is running."""
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    
    def cleanup_gstreamer(self):
        """Clean up GStreamer process and related processes."""
        # Kill the tracked GStreamer process
        if self.gst_pid and self.is_process_running(self.gst_pid):
            try:
                self.log(f"Cleaning up GStreamer process (PID: {self.gst_pid})")
                os.kill(self.gst_pid, signal.SIGTERM)
                time.sleep(0.5)
                # Force kill if still running
                if self.is_process_running(self.gst_pid):
                    os.kill(self.gst_pid, signal.SIGKILL)
                    time.sleep(0.2)
            except (OSError, ProcessLookupError):
                pass
            finally:
                self.gst_pid = None
        
        # Also kill any orphaned gst-launch processes that might be using v4l2src
        # This handles cases where PID tracking failed
        try:
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
                        self.log(f"Cleaning up orphaned GStreamer process (PID: {pid})")
                        os.kill(pid, signal.SIGTERM)
                        time.sleep(0.2)
                        if self.is_process_running(pid):
                            os.kill(pid, signal.SIGKILL)
                    except (OSError, ProcessLookupError):
                        pass
        except Exception:
            pass
    
    def kill_existing_instances(self):
        """Kill other instances of this script and their GStreamer processes."""
        current_pid = os.getpid()
        killed_count = 0
        
        try:
            # Find all python processes running hdmi-usb.py (excluding current process)
            result = subprocess.run(
                ['pgrep', '-f', r'python.*hdmi-usb\.py'],
                capture_output=True,
                text=True,
                timeout=2
            )
            
            if result.returncode == 0:
                pids = [int(pid.strip()) for pid in result.stdout.strip().split('\n') if pid.strip()]
                for pid in pids:
                    if pid != current_pid:
                        try:
                            self.log(f"Killing existing instance (PID: {pid})")
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
            
            # Also kill any orphaned gst-launch processes for this device
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
                        self.log(f"Killing orphaned GStreamer process (PID: {pid})")
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
                self.log(f"Killed {killed_count} existing instance(s)")
                time.sleep(1)  # Give processes time to fully exit
                
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError, FileNotFoundError):
            # pgrep not available or failed, try alternative method
            pass
    
    def find_gst_launch_pid(self, parent_pid: Optional[int] = None, timeout: float = 3.0) -> Optional[int]:
        """Find the actual gst-launch-1.0 process PID.
        
        When using shell=True, the Popen PID is the shell, not gst-launch.
        This function finds the actual gst-launch-1.0 process.
        """
        start_time = time.time()
        
        while time.time() - start_time < timeout:
            try:
                # Find gst-launch-1.0 process that matches our video device
                # Use a more specific pattern to avoid matching other instances
                result = subprocess.run(
                    ['pgrep', '-f', 'gst-launch-1.0.*v4l2src'],
                    capture_output=True,
                    text=True,
                    timeout=1
                )
                
                if result.returncode == 0:
                    pids = [int(pid.strip()) for pid in result.stdout.strip().split('\n') if pid.strip()]
                    
                    if pids:
                        # If we have a parent PID, try to find the child
                        if parent_pid:
                            try:
                                # Get process tree to find children
                                result = subprocess.run(
                                    ['ps', '--ppid', str(parent_pid), '-o', 'pid=', '--no-headers'],
                                    capture_output=True,
                                    text=True,
                                    timeout=1
                                )
                                child_pids = [int(pid.strip()) for pid in result.stdout.strip().split('\n') if pid.strip()]
                                
                                # Check if any gst-launch PID is a child
                                for gst_pid in pids:
                                    if gst_pid in child_pids:
                                        return gst_pid
                                    # Also check grandchildren by checking if gst_pid's parent is a child
                                    try:
                                        parent_result = subprocess.run(
                                            ['ps', '-p', str(gst_pid), '-o', 'ppid=', '--no-headers'],
                                            capture_output=True,
                                            text=True,
                                            timeout=1
                                        )
                                        gst_parent = int(parent_result.stdout.strip())
                                        if gst_parent in child_pids:
                                            return gst_pid
                                    except Exception:
                                        pass
                            except Exception:
                                pass
                        
                        # Return the first (or only) PID found
                        return pids[0]
            except Exception:
                pass
            
            time.sleep(0.2)
        
        return None
    
    def check_device_streaming(self, video_dev: str) -> bool:
        """Check if device can start streaming (detect bad state)."""
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
        """Reset device state by closing any open streams."""
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
                self.err(f"Device {video_dev} is in a bad state (STREAMON fails)")
                self.err("This usually happens when a previous process didn't close the device properly.")
                self.err("Try one of these solutions:")
                self.err("  1. Unplug and replug the USB device")
                self.err("  2. Reset the USB device: sudo usb_modeswitch -v 0x534d -p 0x2109 -R")
                self.err("  3. Reload the driver: sudo modprobe -r uvcvideo && sudo modprobe uvcvideo")
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
    
    def launch_gstreamer(self, video_dev: str, audio_card: Optional[str] = None):
        """Launch GStreamer pipeline."""
        # Reset device state before starting
        if not self.reset_device_state(video_dev):
            self.err(f"Cannot use device {video_dev} - it is in a bad state")
            return False
        
        # Build video pipeline
        # Try explicit MJPG format first, fallback to auto-negotiation
        # Use jpegdec for MJPG decoding, videoconvert for format conversion
        # Queue helps with buffering
        # Try with explicit caps first - if this fails, we can fall back to decodebin
        gst_video = f"v4l2src device={video_dev} ! image/jpeg ! jpegdec ! videoconvert ! videoscale ! ximagesink sync=false"
        
        if audio_card:
            # Get audio card name
            audio_card_name = "unknown"
            card_id_path = Path(f"/proc/asound/card{audio_card}/id")
            if card_id_path.exists():
                try:
                    audio_card_name = card_id_path.read_text().strip()
                except Exception:
                    pass
            
            gst_audio = f"alsasrc device=hw:{audio_card},0 ! audioconvert ! audioresample ! autoaudiosink sync=false"
            self.log("Launching A/V preview in background")
            self.log(f"  Video: {video_dev}")
            self.log(f"  Audio: hw:{audio_card},0 ({audio_card_name})")
            self.log(f"GStreamer command: gst-launch-1.0 {gst_video} {gst_audio}")
            
            # Always show audio source info to user
            if not self.debug_mode:
                print(f"[INFO] Using audio from USB HDMI capture device (card {audio_card}: {audio_card_name})")
            
            # Use shell=True to match bash script behavior (allows proper pipeline expansion)
            cmd = f'gst-launch-1.0 {gst_video} {gst_audio}'
        else:
            self.log(f"Launching video-only preview in background (video={video_dev})")
            self.log(f"GStreamer command: gst-launch-1.0 {gst_video}")
            
            # Show info that audio is not available
            if not self.debug_mode:
                print("[INFO] No audio device found for USB HDMI capture - running video only")
            
            # Use shell=True to match bash script behavior (allows proper pipeline expansion)
            cmd = f'gst-launch-1.0 {gst_video}'
        
        # Launch GStreamer
        try:
            if self.debug_mode:
                process = subprocess.Popen(cmd, shell=True)
            else:
                process = subprocess.Popen(
                    cmd,
                    shell=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL
                )
            
            # When using shell=True, process.pid is the shell PID, not gst-launch
            # Find the actual gst-launch-1.0 process PID
            time.sleep(0.5)  # Give gst-launch time to start
            actual_gst_pid = self.find_gst_launch_pid(parent_pid=process.pid)
            
            if actual_gst_pid:
                self.gst_pid = actual_gst_pid
                self.log(f"GStreamer started with PID: {self.gst_pid} (found actual gst-launch process)")
            else:
                # Fallback to shell PID if we can't find gst-launch
                self.gst_pid = process.pid
                self.log(f"GStreamer started with PID: {self.gst_pid} (using shell PID, monitoring may be limited)")
            
            self.log(f"To stop the preview, run: kill {self.gst_pid}")
            
            # Wait a moment to ensure GStreamer starts properly
            time.sleep(0.5)
            
            # Check if GStreamer is still running
            if self.is_process_running(self.gst_pid):
                self.log("Preview is running successfully in the background")
                self.log("Terminal is now free for other commands")
                
                # Apply window state
                if all([self.restore_x, self.restore_y, self.restore_width, self.restore_height]):
                    self.log(f"Restoring window to saved size: {self.restore_width}x{self.restore_height} at position: {self.restore_x},{self.restore_y}")
                    self.apply_window_state()
                
                # Start monitoring in background using threading
                # Always start monitoring thread, even if PID detection failed
                import threading
                monitor_thread = threading.Thread(target=self.monitor_window_state, daemon=True)
                monitor_thread.start()
                self.log("Window monitoring thread started")
                
                return True
            else:
                self.err("GStreamer failed to start properly")
                return False
        except Exception as e:
            self.err(f"Failed to launch GStreamer: {e}")
            return False
    
    def run(self) -> int:
        """Main run method."""
        # Set up signal handlers for cleanup
        def signal_handler(signum, frame):
            self.log(f"Received signal {signum}, cleaning up...")
            self.cleanup_gstreamer()
            sys.exit(0)
        
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
        
        try:
            # Kill any existing instances before starting
            self.kill_existing_instances()
            
            # Detect video device
            video_dev = self.detect_video_device()
            
            if not video_dev:
                self.err("Could not find a MacroSilicon USB Video HDMI capture device")
                self.err("")
                self.err("Troubleshooting:")
                self.err("  1. Make sure the device is connected and recognized by the system")
                self.err("  2. Check 'v4l2-ctl --list-devices' to see if the device appears")
                self.err("  3. Try running with --debug to see detailed detection information")
                self.err("  4. Verify the device is not in use by another application")
                return 1
            
            self.log(f"Selected video node: {video_dev}")
            self.get_video_info(video_dev)
            
            # Detect audio card
            audio_card = self.detect_audio_card(video_dev)
            
            # Restore window state
            self.restore_window_state()
            
            # Launch GStreamer
            if self.launch_gstreamer(video_dev, audio_card):
                # Keep script running to maintain monitoring thread
                try:
                    while self.is_process_running(self.gst_pid):
                        time.sleep(1)
                except KeyboardInterrupt:
                    self.log("Interrupted by user")
                finally:
                    # Always cleanup on exit
                    self.cleanup_gstreamer()
                return 0
            else:
                return 1
        except Exception as e:
            self.err(f"Unexpected error: {e}")
            self.cleanup_gstreamer()
            return 1


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description='HDMI USB Capture Device Preview Tool',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
DESCRIPTION:
    Automatically detects MacroSilicon USB Video HDMI capture devices and
    launches a GStreamer preview window. The window position and size
    are automatically saved and restored between sessions.

EXAMPLES:
    %(prog)s                   # Launch with default settings (no debug output)
    %(prog)s --debug           # Launch with debug output enabled
    %(prog)s --reset-window     # Reset window state and exit
        '''
    )
    
    parser.add_argument(
        '-d', '--debug',
        action='store_true',
        help='Enable debug mode (show application and GStreamer logs)'
    )
    
    parser.add_argument(
        '--reset-window',
        action='store_true',
        help='Reset saved window position and size'
    )
    
    args = parser.parse_args()
    
    # Handle reset-window option
    if args.reset_window:
        window_state_file = Path.home() / '.hdmi-usb-window-state'
        if window_state_file.exists():
            window_state_file.unlink()
            print("[INFO] Window state reset. Next launch will use default position.")
        else:
            print("[INFO] No saved window state found.")
        return 0
    
    # Run the capture
    capture = HDMICapture(debug_mode=args.debug)
    
    # Continue with the full run
    return capture.run()


if __name__ == '__main__':
    sys.exit(main())

