# Plan: hardware MJPEG decode via vajpegdec

Status: **deployed 2026-07-18** — hardware decode is live in the running
server; integration suite passes 11/11 (twice). Measured total process CPU
dropped from ~14% to ~9.5% of a core.

## Motivation

CPU profile of the server before the change (`hdmi-usb.py`, 6h18m uptime,
~14% of one core total, ~90 MB):

| Thread | Avg CPU | What it is |
|---|---|---|
| `queue0` | ~7–9% | MJPEG software decode (`jpegdec`) of 1080p25 + videoconvert + tee |
| `gstglcolorconvert` | ~3% | Local preview: GL upload/convert for `glimagesink` |
| `intervideosink` | ~1% | Frame fan-out to RTSP/local inter* channels |

Software JPEG decode of the 1920×1080@25 capture stream was the dominant
cost. The machine has an Intel GPU with VA-API (`/dev/dri/renderD128`) and
the `vajpegdec` element available; the H.264 *encode* side already used
VA-API (`vah264lpenc`), so decode was the missing half.

## The change

In `_start_capture_pipeline` (hdmi-usb.py), the MJPEG branch now prefers
hardware decode when `vajpegdec`, `vapostproc`, and `/dev/dri/renderD128`
are all present (the same device guard `_pick_encoder` uses), and falls
back to the previous software `jpegdec` otherwise:

```
queue … leaky=downstream !
image/jpeg,framerate=25/1,sof-marker=0,colorspace=sYUV,
    sampling=YCbCr-4:2:2,interlace-mode=progressive !
vajpegdec ! vapostproc ! video/x-raw,format=I420 ! videoconvert ! tee …
```

Startup logs `✅ Using hardware JPEG decoder: vajpegdec` when the hardware
path is taken.

A second, related fix went into the local-display window watcher: the 16:9
enforcement now re-reads the window geometry immediately before applying,
because applying a target computed from a ~1s-stale read races with
concurrent external resizes (this is what made the integration suite's
window tests fail after the decode change shifted startup timing).

## Findings

1. **`vajpegdec` cannot be adopted via `decodebin`/`jpegparse`.** It has
   rank `none`, so `decodebin` never selects it, and `jpegparse` on
   GStreamer 1.24.2 still emits old-style caps that `vajpegdec` refuses.

2. **Four caps fields are mandatory for negotiation.** `vajpegdec` only
   negotiates when the JPEG caps carry explicit `sof-marker`, `colorspace`,
   `sampling`, and `interlace-mode` fields. Dropping any one of them fails
   with `not-negotiated`. Neither `v4l2src` nor demuxers/parsers set them,
   hence the explicit capsfilter. Negotiation against the real `v4l2src`
   works.

3. **Wrong hint values are harmless.** A genuine 4:2:0 stream claimed as
   4:2:2 (and vice versa) still decodes; a dumped frame was pixel-perfect.
   The decoder parses the real bitstream — the caps values are negotiation
   hints only. Hardcoding `sampling=YCbCr-4:2:2` (typical for MacroSilicon)
   is therefore safe even if the device emits something else.

4. **`vapostproc` is required, not optional.** `vajpegdec` alone made CPU
   *worse* (~20% total vs 14% software): it outputs its native 4:2:2
   (Y42B), so the downstream CPU `videoconvert` did real conversion work
   and the GL preview uploaded fatter frames. Converting to I420 on the GPU
   via `vapostproc` fixes it, and I420 matches software `jpegdec`'s output
   so all downstream branches behave identically. Forcing
   `vajpegdec ! video/x-raw,format=I420` directly fails to negotiate, and
   `vapostproc ! NV12` crashed the pipeline — `vapostproc ! I420` is the
   working combination.

5. **Decode-chain benchmark** (500 × 1080p MJPEG frames from file, sink =
   `intervideosink sync=false`, CPU-seconds):

   | Chain | CPU time |
   |---|---|
   | `jpegdec ! videoconvert` (software) | 0.86 s |
   | `vajpegdec ! videoconvert` (CPU convert) | 1.43 s ← regression |
   | `vajpegdec ! vapostproc ! I420` | **0.20 s** |

   Caution: benchmarking with `fakesink` is misleading — frames stay in
   GPU memory and the mandatory VAMemory→system download is never paid.

6. **Deployed result** (local preview up, no RTSP clients): total process
   CPU ~9.5% of a core (was ~14%); the decode thread is no longer the top
   consumer. Memory ~83 MB PSS (was ~90 MB RSS+swap) — the VA driver
   context replaces what the software decoder used.

## Verification performed

- Startup log shows the hardware decoder line; no pipeline errors.
- Stream visually correct end-to-end (RTSP → MCP `get_last_frame` PNG).
- `test_hdmi_usb_screenshot_mcp.py` passes.
- `integration-test.sh` passes 11/11 twice in a row (the window-watch race
  fix was required for this; unmodified HEAD passed, the decode change
  alone failed the window tests reproducibly by shifting the enforcement
  timing into the test's resize).
