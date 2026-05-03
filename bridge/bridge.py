#!/usr/bin/env python3
"""Linux/X11 bridge for linux-computer-use.

Newline-delimited JSON protocol over stdio. Each request:
  {"id": "...", "cmd": "...", ...args}
Response:
  {"id": "...", "ok": true, "result": ...}  or  {"id": "...", "ok": false, "error": "..."}
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from typing import Any

# Optional AT-SPI
_ATSPI = None
try:
    import gi  # type: ignore
    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi  # type: ignore
    _ATSPI = Atspi
except Exception as e:  # pragma: no cover
    print(f"[pcul] AT-SPI not available: {e}", file=sys.stderr)

WINDOWS: dict[str, dict] = {}      # @wN -> info
ELEMENTS: dict[str, dict] = {}     # @eN -> {x,y,w,h, role, name, atspi (opt)}
_WID_SEQ = 0
_EID_SEQ = 0

INTERESTING_ROLES = {
    "push button", "toggle button", "link", "text", "entry", "password text",
    "list item", "menu item", "check box", "radio button", "combo box",
    "tab", "slider", "spin button", "tree item", "table cell",
}
WALK_MAX_DEPTH = 12
WALK_MAX_ELEMENTS = 200


def log(msg: str) -> None:
    print(f"[pcul] {msg}", file=sys.stderr, flush=True)


def run(cmd: list[str], timeout: float = 10.0, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=check)


# ---------- windows ----------

def _next_wid() -> str:
    global _WID_SEQ
    _WID_SEQ += 1
    return f"@w{_WID_SEQ}"


def get_active_window_id() -> str | None:
    r = run(["xdotool", "getactivewindow"])
    return r.stdout.strip() or None


def list_windows() -> list[dict]:
    global WINDOWS
    WINDOWS = {}
    out = run(["wmctrl", "-lpG"]).stdout
    active = get_active_window_id()
    result = []
    for line in out.splitlines():
        parts = line.split(None, 8)
        if len(parts) < 9:
            continue
        wid_hex, _desk, pid, x, y, w, h, _host, title = parts
        try:
            wid_int = int(wid_hex, 16)
            pid_int = int(pid)
            x, y, w, h = int(x), int(y), int(w), int(h)
        except ValueError:
            continue
        ref = _next_wid()
        info = {
            "ref": ref,
            "wid": wid_int,
            "title": title,
            "pid": pid_int,
            "x": x, "y": y, "w": w, "h": h,
            "isFocused": active is not None and int(active) == wid_int,
        }
        WINDOWS[ref] = info
        result.append({k: v for k, v in info.items() if k != "wid"})
    return result


def resolve_window(ref: str | None) -> dict | None:
    if not ref:
        return None
    if not WINDOWS:
        list_windows()
    return WINDOWS.get(ref)


def focus_window(ref: str) -> None:
    info = resolve_window(ref)
    if not info:
        raise ValueError(f"unknown window ref {ref}")
    run(["xdotool", "windowactivate", "--sync", str(info["wid"])])
    time.sleep(0.05)


# ---------- AT-SPI walk ----------

def _walk_atspi(pid: int, win_x: int = 0, win_y: int = 0) -> list[dict]:
    if _ATSPI is None:
        return []
    out: list[dict] = []
    try:
        desktop = _ATSPI.get_desktop(0)
        n_apps = desktop.get_child_count()
        target_app = None
        for i in range(n_apps):
            try:
                app = desktop.get_child_at_index(i)
                if app is None:
                    continue
                if app.get_process_id() == pid:
                    target_app = app
                    break
            except Exception:
                continue
        if target_app is None:
            return []

        def visit(node, depth: int) -> None:
            if len(out) >= WALK_MAX_ELEMENTS or depth > WALK_MAX_DEPTH:
                return
            try:
                role = node.get_role_name()
            except Exception:
                role = ""
            try:
                name = node.get_name() or ""
            except Exception:
                name = ""
            if role in INTERESTING_ROLES:
                try:
                    ext = node.get_extents(_ATSPI.CoordType.SCREEN)
                    ex_x, ex_y, ex_w, ex_h = ext.x, ext.y, ext.width, ext.height
                    # Some toolkits (GTK4, Xwayland) report SCREEN as 0,0; fall back to WINDOW coords + window offset.
                    if (ex_x == 0 and ex_y == 0) or ex_w == 0 or ex_h == 0:
                        try:
                            ew = node.get_extents(_ATSPI.CoordType.WINDOW)
                            if ew.width > 0 and ew.height > 0:
                                ex_x, ex_y, ex_w, ex_h = ew.x + win_x, ew.y + win_y, ew.width, ew.height
                        except Exception:
                            pass
                    if ex_w > 0 and ex_h > 0:
                        states = set()
                        try:
                            states = {s.value_name for s in node.get_state_set().get_states()}
                        except Exception:
                            pass
                        actions: set[str] = set()
                        try:
                            ai = node.get_action_iface()
                            if ai is not None:
                                for k in range(ai.get_n_actions()):
                                    nm_k = ""
                                    try:
                                        nm_k = _ATSPI.Action.get_action_name(ai, k) or ""
                                    except Exception:
                                        try:
                                            nm_k = ai.get_name(k) or ""
                                        except Exception:
                                            pass
                                    actions.add(nm_k.lower())
                        except Exception:
                            pass
                        out.append({
                            "role": role,
                            "name": name[:120],
                            "x": ex_x, "y": ex_y, "w": ex_w, "h": ex_h,
                            "canPress": "press" in actions or "click" in actions or "activate" in actions,
                            "canSetValue": role in {"text", "entry", "password text", "spin button", "combo box"},
                            "canFocus": "ATSPI_STATE_FOCUSABLE" in states or True,
                            "_node": node,
                        })
                except Exception:
                    pass
            try:
                nc = node.get_child_count()
            except Exception:
                nc = 0
            for j in range(nc):
                if len(out) >= WALK_MAX_ELEMENTS:
                    return
                try:
                    child = node.get_child_at_index(j)
                except Exception:
                    continue
                if child is not None:
                    visit(child, depth + 1)

        visit(target_app, 0)
    except Exception as e:
        log(f"atspi walk failed: {e}")
    return out


def assign_elements(walk: list[dict]) -> list[dict]:
    global ELEMENTS, _EID_SEQ
    ELEMENTS = {}
    _EID_SEQ = 0
    public = []
    for el in walk:
        _EID_SEQ += 1
        ref = f"@e{_EID_SEQ}"
        ELEMENTS[ref] = el
        public.append({
            "ref": ref,
            "role": el["role"], "name": el["name"],
            "x": el["x"], "y": el["y"], "w": el["w"], "h": el["h"],
            "canPress": el["canPress"],
            "canSetValue": el["canSetValue"],
            "canFocus": el["canFocus"],
        })
    return public


# ---------- screenshot ----------

def screenshot(window_ref: str | None) -> dict:
    if window_ref:
        focus_window(window_ref)
    win = resolve_window(window_ref) if window_ref else None
    state_id = uuid.uuid4().hex[:12]
    path = f"/tmp/pcul-{state_id}.png"
    if win:
        # capture full screen then we expose window bounds; scrot autoselect can be flaky
        # We crop using scrot -a x,y,w,h
        x, y, w, h = win["x"], win["y"], win["w"], win["h"]
        run(["scrot", "-o", "-a", f"{x},{y},{w},{h}", path], timeout=5.0)
        width, height = w, h
    else:
        run(["scrot", "-o", path], timeout=5.0)
        # parse with file
        width = height = 0
        try:
            with open(path, "rb") as f:
                data = f.read(32)
                if data[:8] == b"\x89PNG\r\n\x1a\n":
                    width = int.from_bytes(data[16:20], "big")
                    height = int.from_bytes(data[20:24], "big")
        except Exception:
            pass

    with open(path, "rb") as f:
        png = f.read()
    try:
        os.remove(path)
    except OSError:
        pass

    pid = win["pid"] if win else 0
    wx = win["x"] if win else 0
    wy = win["y"] if win else 0
    walk = _walk_atspi(pid, wx, wy) if pid else []
    targets = assign_elements(walk)
    return {
        "stateId": state_id,
        "pngBase64": base64.b64encode(png).decode("ascii"),
        "width": width,
        "height": height,
        "axTargets": targets,
        "window": {k: v for k, v in win.items() if k != "wid"} if win else None,
    }


# ---------- input ----------

def _ref_center(ref: str) -> tuple[int, int]:
    if ref.startswith("@e"):
        el = ELEMENTS.get(ref)
        if not el:
            raise ValueError(f"unknown element ref {ref}")
        return el["x"] + el["w"] // 2, el["y"] + el["h"] // 2
    if ref.startswith("@w"):
        w = WINDOWS.get(ref)
        if not w:
            raise ValueError(f"unknown window ref {ref}")
        return w["x"] + w["w"] // 2, w["y"] + w["h"] // 2
    raise ValueError(f"bad ref {ref}")


_BTN = {"left": "1", "middle": "2", "right": "3"}


def click(ref: str | None, x: float | None, y: float | None,
          button: str = "left", click_count: int = 1) -> dict:
    if ref:
        cx, cy = _ref_center(ref)
    elif x is not None and y is not None:
        cx, cy = int(x), int(y)
    else:
        raise ValueError("click requires ref or x,y")
    btn = _BTN.get(button, "1")
    cmd = ["xdotool", "mousemove", str(cx), str(cy)]
    for _ in range(max(1, int(click_count))):
        cmd += ["click", btn]
    run(cmd)
    return {"x": cx, "y": cy, "button": button, "clickCount": click_count}


def type_text(text: str) -> dict:
    run(["xdotool", "type", "--delay", "8", "--", text])
    return {"typed": len(text)}


def set_text(ref: str, text: str) -> dict:
    used_atspi = False
    if ref.startswith("@e") and _ATSPI is not None:
        el = ELEMENTS.get(ref)
        if el is not None:
            node = el.get("_node")
            try:
                eti = node.get_editable_text_iface() if node is not None else None
                if eti is not None:
                    # clear then set
                    try:
                        ti = node.get_text_iface()
                        cur_len = ti.get_character_count() if ti is not None else 0
                        if cur_len > 0:
                            eti.delete_text(0, cur_len)
                    except Exception:
                        pass
                    eti.set_text_contents(text)
                    used_atspi = True
            except Exception as e:
                log(f"atspi set_text failed: {e}")
    if not used_atspi:
        # focus + ctrl-a + type
        if ref.startswith("@e"):
            cx, cy = _ref_center(ref)
            run(["xdotool", "mousemove", str(cx), str(cy), "click", "1"])
        run(["xdotool", "key", "ctrl+a"])
        run(["xdotool", "key", "Delete"])
        run(["xdotool", "type", "--delay", "8", "--", text])
    return {"used": "atspi" if used_atspi else "fallback"}


_KEY_MAP = {
    "enter": "Return", "return": "Return",
    "esc": "Escape", "escape": "Escape",
    "tab": "Tab", "space": "space", "backspace": "BackSpace",
    "delete": "Delete", "del": "Delete",
    "up": "Up", "down": "Down", "left": "Left", "right": "Right",
    "home": "Home", "end": "End", "pageup": "Prior", "pagedown": "Next",
    "ctrl": "ctrl", "control": "ctrl",
    "shift": "shift", "alt": "alt", "meta": "super", "super": "super", "cmd": "super", "command": "super",
}


def _xdo_key(token: str) -> str:
    parts = token.replace(" ", "").split("+")
    out = []
    for p in parts:
        lp = p.lower()
        out.append(_KEY_MAP.get(lp, p if len(p) > 1 else lp))
    return "+".join(out)


def keypress(keys: list[str]) -> dict:
    if not keys:
        raise ValueError("keypress needs keys")
    # Heuristic: if all entries are modifier-like + last is non-modifier, treat as one chord
    mods = {"ctrl", "control", "shift", "alt", "meta", "super", "cmd", "command"}
    if len(keys) > 1 and all(k.lower() in mods for k in keys[:-1]):
        chord = "+".join([_KEY_MAP.get(k.lower(), k.lower()) for k in keys[:-1]])
        last = keys[-1]
        chord = chord + "+" + _KEY_MAP.get(last.lower(), last if len(last) > 1 else last.lower())
        run(["xdotool", "key", chord])
    else:
        for k in keys:
            run(["xdotool", "key", _xdo_key(k)])
    return {"keys": keys}


def scroll(ref: str | None, x: float | None, y: float | None,
           scrollY: float = 0, scrollX: float = 0) -> dict:
    if ref:
        cx, cy = _ref_center(ref)
    elif x is not None and y is not None:
        cx, cy = int(x), int(y)
    else:
        cx = cy = None
    if cx is not None:
        run(["xdotool", "mousemove", str(cx), str(cy)])
    ticks_y = int(abs(scrollY) // 40) or (1 if scrollY else 0)
    ticks_x = int(abs(scrollX) // 40) or (1 if scrollX else 0)
    btn_y = "5" if scrollY > 0 else "4"
    btn_x = "7" if scrollX > 0 else "6"
    for _ in range(ticks_y):
        run(["xdotool", "click", btn_y])
    for _ in range(ticks_x):
        run(["xdotool", "click", btn_x])
    return {"ticksY": ticks_y, "ticksX": ticks_x}


# ---------- batched ----------

def computer_actions(actions: list[dict]) -> dict:
    trace = []
    for a in actions:
        t = a.get("type")
        try:
            if t == "click":
                r = click(a.get("ref"), a.get("x"), a.get("y"),
                          a.get("button", "left"), a.get("clickCount", 1))
            elif t == "type_text":
                r = type_text(a["text"])
            elif t == "set_text":
                r = set_text(a["ref"], a["text"])
            elif t == "keypress":
                r = keypress(a["keys"])
            elif t == "scroll":
                r = scroll(a.get("ref"), a.get("x"), a.get("y"),
                           a.get("scrollY", 0), a.get("scrollX", 0))
            else:
                raise ValueError(f"unknown action type {t}")
            trace.append({"type": t, "ok": True, "result": r})
        except Exception as e:
            trace.append({"type": t, "ok": False, "error": str(e)})
    return {"trace": trace}


# ---------- dispatch ----------

def handle(req: dict) -> dict:
    cmd = req.get("cmd")
    if cmd == "list_windows":
        return {"windows": list_windows()}
    if cmd == "screenshot":
        return screenshot(req.get("window"))
    if cmd == "click":
        return click(req.get("ref"), req.get("x"), req.get("y"),
                     req.get("button", "left"), req.get("clickCount", 1))
    if cmd == "type_text":
        return type_text(req["text"])
    if cmd == "set_text":
        return set_text(req["ref"], req["text"])
    if cmd == "keypress":
        return keypress(req["keys"])
    if cmd == "scroll":
        return scroll(req.get("ref"), req.get("x"), req.get("y"),
                      req.get("scrollY", 0), req.get("scrollX", 0))
    if cmd == "computer_actions":
        return computer_actions(req["actions"])
    if cmd == "ping":
        return {"pong": True}
    raise ValueError(f"unknown cmd {cmd}")


def main() -> int:
    for tool in ("xdotool", "wmctrl", "scrot"):
        if shutil.which(tool) is None:
            log(f"warning: {tool} not on PATH")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as e:
            sys.stdout.write(json.dumps({"id": None, "ok": False, "error": f"bad json: {e}"}) + "\n")
            sys.stdout.flush()
            continue
        rid = req.get("id")
        try:
            result = handle(req)
            sys.stdout.write(json.dumps({"id": rid, "ok": True, "result": result}) + "\n")
        except Exception as e:
            sys.stdout.write(json.dumps({"id": rid, "ok": False, "error": str(e)}) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
