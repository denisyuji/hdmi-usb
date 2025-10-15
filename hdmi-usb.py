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
        try:
            result = subprocess.run(
                ['v4l2-ctl', '-d', dev, '--all'],
                capture_output=True,
                text=True,
                timeout=5
            )
            info = result.stdout
            
            if not info:
                return False
            
            # Check for Video Capture capability
            if 'Video Capture' not in info:
                return False
            
            # Check for high resolution support (HDMI capture devices)
            if not re.search(r'1920.*1080|1280.*720', info):
                return False
            
            return True
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError, FileNotFoundError):
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
        """Detect video HDMI capture device."""
        for node in self.pick_nodes_by_name():
            if not node:
                continue
            if self.is_video_hdmi_usb(node):
                return node
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
        window_id = self.get_window_id(timeout=2.0)
        
        if not window_id:
            return
        
        self.log(f"Monitoring window {window_id} for position changes...")
        
        # Monitor window position every 2 seconds
        while self.gst_pid and self.is_process_running(self.gst_pid):
            try:
                current_geometry = self.get_window_geometry(window_id)
                
                if current_geometry and current_geometry != last_geometry:
                    self.window_state_file.write_text(current_geometry)
                    self.log(f"Window moved, state updated: {current_geometry}")
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
    
    def launch_gstreamer(self, video_dev: str, audio_card: Optional[str] = None):
        """Launch GStreamer pipeline."""
        # Build video pipeline
        gst_video = f"v4l2src device={video_dev} ! queue ! decodebin ! videoconvert ! videoscale ! ximagesink sync=false"
        
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
            
            cmd = ['gst-launch-1.0'] + gst_video.split() + gst_audio.split()
        else:
            self.log(f"Launching video-only preview in background (video={video_dev})")
            self.log(f"GStreamer command: gst-launch-1.0 {gst_video}")
            
            # Show info that audio is not available
            if not self.debug_mode:
                print("[INFO] No audio device found for USB HDMI capture - running video only")
            
            cmd = ['gst-launch-1.0'] + gst_video.split()
        
        # Launch GStreamer
        try:
            if self.debug_mode:
                process = subprocess.Popen(cmd)
            else:
                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL
                )
            
            self.gst_pid = process.pid
            self.log(f"GStreamer started with PID: {self.gst_pid}")
            self.log(f"To stop the preview, run: kill {self.gst_pid}")
            
            # Wait a moment to ensure GStreamer starts properly
            time.sleep(1)
            
            # Check if GStreamer is still running
            if self.is_process_running(self.gst_pid):
                self.log("Preview is running successfully in the background")
                self.log("Terminal is now free for other commands")
                
                # Apply window state
                if all([self.restore_x, self.restore_y, self.restore_width, self.restore_height]):
                    self.log(f"Restoring window to saved size: {self.restore_width}x{self.restore_height} at position: {self.restore_x},{self.restore_y}")
                    self.apply_window_state()
                
                # Start monitoring in background using threading
                import threading
                monitor_thread = threading.Thread(target=self.monitor_window_state, daemon=True)
                monitor_thread.start()
                
                return True
            else:
                self.err("GStreamer failed to start properly")
                return False
        except Exception as e:
            self.err(f"Failed to launch GStreamer: {e}")
            return False
    
    def run(self) -> int:
        """Main run method."""
        # Detect video device
        video_dev = self.detect_video_device()
        
        if not video_dev:
            self.err("Could not find a MacroSilicon USB Video HDMI capture device")
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
            
            return 0
        else:
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
    %(prog)s --reset-window    # Reset window state and exit
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
    return capture.run()


if __name__ == '__main__':
    sys.exit(main())

