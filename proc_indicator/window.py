"""浮窗：Wayland 上用 wlr-layer-shell（gtk4-layer-shell），X11 上回退到 override-redirect 窗口。

窗口是透明的竖条，贴在屏幕右下角，输入区域为空（点击穿透），不接收键盘焦点。
所有卡片在一个 DrawingArea 里用 cairo 绘制；动画只在需要时通过 frame clock 驱动。
"""
from __future__ import annotations

import os
import time

import cairo
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_foreign("cairo")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from .icons import IconCache  # noqa: E402
from .render import Style, render_card  # noqa: E402


DEBUG_CURSOR = bool(os.environ.get("PROC_INDICATOR_DEBUG_CURSOR"))


def _clamp01(t):
    return 0.0 if t <= 0 else (1.0 if t >= 1 else t)


def ease_out_cubic(t):
    t = _clamp01(t)
    return 1 - (1 - t) ** 3


def ease_out_back(t, k=1.2):
    """带一点点回弹的减速曲线（滑入用）"""
    t = _clamp01(t) - 1
    return 1 + (k + 1) * t ** 3 + k * t ** 2


def ease_in_cubic(t):
    t = _clamp01(t)
    return t * t * t


def ease_in_out_cubic(t):
    t = _clamp01(t)
    return 4 * t ** 3 if t < 0.5 else 1 - (-2 * t + 2) ** 3 / 2


class Card:
    __slots__ = ("surf", "born", "y0", "y1", "ty0", "fade_start", "fade_len", "timer", "kind",
                 "out_start", "hx", "hx0", "hx1", "thx0", "hovered")

    def __init__(self, surf, kind, now):
        self.surf = surf
        self.kind = kind
        self.born = now          # 开始滑入的时间
        self.y0 = self.y1 = None
        self.ty0 = now
        self.fade_start = None   # 被挤掉 → 渐隐
        self.fade_len = 0.8
        self.out_start = None    # 正常到时 → 向右滑出
        self.timer = 0
        self.hx = 0.0            # 悬停避让的水平偏移（缓动）
        self.hx0 = self.hx1 = 0.0
        self.thx0 = now
        self.hovered = False


class Overlay:
    def __init__(self, app: Gtk.Application, cfg, layer_shell=None):
        self.app = app
        self.layer = layer_shell     # Gtk4LayerShell 模块或 None
        self.cards: list[Card] = []  # 最旧的在前
        self.tick_id = 0
        self.x11 = None
        self.xid = 0
        self.frames = 0
        self.cursor = None
        self.cursor_pos = None       # 相对浮窗左上角的逻辑坐标
        self.x11_origin = (0, 0, 1)
        self.win = Gtk.Window(application=app)
        self.win.set_title("proc-indicator")
        self.win.set_decorated(False)
        self.win.set_resizable(False)
        self.win.set_focusable(False)
        self.win.set_can_focus(False)
        self.win.set_can_target(False)
        self.win.add_css_class("proc-indicator")
        css = Gtk.CssProvider()
        css.load_from_data(b"window.proc-indicator, window.proc-indicator > * "
                           b"{ background: transparent; box-shadow: none; border: none; }", -1)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 10)
        self.area = Gtk.DrawingArea()
        self.area.set_can_target(False)
        self.area.set_draw_func(self._draw)
        self.win.set_child(self.area)
        self.apply_config(cfg, first=True)
        self.win.connect("realize", self._on_realize)
        self.win.connect("map", self._on_map)

    # ---------- 配置 ----------
    def apply_config(self, cfg, first=False):
        self.cfg = cfg
        self.style = Style(cfg)
        self.timeout = cfg.num("behavior", "timeout", 6.0, 0.5, 600.0)
        self.fade = cfg.num("behavior", "fade_out", 0.8, 0.05, 10.0)
        self.slide_in = cfg.num("behavior", "slide_in", 0.45, 0.05, 3.0)
        self.slide_out = cfg.num("behavior", "slide_out", 0.45, 0.05, 3.0)
        self.move = cfg.num("behavior", "move", 0.25, 0.05, 3.0)
        self.slide_delay = cfg.num("behavior", "slide_delay", 0.08, 0.0, 1.0)  # 旧卡片先让出位置，新卡片再滑入，避免重叠
        self.max_cards = cfg.num("behavior", "max_cards", 5, 1, 200)
        self.hover_enabled = bool(cfg.get("behavior", "hover_avoid", True))
        self.hover_shift = cfg.num("behavior", "hover_shift", 260, 20, 2000)
        self.hover_time = cfg.num("behavior", "hover_duration", 0.22, 0.05, 2.0)
        st = self.style
        self.right_space = max(st.pad, st.margin_right)
        self.bottom_space = max(st.pad, st.margin_bottom)
        # 窗口宽度 = 卡片 + 两侧发光留白。卡片向右滑出/避让时超出窗口右边（= 屏幕右边缘）的部分直接被裁掉。
        self.width = int(st.pad + st.w + self.right_space)
        if getattr(self, "cursor", None) is not None:
            self.cursor.set_band(self.width)
        theme = str(cfg.get("appearance", "icon_theme", "") or "")
        if theme:
            Gtk.Settings.get_default().set_property("gtk-icon-theme-name", theme)
        scale = self.win.get_scale_factor() if not first else 1
        self.icons = IconCache(st.icon, max(1, scale))
        if first:
            self.win.set_default_size(self.width, 600)
        else:
            self.win.set_default_size(self.width, self.win.get_height() or 600)
            self.win.set_size_request(self.width, -1)
            self._place_x11()

    # ---------- 平台相关 ----------
    def setup_layer_shell(self):
        L = self.layer
        # 不能设成不可调整大小，否则 GTK 会忽略合成器给的高度（上下都锚定时高度由合成器决定）
        self.win.set_resizable(True)
        self.win.set_default_size(self.width, 0)
        L.init_for_window(self.win)
        L.set_namespace(self.win, "proc-indicator")
        L.set_layer(self.win, L.Layer.OVERLAY)
        L.set_keyboard_mode(self.win, L.KeyboardMode.NONE)
        for edge in (L.Edge.RIGHT, L.Edge.BOTTOM, L.Edge.TOP):
            L.set_anchor(self.win, edge, True)
        # exclusive zone = 0：会避开任务栏等面板的独占区域，所以卡片“贴着任务栏往上堆”
        L.set_exclusive_zone(self.win, 0)
        idx = self.cfg.num("appearance", "monitor", -1)
        mons = Gdk.Display.get_default().get_monitors()
        if 0 <= idx < mons.get_n_items():
            L.set_monitor(self.win, mons.get_item(idx))

    def _on_realize(self, win):
        surface = win.get_surface()
        surface.connect("layout", lambda *a: self._clear_input())
        if self.layer is None:
            try:
                gi.require_version("GdkX11", "4.0")
                from gi.repository import GdkX11
                if isinstance(surface, GdkX11.X11Surface):
                    from .x11util import X11
                    self.xid = surface.get_xid()
                    Gdk.Display.get_default().sync()
                    self.x11 = X11()
                    self.x11.override_redirect(self.xid)
            except Exception as e:  # noqa: BLE001
                print("[proc-indicator] X11 定位初始化失败:", e)

    def _on_map(self, win):
        self._clear_input()
        self._place_x11()

    def _clear_input(self):
        surface = self.win.get_surface()
        if surface is not None:
            # 空的输入区域 = 鼠标点击直接穿透到下面的窗口（X11 用 XShape，Wayland 用 wl_surface.set_input_region）
            surface.set_input_region(cairo.Region())

    def _monitor_geometry(self):
        disp = Gdk.Display.get_default()
        mons = disp.get_monitors()
        idx = self.cfg.num("appearance", "monitor", -1)
        mon = None
        if 0 <= idx < mons.get_n_items():
            mon = mons.get_item(idx)
        else:
            try:
                mon = disp.get_primary_monitor()
            except Exception:
                mon = None
            mon = mon or mons.get_item(0)
        try:
            gi.require_version("GdkX11", "4.0")
            from gi.repository import GdkX11
            if isinstance(mon, GdkX11.X11Monitor):
                return mon.get_workarea()  # 去掉任务栏后的可用区域
        except Exception:
            pass
        return mon.get_geometry()

    def _place_x11(self):
        if not (self.x11 and self.xid):
            return
        wa = self._monitor_geometry()
        h = wa.height
        self.win.set_default_size(self.width, h)
        self.win.set_size_request(self.width, h)
        sc = max(1, self.win.get_scale_factor())
        self.x11_origin = ((wa.x + wa.width - self.width) * sc, wa.y * sc, sc)
        self.x11.move(self.xid, self.x11_origin[0], self.x11_origin[1])
        self.x11.raise_(self.xid)

    def show(self):
        if self.layer is not None:
            self.setup_layer_shell()
        else:
            try:
                wa = self._monitor_geometry()
                self.win.set_default_size(self.width, wa.height)
            except Exception:
                pass
        self.win.present()

    # ---------- 光标（悬停避让） ----------
    def start_cursor(self):
        mode = str(self.cfg.get("behavior", "cursor_source", "auto"))
        if not self.hover_enabled:
            mode = "none"
        from . import cursor as cur
        try:
            self.cursor = cur.create(mode, x11=self.x11, win_origin=lambda: self.x11_origin,
                                     window_info=self._window_info, on_cursor=self._on_cursor,
                                     band=self.width)
        except Exception as e:  # noqa: BLE001  拿不到就安静降级
            print("[proc-indicator] 悬停避让初始化失败，已关闭：", e)
            self.cursor = cur.NullCursor()

    def _window_info(self):
        mon = None
        surface = self.win.get_surface()
        if surface is not None:
            try:
                m = Gdk.Display.get_default().get_monitor_at_surface(surface)
                mon = m.get_geometry() if m else None
            except Exception:
                mon = None
        return self.width, mon

    def _on_cursor(self, x, y):
        self.cursor_pos = None if x is None else (x, y)
        if DEBUG_CURSOR:
            print("[proc-indicator] cursor", self.cursor_pos, flush=True)
        self._update_hover(time.monotonic())

    def _update_hover(self, now):
        """命中测试用卡片的“原位”（不含避让偏移），所以卡片让开后光标仍在原位就保持让开，不会来回抖。"""
        st = self.style
        changed = False
        cp = self.cursor_pos if self.hover_enabled else None
        for c in self.cards:
            hov = False
            if cp is not None and c.out_start is None and c.fade_start is None:
                y = self._cur_y(c, now) + st.pad
                x = st.pad
                hov = x <= cp[0] <= x + st.w and y <= cp[1] <= y + st.h
            if hov != c.hovered:
                c.hovered = hov
                c.hx0 = c.hx
                c.hx1 = self.hover_shift if hov else 0.0
                c.thx0 = now
                changed = True
        if changed:
            self._ensure_tick()

    # ---------- 卡片 ----------
    def _live(self):
        return [c for c in self.cards if c.fade_start is None and c.out_start is None]

    def _capacity(self):
        st = self.style
        h = self.area.get_height() or 600
        fit = int((h - st.pad - self.bottom_space + st.gap) // (st.h + st.gap))
        return max(1, min(self.max_cards, fit))

    def _target_y(self, k):
        """第 k 张（0 = 最新/最底部）卡片的 surface 左上角 y。"""
        st = self.style
        h = self.area.get_height() or 600
        return h - self.bottom_space - st.h - k * (st.h + st.gap) - st.pad

    def add(self, ev):
        st = self.style
        now = time.monotonic()
        scale = max(1, self.win.get_scale_factor())
        if self.icons.scale != scale:
            self.icons = IconCache(st.icon, scale)
        icon = None
        if ev.info is not None:
            i = ev.info
            icon = self.icons.get([i.display_name, i.exe_name, i.argv0_name, i.comm])
        elif ev.kind == "merged":
            icon = self.icons.get(["view-list-symbolic", "dialog-information"])
        surf = render_card(st, ev.kind, ev.name, ev.detail, icon, scale)
        card = Card(surf, ev.kind, now + self.slide_delay)
        card.fade_len = self.fade
        # 超过上限：最旧的几张走渐隐（不是向右滑出）
        live = self._live()
        cap = self._capacity()
        for c in live[:max(0, len(live) + 1 - cap)]:
            self._start_fade(c, now, min(self.fade, 0.35))
        self.cards.append(card)
        self._relayout(now, new=card)
        card.timer = GLib.timeout_add(int(self.timeout * 1000), self._expire, card)
        if self.x11 and self.xid:
            self.x11.raise_(self.xid)
        if self.cursor is not None:
            self.cursor.set_active(True)
        self._ensure_tick()

    def _relayout(self, now, new=None):
        """离开中的卡片（滑出/渐隐）保持原位，其余按新旧顺序从底部往上排。"""
        stack = [c for c in self.cards if c.out_start is None and c.fade_start is None]
        n = len(stack)
        for idx, c in enumerate(stack):
            target = self._target_y(n - 1 - idx)
            if c is new:
                c.y0 = c.y1 = target       # 新卡片直接在最底部的位置，从右侧水平滑入
                c.ty0 = now
            elif c.y1 != target:
                c.y0 = self._cur_y(c, now)
                c.y1 = target
                c.ty0 = now

    def _cur_y(self, c, now):
        if c.y0 is None:
            return c.y1
        return c.y0 + (c.y1 - c.y0) * ease_out_cubic((now - c.ty0) / self.move)

    def _cur_x(self, c, now):
        """水平偏移：滑入 + 悬停避让 + 滑出。正数 = 向右。"""
        off = self.width  # 完全在屏幕右边缘之外
        x = off * (1.0 - ease_out_cubic((now - c.born) / self.slide_in))
        if c.hx0 != c.hx1:
            c.hx = c.hx0 + (c.hx1 - c.hx0) * ease_in_out_cubic((now - c.thx0) / self.hover_time)
        else:
            c.hx = c.hx1
        x += c.hx
        if c.out_start is not None and now >= c.out_start:
            x += (off - x) * ease_in_cubic((now - c.out_start) / self.slide_out)
        return x

    def _start_fade(self, c, now, length=None):
        if c.fade_start is None:
            c.fade_start = now
            if length is not None:
                c.fade_len = min(c.fade_len, length)
        if c.timer:
            GLib.source_remove(c.timer)
            c.timer = 0

    def _expire(self, card):
        card.timer = 0
        if card.fade_start is None and card.out_start is None:
            card.out_start = time.monotonic()
            self._ensure_tick()
        return False

    def _ensure_tick(self):
        if not self.tick_id:
            self.tick_id = self.area.add_tick_callback(self._tick)
        self.area.queue_draw()

    def _animating(self, now):
        for c in self.cards:
            if c.fade_start is not None or c.out_start is not None:
                return True
            if now - c.ty0 < self.move or now - c.born < self.slide_in:
                return True
            if c.hx0 != c.hx1 and now - c.thx0 < self.hover_time:
                return True
        return False

    def _tick(self, widget, clock):
        now = time.monotonic()
        before = len(self.cards)
        keep = []
        for c in self.cards:
            if c.fade_start is not None and now - c.fade_start >= c.fade_len:
                continue
            if c.out_start is not None and now - c.out_start >= self.slide_out:
                continue
            keep.append(c)
        self.cards = keep
        if len(self.cards) != before:
            self._relayout(now)      # 剩下的卡片平滑补位
            if not self.cards and self.cursor is not None:
                self.cursor.set_active(False)
        self._update_hover(now)      # 卡片在移动时，光标没动也要重新判断
        self.area.queue_draw()
        if not self._animating(now):
            self.tick_id = 0
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def _draw(self, area, cr, w, h):
        self.frames += 1
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)
        now = time.monotonic()
        for c in self.cards:
            y = self._cur_y(c, now)
            x = self._cur_x(c, now)
            a = 0.35 + 0.65 * ease_out_cubic((now - c.born) / (self.slide_in * 0.6))
            if c.fade_start is not None and now >= c.fade_start:
                a *= max(0.0, 1.0 - (now - c.fade_start) / c.fade_len)
            if a <= 0.003 or x >= w or y + c.surf.get_height() < 0:
                continue
            cr.set_source_surface(c.surf, round(x), y)
            cr.paint_with_alpha(min(1.0, a))
