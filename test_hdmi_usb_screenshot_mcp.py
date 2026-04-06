#!/usr/bin/env python3
"""
Spawn hdmi-usb-screenshot-mcp and verify the stdio MCP protocol: initialize,
ping, tools/list, tools/call get_last_frame (PNG base64).

Requires a reachable RTSP_URL. The child exits during RTSP preflight if the
stream is unavailable.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path


def _write_mcp(proc: subprocess.Popen, obj: dict) -> None:
    """MCP 2025-03-26 stdio: one JSON-RPC object per line (matches Cursor)."""
    assert proc.stdin is not None
    line = json.dumps(obj, separators=(",", ":"), ensure_ascii=False) + "\n"
    proc.stdin.write(line.encode("utf-8"))
    proc.stdin.flush()


def _read_mcp(proc: subprocess.Popen) -> dict | None:
    assert proc.stdout is not None
    line = proc.stdout.readline()
    if not line:
        return None
    raw = line.strip()
    if not raw:
        return None
    if raw.startswith(b"{"):
        return json.loads(raw.decode("utf-8"))
    if raw.lower().startswith(b"content-length:"):
        headers: dict[bytes, bytes] = {}
        name, _, value = raw.partition(b":")
        headers[name.strip().lower()] = value.strip()
        while True:
            line2 = proc.stdout.readline()
            if not line2 or line2 in (b"\r\n", b"\n") or not line2.strip():
                break
            if b":" in line2:
                n2, _, v2 = line2.partition(b":")
                headers[n2.strip().lower()] = v2.strip()
        cl = headers.get(b"content-length")
        if cl is None:
            return None
        n = int(cl)
        body = proc.stdout.read(n)
        if len(body) != n:
            return None
        return json.loads(body.decode("utf-8"))
    return json.loads(raw.decode("utf-8"))


def _drain_stderr(proc: subprocess.Popen, lines: list[str]) -> None:
    assert proc.stderr is not None
    for raw in iter(proc.stderr.readline, b""):
        if not raw:
            break
        lines.append(raw.decode("utf-8", errors="replace"))


def main() -> int:
    root = Path(__file__).resolve().parent
    default_server = root / "hdmi-usb-screenshot-mcp"

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--server",
        type=Path,
        default=default_server,
        help="Path to hdmi-usb-screenshot-mcp (default: next to this script)",
    )
    p.add_argument(
        "--rtsp-url",
        default=os.environ.get("RTSP_URL", "rtsp://127.0.0.1:1234/hdmi"),
        help="RTSP_URL for the child process",
    )
    p.add_argument(
        "--connect-retries",
        type=int,
        default=5,
        help="CONNECT_RETRIES for the child (lower = fail faster)",
    )
    p.add_argument(
        "--frame-wait",
        type=float,
        default=30.0,
        help="Max seconds to poll get_last_frame until a PNG arrives",
    )
    p.add_argument(
        "--poll-interval",
        type=float,
        default=0.5,
        help="Seconds between get_last_frame attempts",
    )
    p.add_argument(
        "--debug-child",
        action="store_true",
        help="Pass --debug to hdmi-usb-screenshot-mcp (verbose stderr)",
    )
    args = p.parse_args()

    server = args.server.resolve()
    if not server.is_file():
        print(f"error: server not found: {server}", file=sys.stderr)
        return 2

    env = os.environ.copy()
    env["RTSP_URL"] = args.rtsp_url
    env["CONNECT_RETRIES"] = str(args.connect_retries)

    cmd = [str(server)]
    if args.debug_child:
        cmd.append("--debug")

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=str(root),
    )

    stderr_lines: list[str] = []
    t_err = threading.Thread(
        target=_drain_stderr,
        args=(proc, stderr_lines),
        daemon=True,
    )
    t_err.start()

    def fail(msg: str, code: int = 1) -> int:
        proc.stdin.close()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        tail = "".join(stderr_lines[-40:])
        if tail.strip():
            print("--- server stderr (tail) ---", file=sys.stderr)
            print(tail.rstrip(), file=sys.stderr)
        print(f"error: {msg}", file=sys.stderr)
        return code

    _next_id = iter(range(1, 10_000))

    def req(method: str, params: dict | None = None) -> int:
        rid = next(_next_id)
        _write_mcp(
            proc,
            {
                "jsonrpc": "2.0",
                "id": rid,
                "method": method,
                **({"params": params} if params is not None else {}),
            },
        )
        return rid

    def expect_result(rid: int, label: str) -> dict:
        msg = _read_mcp(proc)
        if msg is None:
            raise RuntimeError(f"{label}: EOF from server (exit {proc.poll()})")
        if msg.get("id") != rid:
            raise RuntimeError(f"{label}: id mismatch got {msg!r}")
        if "error" in msg:
            raise RuntimeError(f"{label}: {msg['error']}")
        return msg["result"]

    try:
        rid = req(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "test_hdmi_usb_screenshot_mcp", "version": "0.1"},
            },
        )
        init = expect_result(rid, "initialize")
        if init.get("protocolVersion") != "2024-11-05":
            return fail(f"unexpected protocolVersion: {init!r}")

        _write_mcp(
            proc,
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
        )

        rid = req("ping")
        ping_res = expect_result(rid, "ping")
        if ping_res != {}:
            return fail(f"unexpected ping result: {ping_res!r}")

        rid = req("tools/list")
        listed = expect_result(rid, "tools/list")
        names = {t["name"] for t in listed.get("tools", [])}
        if "get_last_frame" not in names:
            return fail(f"get_last_frame not in tools/list: {names!r}")

        deadline = time.monotonic() + args.frame_wait
        png_b64 = None
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                return fail(f"server exited early (code {proc.returncode})")

            rid = req(
                "tools/call",
                {"name": "get_last_frame", "arguments": {}},
            )
            msg = _read_mcp(proc)
            if msg is None:
                return fail("EOF during tools/call")
            if msg.get("id") != rid:
                return fail(f"tools/call id mismatch: {msg!r}")
            if "error" in msg:
                return fail(f"tools/call error: {msg['error']!r}")

            result = msg["result"]
            if result.get("isError"):
                time.sleep(args.poll_interval)
                continue

            for block in result.get("content", []):
                if block.get("type") == "image" and block.get("mimeType") == "image/png":
                    png_b64 = block.get("data")
                    break
            if png_b64:
                break
            time.sleep(args.poll_interval)

        if not png_b64:
            return fail(
                f"no PNG from get_last_frame within {args.frame_wait}s "
                "(is RTSP up and streaming?)"
            )

        raw = base64.b64decode(png_b64, validate=True)
        if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
            return fail("decoded image is not a PNG (bad signature)")

        print("ok: MCP handshake, tools/list, get_last_frame -> valid PNG")
        print(f"    PNG size: {len(raw)} bytes (640x360 expected from server)")
        return 0

    except RuntimeError as e:
        return fail(str(e))
    finally:
        try:
            proc.stdin.close()
        except BrokenPipeError:
            pass
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)


if __name__ == "__main__":
    sys.exit(main())
