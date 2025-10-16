#!/usr/bin/env python3
import gi
import argparse
import signal
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional

gi.require_version('Gst', '1.0')
gi.require_version('GstRtspServer', '1.0')
from gi.repository import Gst, GstRtspServer, GLib, GObject

Gst.init(None)

def timestamp():
  return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

class HDMIDeviceDetector:
    """Device detection class adapted from hdmi-usb.py"""
    
    def __init__(self, debug_mode: bool = False):
        self.debug_mode = debug_mode
        self.audio_force_card = os.environ.get('AUDIO_FORCE_CARD', '')
    
    def log(self, message: str):
        """Print log message if debug mode is enabled."""
        if self.debug_mode:
            print(f"[INFO] {message}")
    
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
    
    def detect_audio_card(self, video_dev: str) -> Optional[str]:
        """Detect audio card for the video device."""
        if self.audio_force_card:
            self.log(f"Forcing ALSA card: {self.audio_force_card}")
            if self.verify_audio_card(self.audio_force_card):
                return self.audio_force_card
            else:
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
                    return None
            else:
                self.log(f"No ALSA card matched USB path ({usb_tail}). Running video-only.")
        else:
            self.log("Could not resolve USB path tail. Running video-only.")
        
        return None

class LoopMediaFactory(GstRtspServer.RTSPMediaFactory):
  def __init__(self, video_device=None, audio_card=None, audio_only=False, debug_mode=False, server=None):
    super(LoopMediaFactory, self).__init__()
    self.video_device = video_device
    self.audio_card = audio_card
    self.audio_only = audio_only
    self.debug_mode = debug_mode
    self.server = server  # Reference to RTSPServer for error handling
    self.set_shared(True)  # Share pipelines to avoid audio/video device conflicts

  def check_mjpeg_support(self):
    """Check if the video device supports MJPEG format."""
    try:
      import subprocess
      result = subprocess.run(
        ['v4l2-ctl', '-d', self.video_device, '--list-formats-ext'],
        capture_output=True,
        text=True,
        timeout=5
      )
      return 'MJPG' in result.stdout or 'MJPEG' in result.stdout
    except Exception:
      return True  # Assume MJPEG is supported if we can't check
  
  def do_create_element(self, url):
    """Create GStreamer pipeline element."""
    # Validate devices
    if not self.video_device and not self.audio_only:
      print("❌ ERROR: No video device specified!")
      return None
    
    if self.audio_only and not self.audio_card:
      print("❌ ERROR: Audio-only mode requires audio card!")
      return None
    
    # Check if device supports MJPEG
    mjpeg_supported = self.check_mjpeg_support()
    
    if self.audio_only:
      # Audio-only pipeline using ALSA source
      pipeline_str = (
        f'alsasrc device=plughw:{self.audio_card},0 ! '
        'queue max-size-time=1000000000 ! '
        'audioconvert ! audioresample ! audio/x-raw,format=S16LE,rate=48000,channels=2 ! '
        'voaacenc bitrate=128000 ! rtpmp4gpay pt=97 name=pay0'
      )
    else:
      # Video + Audio pipeline - use MJPEG if supported, otherwise fallback to decodebin
      if mjpeg_supported:
        video_pipeline = (
          f'v4l2src device={self.video_device} ! '
          'image/jpeg ! jpegdec ! videoconvert ! video/x-raw,format=I420 ! '
          'x264enc tune=zerolatency key-int-max=30 bitrate=3000 speed-preset=veryfast byte-stream=true threads=1 ! '
          'h264parse config-interval=1 ! '
          'video/x-h264,stream-format=avc,alignment=au ! '
          'rtph264pay config-interval=1 pt=96 name=pay0'
        )
      else:
        video_pipeline = (
          f'v4l2src device={self.video_device} ! '
          'queue ! decodebin ! videoconvert ! video/x-raw,format=I420 ! '
          'x264enc tune=zerolatency key-int-max=30 bitrate=3000 speed-preset=veryfast byte-stream=true threads=1 ! '
          'h264parse config-interval=1 ! '
          'video/x-h264,stream-format=avc,alignment=au ! '
          'rtph264pay config-interval=1 pt=96 name=pay0'
        )
      
      if self.audio_card:
        # Video + Audio
        audio_pipeline = (
          f'alsasrc device=dsnoop:{self.audio_card},0 ! '
          'queue max-size-time=1000000000 ! '
          'audioconvert ! audioresample ! audio/x-raw,format=S16LE,rate=48000,channels=2 ! '
          'voaacenc bitrate=128000 ! rtpmp4gpay pt=97 name=pay1'
        )
        pipeline_str = f'{video_pipeline} {audio_pipeline}'
      else:
        # Video only
        pipeline_str = video_pipeline
    
    if self.debug_mode:
      print(f"[DEBUG] Pipeline: {pipeline_str}")
    
    try:
      element = Gst.parse_launch(pipeline_str)
      if not element:
        print("❌ ERROR: Pipeline is NULL after parse_launch!")
        if self.server:
          self.server.on_pipeline_error("Pipeline is NULL after parse_launch")
        return None
      
      return element
    except Exception as e:
      error_msg = f"Failed to create pipeline: {e}"
      print(f"❌ ERROR: {error_msg}")
      if self.server:
        self.server.on_pipeline_error(error_msg)
      return None
  
  def do_configure(self, media):
    """Configure media and monitor for errors."""
    media.connect("prepared", self.on_media_prepared)
    media.connect("target-state", self.on_target_state)
    media.connect("new-state", self.on_new_state)
    
  def on_media_prepared(self, media):
    """Called when media is prepared - set up bus monitoring."""
    element = media.get_element()
    if not element:
      if self.server:
        self.server.on_pipeline_error("Media element is NULL after preparation")
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
    """Monitor actual state changes and detect failures."""
    if self.debug_mode:
      print(f"[DEBUG] Media new state: {state}")
    
    # If we're in NULL or READY state after trying to prepare, something went wrong
    if state == Gst.State.NULL:
      element = media.get_element()
      if element:
        # Check the bus for error messages
        bus = element.get_bus()
        if bus:
          msg = bus.pop_filtered(Gst.MessageType.ERROR)
          if msg:
            err, dbg = msg.parse_error()
            if self.server:
              self.server.on_pipeline_error(f"Media failed to start: {err.message}")
    
    return True
  
  def on_bus_message(self, bus, message, media):
    """Monitor bus messages for errors."""
    t = message.type
    if t == Gst.MessageType.ERROR:
      err, dbg = message.parse_error()
      error_msg = f"{err.message}"
      print(f"❌ GStreamer Pipeline ERROR: {error_msg}")
      if self.debug_mode:
        print(f"   Debug: {dbg}")
      
      # Report critical errors to server
      if self.server and ("resource busy" in error_msg.lower() or 
                         "failed to" in error_msg.lower() or
                         "cannot" in error_msg.lower()):
        self.server.on_pipeline_error(error_msg)
    
    elif t == Gst.MessageType.WARNING:
      warn, dbg = message.parse_warning()
      if self.debug_mode:
        print(f"⚠️  Pipeline WARNING: {warn.message}")
    
    return True


class RTSPServer(GstRtspServer.RTSPServer):
  def __init__(self, audio_only=False, debug_mode=False):
    super(RTSPServer, self).__init__()
    self.port = "1234"
    self.endpoint = "/hdmi"
    self.debug_mode = debug_mode
    self.main_loop = None  # Will be set later
    self.pipeline_errors = 0  # Track pipeline errors
    self.set_address("0.0.0.0")
    self.set_service(self.port)
    
    # Detect HDMI devices
    detector = HDMIDeviceDetector(debug_mode=debug_mode)
    video_device = detector.detect_video_device()
    audio_card = None
    
    if not video_device and not audio_only:
      raise RuntimeError("Could not find a MacroSilicon USB Video HDMI capture device")
    
    if video_device:
      audio_card = detector.detect_audio_card(video_device)
      if audio_card:
        print(f"[{timestamp()}] ✅ Found video device: {video_device}")
        print(f"[{timestamp()}] ✅ Found audio card: {audio_card}")
      else:
        print(f"[{timestamp()}] ✅ Found video device: {video_device}")
        print(f"[{timestamp()}] ⚠️ No audio device found - video only")
    elif audio_only:
      # For audio-only mode, try to detect any audio card
      detector.log("Audio-only mode: attempting to find audio card")
      # This is a simplified approach - in practice you might want to list available cards
      raise RuntimeError("Audio-only mode requires manual audio card specification")
    
    # Create factory with detected devices
    self.factory = LoopMediaFactory(
      video_device=video_device,
      audio_card=audio_card,
      audio_only=audio_only,
      debug_mode=debug_mode,
      server=self  # Pass server reference for error handling
    )
    self.factory.set_eos_shutdown(False)
    self.factory.set_stop_on_disconnect(False)  # Keep pipeline running for multiple clients
    self.factory.set_transport_mode(GstRtspServer.RTSPTransportMode.PLAY)
    self.factory.set_latency(200)  # 200ms latency for better compatibility
    
    mount_points = self.get_mount_points()
    mount_points.add_factory(self.endpoint, self.factory)
    self.attach(None)
    self.connect("client-connected", self.on_client_connected)
    
    # Print server info
    mode_info = "AUDIO-ONLY 🎵" if audio_only else ("VIDEO+AUDIO 🎥🎵" if audio_card else "VIDEO-ONLY 🎥")
    print(f"[{timestamp()}] 🚀 RTSP server is running at rtsp://0.0.0.0:{self.port}{self.endpoint}")
    print(f"[{timestamp()}] 📡 Streaming mode: {mode_info}")

  def on_client_connected(self, server, client):
    ip = client.get_connection().get_ip()
    print(f"[{timestamp()}] 📡 Client connected from {ip}")
    client.connect("closed", self.on_client_disconnected)

  def on_client_disconnected(self, client):
    ip = client.get_connection().get_ip()
    print(f"[{timestamp()}] ❌ Client disconnected: {ip}")
  
  def on_pipeline_error(self, error_msg):
    """Handle pipeline errors by shutting down the server."""
    self.pipeline_errors += 1
    print(f"❌ Pipeline Error #{self.pipeline_errors}: {error_msg}")
    
    # Terminate after first critical error
    if self.pipeline_errors >= 1:
      print(f"[{timestamp()}] 💥 Critical pipeline failure - shutting down server")
      if self.main_loop:
        GLib.idle_add(self.main_loop.quit)
  
  def set_main_loop(self, loop):
    """Set the main loop reference for error handling."""
    self.main_loop = loop

if __name__ == '__main__':
  parser = argparse.ArgumentParser(
    description='HDMI USB Capture RTSP Server',
    formatter_class=argparse.RawDescriptionHelpFormatter,
    epilog='''
DESCRIPTION:
    Automatically detects MacroSilicon USB Video HDMI capture devices and
    streams live video/audio over RTSP. The server will auto-detect both
    video and audio devices from the same USB HDMI capture adapter.
    
    Default RTSP URL: rtsp://0.0.0.0:1234/hdmi

EXAMPLES:
    %(prog)s                     # Stream video+audio (auto-detect devices)
    %(prog)s --audio-only        # Stream audio only (requires AUDIO_FORCE_CARD)
    %(prog)s --debug             # Enable debug output
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
  parser.add_argument('--audio-only', action='store_true', help='Start RTSP server in audio-only mode')
  parser.add_argument('--debug', action='store_true', help='Enable debug output')
  args = parser.parse_args()

  try:
    if args.audio_only:
      print("\033[92m🎵 Starting RTSP server in AUDIO-ONLY mode\033[0m")
    else:
      print("\033[92m🎥🎵 Starting RTSP server with HDMI capture\033[0m")

    server = RTSPServer(audio_only=args.audio_only, debug_mode=args.debug)
    loop = GLib.MainLoop()
    server.set_main_loop(loop)  # Set loop reference for error handling

    def shutdown(sig, frame):
      print(f"\n[{timestamp()}] 👋 Shutting down RTSP server gracefully...")
      loop.quit()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    
    print(f"[{timestamp()}] 🎬 HDMI capture RTSP server ready for connections")
    loop.run()
    
    # Check if we exited due to pipeline errors
    if server.pipeline_errors > 0:
      print(f"\n❌ Server terminated due to {server.pipeline_errors} pipeline error(s)")
      exit(1)
    
  except RuntimeError as e:
    print(f"❌ ERROR: {e}")
    print("\n💡 TROUBLESHOOTING:")
    print("   • Make sure your HDMI capture device is connected")
    print("   • Check that v4l2-ctl is installed: sudo apt install v4l-utils")
    print("   • For audio-only mode, set AUDIO_FORCE_CARD environment variable")
    print("   • Run with --debug for more detailed information")
    print("\n📺 CLIENT COMPATIBILITY:")
    print("   ✅ Recommended: ffplay -rtsp_transport tcp rtsp://127.0.0.1:1234/hdmi")
    print("   ⚠️  VLC has known RTSP compatibility issues - use ffplay instead")
    exit(1)
  except KeyboardInterrupt:
    print(f"\n[{timestamp()}] 👋 Server stopped by user")
    exit(0)
