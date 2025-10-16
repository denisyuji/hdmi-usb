#!/usr/bin/env python3
"""RTSP Server for HDMI USB Capture Devices

Automatically detects MacroSilicon USB Video HDMI capture devices and
streams live video/audio over RTSP.
"""
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
from gi.repository import Gst, GstRtspServer, GLib

# Constants
DEFAULT_PORT = "1234"
DEFAULT_ENDPOINT = "/hdmi"
RTSP_LATENCY_MS = 200
SUBPROCESS_TIMEOUT = 5
AUDIO_SAMPLE_RATE = 48000
AUDIO_BITRATE = 128000
VIDEO_BITRATE = 3000
VIDEO_KEYFRAME_INTERVAL = 30

Gst.init(None)


def timestamp() -> str:
    """Return current timestamp in standard format."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class HDMIDeviceDetector:
    """Detects and validates HDMI capture devices and associated audio cards."""

    def __init__(self, debug_mode: bool = False):
        self.debug_mode = debug_mode
        self.audio_force_card = os.environ.get('AUDIO_FORCE_CARD', '')

    def log(self, message: str) -> None:
        """Print log message if debug mode is enabled."""
        if self.debug_mode:
            print(f"[INFO] {message}")

    def is_video_hdmi_usb(self, device: str) -> bool:
        """Check if device is a video HDMI capture device."""
        try:
            result = subprocess.run(
                ['v4l2-ctl', '-d', device, '--all'],
                capture_output=True,
                text=True,
                timeout=SUBPROCESS_TIMEOUT
            )
            info = result.stdout

            if not info or 'Video Capture' not in info:
                return False

            # Check for high resolution support (HDMI capture devices)
            return bool(re.search(r'1920.*1080|1280.*720', info))

        except (subprocess.TimeoutExpired, subprocess.CalledProcessError,
                FileNotFoundError):
            return False

    def usb_tail_for_video(self, device: str) -> Optional[str]:
        """Extract USB path for video device."""
        node = os.path.basename(device)
        sys_path = f"/sys/class/video4linux/{node}/device"

        if not os.path.exists(sys_path):
            return None

        try:
            full_path = os.path.realpath(sys_path)
            match = re.findall(r'\d+-[\d.]+', full_path)
            return match[-1] if match else None
        except Exception:
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
                audio_usb_matches = re.findall(r'\d+-[\d.]+', full_path)
                if not audio_usb_matches:
                    continue

                # Match must be exact on the USB device path
                if audio_usb_matches[-1] == usb_tail:
                    card_num = card_path.name.replace('card', '')

                    # Verify this card has a capture device
                    asound_path = Path(f"/proc/asound/card{card_num}")
                    if any(asound_path.glob('pcm*c')):
                        return card_num

                    self.log(f"Warning: Found audio card {card_num} on same "
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
                timeout=SUBPROCESS_TIMEOUT
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
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError,
                FileNotFoundError):
            return []

    def detect_video_device(self) -> Optional[str]:
        """Detect video HDMI capture device."""
        for node in self.pick_nodes_by_name():
            if node and self.is_video_hdmi_usb(node):
                return node
        return None

    def detect_audio_card(self, video_device: str) -> Optional[str]:
        """Detect audio card for the video device."""
        if self.audio_force_card:
            self.log(f"Forcing ALSA card: {self.audio_force_card}")
            return (self.audio_force_card if
                    self.verify_audio_card(self.audio_force_card) else None)

        usb_tail = self.usb_tail_for_video(video_device)
        if not usb_tail:
            self.log("Could not resolve USB path tail. Running video-only.")
            return None

        self.log(f"USB path for video device: {usb_tail}")
        audio_card = self.alsa_card_for_usb_tail(usb_tail)

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


class RTSPMediaFactory(GstRtspServer.RTSPMediaFactory):
    """Factory for creating RTSP media pipelines."""

    def __init__(self, video_device=None, audio_card=None, audio_only=False,
                 debug_mode=False, server=None):
        super().__init__()
        self.video_device = video_device
        self.audio_card = audio_card
        self.audio_only = audio_only
        self.debug_mode = debug_mode
        self.server = server
        self.set_shared(True)

    def check_mjpeg_support(self) -> bool:
        """Check if the video device supports MJPEG format."""
        try:
            result = subprocess.run(
                ['v4l2-ctl', '-d', self.video_device, '--list-formats-ext'],
                capture_output=True,
                text=True,
                timeout=SUBPROCESS_TIMEOUT
            )
            return 'MJPG' in result.stdout or 'MJPEG' in result.stdout
        except Exception:
            return True

    def _build_audio_pipeline(self, device_spec: str, pay_name: str) -> str:
        """Build audio pipeline string."""
        return (
            f'alsasrc device={device_spec} ! '
            f'queue max-size-time=1000000000 ! '
            f'audioconvert ! audioresample ! '
            f'audio/x-raw,format=S16LE,rate={AUDIO_SAMPLE_RATE},channels=2 ! '
            f'voaacenc bitrate={AUDIO_BITRATE} ! '
            f'rtpmp4gpay pt=97 name={pay_name}'
        )

    def _build_video_pipeline(self, use_mjpeg: bool) -> str:
        """Build video pipeline string."""
        source = f'v4l2src device={self.video_device} ! '

        if use_mjpeg:
            decoder = 'image/jpeg ! jpegdec ! '
        else:
            decoder = 'queue ! decodebin ! '

        encoder = (
            f'videoconvert ! video/x-raw,format=I420 ! '
            f'x264enc tune=zerolatency key-int-max={VIDEO_KEYFRAME_INTERVAL} '
            f'bitrate={VIDEO_BITRATE} speed-preset=veryfast '
            f'byte-stream=true threads=1 ! '
            f'h264parse config-interval=1 ! '
            f'video/x-h264,stream-format=avc,alignment=au ! '
            f'rtph264pay config-interval=1 pt=96 name=pay0'
        )

        return source + decoder + encoder

    def do_create_element(self, url):
        """Create GStreamer pipeline element."""
        if not self.video_device and not self.audio_only:
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
            mjpeg_supported = self.check_mjpeg_support()
            video_pipeline = self._build_video_pipeline(mjpeg_supported)

            if self.audio_card:
                audio_pipeline = self._build_audio_pipeline(
                    f'dsnoop:{self.audio_card},0', 'pay1'
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

    def __init__(self, audio_only=False, debug_mode=False):
        super().__init__()
        self.port = DEFAULT_PORT
        self.endpoint = DEFAULT_ENDPOINT
        self.debug_mode = debug_mode
        self.main_loop = None
        self.pipeline_errors = 0
        self.set_address("0.0.0.0")
        self.set_service(self.port)

        # Detect HDMI devices
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
            else:
                print(f"[{timestamp()}] ⚠️  No audio device found - video only")
        elif audio_only:
            raise RuntimeError(
                "Audio-only mode requires manual audio card specification"
            )

        # Create and configure factory
        self.factory = RTSPMediaFactory(
            video_device=video_device,
            audio_card=audio_card,
            audio_only=audio_only,
            debug_mode=debug_mode,
            server=self
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


def main():
    """Main entry point for the RTSP server."""
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
    parser.add_argument(
        '--audio-only',
        action='store_true',
        help='Start RTSP server in audio-only mode'
    )
    parser.add_argument(
        '--debug',
        action='store_true',
        help='Enable debug output'
    )
    args = parser.parse_args()

    try:
        if args.audio_only:
            print("\033[92m🎵 Starting RTSP server in AUDIO-ONLY mode\033[0m")
        else:
            print("\033[92m🎥🎵 Starting RTSP server with HDMI capture\033[0m")

        server = RTSPServer(audio_only=args.audio_only,
                           debug_mode=args.debug)
        loop = GLib.MainLoop()
        server.set_main_loop(loop)

        def shutdown(sig, frame):
            print(f"\n[{timestamp()}] 👋 Shutting down RTSP server "
                  f"gracefully...")
            loop.quit()

        signal.signal(signal.SIGINT, shutdown)
        signal.signal(signal.SIGTERM, shutdown)

        print(f"[{timestamp()}] 🎬 HDMI capture RTSP server ready for "
              f"connections")
        loop.run()

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
        print("\n📺 CLIENT COMPATIBILITY:")
        print("   ✅ Recommended: ffplay -rtsp_transport tcp "
              "rtsp://127.0.0.1:1234/hdmi")
        print("   ⚠️  VLC has known RTSP compatibility issues - "
              "use ffplay instead")
        exit(1)
    except KeyboardInterrupt:
        print(f"\n[{timestamp()}] 👋 Server stopped by user")
        exit(0)


if __name__ == '__main__':
    main()
