"""MCP server wrapping the linux-computer-use bridge.

Exposes the same 8 tools (list_windows, screenshot, click, type_text, set_text,
keypress, scroll, computer_actions) over the Model Context Protocol so any
MCP-aware agent (Claude Code, OpenCode, …) can drive Linux/X11 the same way
the Pi extension does.
"""
from __future__ import annotations

import base64
import itertools
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP, Image

# --- locate bridge ---------------------------------------------------------

_HERE = Path(__file__).resolve().parent
_BRIDGE = _HERE.parent / "bridge" / "bridge.py"
if not _BRIDGE.exists():  # pragma: no cover
    raise RuntimeError(f"bridge.py not found at {_BRIDGE}")

# --- subprocess singleton --------------------------------------------------

_proc: subprocess.Popen | None = None
_lock = threading.Lock()
_id_seq = itertools.count(1)


def _python() -> str:
    # Prefer system python3 — bridge needs PyGObject (gi), which venvs lack.
    return "/usr/bin/python3" if Path("/usr/bin/python3").exists() else (shutil.which("python3") or "python3")


def _spawn() -> subprocess.Popen:
    return subprocess.Popen(
        [_python(), str(_BRIDGE)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env=os.environ.copy(),
    )


def _bridge_call(cmd: str, args: dict[str, Any]) -> Any:
    global _proc
    with _lock:
        if _proc is None or _proc.poll() is not None:
            _proc = _spawn()
        rid = str(next(_id_seq))
        req = {"id": rid, "cmd": cmd, **args}
        assert _proc.stdin and _proc.stdout
        _proc.stdin.write(json.dumps(req) + "\n")
        _proc.stdin.flush()
        line = _proc.stdout.readline()
        if not line:
            err = ""
            if _proc.stderr:
                try:
                    err = _proc.stderr.read() or ""
                except Exception:
                    pass
            raise RuntimeError(f"bridge died: {err.strip()}")
        resp = json.loads(line)
        if not resp.get("ok"):
            raise RuntimeError(resp.get("error", "bridge error"))
        return resp.get("result")


# --- server ----------------------------------------------------------------

mcp = FastMCP("linux-computer-use")


@mcp.tool()
def list_windows() -> dict:
    """Enumerate visible X11 windows with @wN refs, titles, pids, geometry, focus."""
    return _bridge_call("list_windows", {})


@mcp.tool()
def screenshot(window: str = "") -> list:
    """Capture window PNG + AT-SPI @eN targets. Empty window = focused."""
    res = _bridge_call("screenshot", {"window": window} if window else {})
    png_b64 = res.pop("pngBase64", "")
    parts: list = [res]
    if png_b64:
        try:
            parts.append(Image(data=base64.b64decode(png_b64), format="png"))
        except Exception:
            # Fallback: write to /tmp and include path.
            path = f"/tmp/lcu-screenshot-{res.get('stateId','x')}.png"
            try:
                Path(path).write_bytes(base64.b64decode(png_b64))
                res["imagePath"] = path
            except Exception:
                pass
    return parts


@mcp.tool()
def click(ref: str = "", x: int = -1, y: int = -1, button: str = "left", click_count: int = 1) -> dict:
    """Click @eN, @wN, or absolute x,y. button=left|middle|right."""
    args: dict[str, Any] = {"button": button, "clickCount": click_count}
    if ref:
        args["ref"] = ref
    if x >= 0:
        args["x"] = x
    if y >= 0:
        args["y"] = y
    return _bridge_call("click", args)


@mcp.tool()
def type_text(text: str) -> dict:
    """Type literal text at the current cursor."""
    return _bridge_call("type_text", {"text": text})


@mcp.tool()
def set_text(ref: str, text: str) -> dict:
    """Replace value of an @eN text/entry via AT-SPI (falls back to ctrl+a + type)."""
    return _bridge_call("set_text", {"ref": ref, "text": text})


@mcp.tool()
def keypress(keys: list[str]) -> dict:
    """Press keys/chords: ['Enter'], ['ctrl','a'], ['ctrl+l','Return']."""
    return _bridge_call("keypress", {"keys": keys})


@mcp.tool()
def scroll(ref: str = "", x: int = -1, y: int = -1, scroll_x: int = 0, scroll_y: int = 0) -> dict:
    """Scroll at ref/coords by pixel delta."""
    args: dict[str, Any] = {"scrollX": scroll_x, "scrollY": scroll_y}
    if ref:
        args["ref"] = ref
    if x >= 0:
        args["x"] = x
    if y >= 0:
        args["y"] = y
    return _bridge_call("scroll", args)


@mcp.tool()
def computer_actions(actions: list[dict]) -> dict:
    """Batch multiple actions ({type:click|type_text|set_text|keypress|scroll, ...})."""
    return _bridge_call("computer_actions", {"actions": actions})


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
