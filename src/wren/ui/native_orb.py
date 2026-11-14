"""Frameless 32-bit RGBA floating orb window for X11/GTK3 + WebKit2GTK.

Renders ONLY the glowing plasma orb (and radial menu / preview pill when active)
with true per-pixel alpha transparency, X11 click-through outside the active
hit region, and non-focus-stealing clicks so the user's editor/terminal/browser
never loses keyboard focus when clicking the orb or Paste.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

STAGE_W = 340
STAGE_H = 390
# Orb center sits at (170, 326) inside the 340x390 transparent stage.
ORB_RECT = (120, 276, 100, 100)
RADIAL_RECT = (20, 178, 300, 206)
EXPANDED_RECT = (6, 6, 328, 378)


def _pos_store_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "wren" / "orb-position.json"


def _load_saved_pos() -> tuple[int, int] | None:
    p = _pos_store_path()
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return int(data["x"]), int(data["y"])
    except Exception:
        return None


def _save_pos(x: int, y: int) -> None:
    try:
        p = _pos_store_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"x": int(x), "y": int(y)}), encoding="utf-8")
    except Exception:
        pass


class NativeOrbWindow:
    """Thread-safe controller for the GTK3 + WebKit2 transparent floating orb."""

    def __init__(self, url: str):
        self.url = url
        self._win = None
        self._glib = None
        self._cairo = None
        self._pos_x = 0
        self._pos_y = 0
        self._region_mode = "orb"
        self._save_timer: threading.Timer | None = None

    def set_hit_region(self, mode: str) -> None:
        if mode not in ("orb", "radial", "expanded"):
            mode = "expanded"
        if mode == self._region_mode and self._win is not None:
            return
        self._region_mode = mode
        if self._glib is not None and self._win is not None:
            self._glib.idle_add(self._apply_hit_region, mode)

    def move_by(self, dx: int, dy: int) -> None:
        if self._glib is None or self._win is None:
            return
        self._glib.idle_add(self._apply_move_by, int(dx), int(dy))

    def set_focusable(self, accept: bool) -> None:
        if self._glib is None or self._win is None:
            return
        self._glib.idle_add(self._apply_focusable, bool(accept))

    def _apply_hit_region(self, mode: str) -> bool:
        if self._win is None or self._cairo is None:
            return False
        x, y, w, h = (
            ORB_RECT
            if mode == "orb"
            else (RADIAL_RECT if mode == "radial" else EXPANDED_RECT)
        )
        rect = self._cairo.RectangleInt(x, y, w, h)
        region = self._cairo.Region(rect)
        try:
            self._win.input_shape_combine_region(region)
            gdk_win = self._win.get_window()
            if gdk_win is not None:
                gdk_win.input_shape_combine_region(region, 0, 0)
        except Exception:
            pass
        return False

    def _apply_move_by(self, dx: int, dy: int) -> bool:
        if self._win is None:
            return False
        self._pos_x += dx
        self._pos_y += dy
        self._win.move(self._pos_x, self._pos_y)
        if self._save_timer is not None:
            self._save_timer.cancel()
        self._save_timer = threading.Timer(0.6, _save_pos, args=(self._pos_x, self._pos_y))
        self._save_timer.daemon = True
        self._save_timer.start()
        return False

    def _apply_focusable(self, accept: bool) -> bool:
        if self._win is None:
            return False
        self._win.set_accept_focus(accept)
        if accept:
            self._win.present()
        return False

    def run(self) -> bool:
        """Start GTK main loop with transparent WebKit2 orb. Returns False if GTK unavailable."""
        try:
            import cairo
            import gi

            gi.require_version("Gtk", "3.0")
            gi.require_version("Gdk", "3.0")
            try:
                gi.require_version("WebKit2", "4.1")
            except ValueError:
                gi.require_version("WebKit2", "4.0")
            from gi.repository import Gdk, GLib, Gtk, WebKit2
        except Exception as exc:
            print(f"GTK3/WebKit2 transparent orb unavailable ({exc}); falling back to pywebview")
            return False

        self._glib = GLib
        self._cairo = cairo

        win = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
        win.set_title("Wren Assistant")
        win.set_default_size(STAGE_W, STAGE_H)
        win.set_size_request(STAGE_W, STAGE_H)
        win.set_decorated(False)
        win.set_resizable(False)
        win.set_app_paintable(True)
        win.set_keep_above(True)
        win.set_skip_taskbar_hint(True)
        win.set_skip_pager_hint(True)
        win.set_accept_focus(False)
        win.set_focus_on_map(False)
        win.set_type_hint(Gdk.WindowTypeHint.UTILITY)

        screen = win.get_screen()
        rgba_visual = screen.get_rgba_visual()
        if rgba_visual is not None:
            win.set_visual(rgba_visual)

        def _on_draw(_widget, cr):
            cr.set_source_rgba(0.0, 0.0, 0.0, 0.0)
            cr.set_operator(cairo.OPERATOR_SOURCE)
            cr.paint()
            cr.set_operator(cairo.OPERATOR_OVER)
            return False

        win.connect("draw", _on_draw)
        win.connect("destroy", Gtk.main_quit)

        # Compute initial placement on primary monitor (or restore saved coords)
        display = Gdk.Display.get_default()
        mon = (display.get_primary_monitor() or display.get_monitor(0)) if display else None
        if mon is not None:
            wa = mon.get_workarea()
            default_x = wa.x + wa.width - STAGE_W - 24
            default_y = wa.y + wa.height - STAGE_H - 12
        else:
            default_x, default_y = 1480, 660

        total_w = screen.get_width() if screen else 3840
        total_h = screen.get_height() if screen else 1080
        saved = _load_saved_pos()
        if saved and -100 <= saved[0] <= total_w - 80 and -100 <= saved[1] <= total_h - 80:
            self._pos_x, self._pos_y = saved
        else:
            self._pos_x, self._pos_y = default_x, default_y

        webview = WebKit2.WebView()
        webview.set_background_color(Gdk.RGBA(0.0, 0.0, 0.0, 0.0))
        settings = webview.get_settings()
        settings.set_enable_media_stream(True)
        settings.set_media_playback_requires_user_gesture(False)
        if hasattr(settings, "set_enable_webgl"):
            settings.set_enable_webgl(True)

        def _retry_load(*_args):
            GLib.timeout_add(1200, lambda: (webview.load_uri(self.url), False)[1])
            return False

        webview.connect("load-failed", _retry_load)
        webview.connect("web-process-crashed", _retry_load)

        win.add(webview)
        self._win = win

        def _on_realize(_w):
            win.move(self._pos_x, self._pos_y)
            self._apply_hit_region(self._region_mode)

        win.connect("realize", _on_realize)
        webview.load_uri(self.url)
        win.show_all()
        win.move(self._pos_x, self._pos_y)
        self._apply_hit_region(self._region_mode)

        Gtk.main()
        return True
