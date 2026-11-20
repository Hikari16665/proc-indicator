"""卡片绘制（cairo + Pango）。每张卡片只在创建时画一次，缓存成 surface，动画时只做贴图 + 透明度。"""
from __future__ import annotations

import colorsys
import math

import cairo
import gi

gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import GLib, Pango, PangoCairo  # noqa: E402

LABELS = {"create": "创建", "exit": "退出", "exec": "执行", "merged": "合并"}


def rounded_rect(cr, x, y, w, h, r):
    r = min(r, w / 2, h / 2)
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


def _shift_hue(c):
    """参考视频里填充是渐变的（橙→黄、青→蓝绿），这里把主色转一点色相得到第二色。"""
    h, l, sat = colorsys.rgb_to_hls(*c)
    h = (h - 0.06) % 1.0 if 0.40 <= h <= 0.70 else (h + 0.045) % 1.0
    return colorsys.hls_to_rgb(h, min(0.66, max(l, 0.55) + 0.06), min(1.0, sat + 0.1))


def _mix(c, t, k):
    return tuple(a + (b - a) * k for a, b in zip(c, t))


class Style:
    def __init__(self, cfg):
        a = lambda k, d, lo=None, hi=None: cfg.num("appearance", k, d, lo, hi)  # noqa: E731
        self.w = a("card_width", 340, 160, 1200)
        self.h = a("card_height", 56, 32, 200)
        self.gap = a("gap", 10, 0, 100)
        self.radius = a("radius", 6, 0, 30)
        self.glow = a("glow_size", 14, 0, 60)
        self.strength = a("glow_strength", 1.0, 0.0, 3.0)
        self.bg_opacity = a("background_opacity", 0.78, 0.0, 1.0)
        self.fill = a("fill_strength", 1.0, 0.0, 2.0)  # 卡片内部彩色光晕填充强度
        self.icon = a("icon_size", 32, 8, 128)
        self.font = str(cfg.get("appearance", "font", "Sans"))
        self.title_pt = a("title_size", 10.5, 5.0, 40.0)
        self.detail_pt = a("detail_size", 8.5, 5.0, 40.0)
        self.margin_right = a("margin_right", 10, 0, 2000)
        self.margin_bottom = a("margin_bottom", 10, 0, 2000)
        self.pad = self.glow + 3  # surface 四周为发光留的空间
        self.colors = {k: cfg.color(k) for k in ("create", "exit", "exec", "merged")}
        cyc = cfg.get("colors", "create_cycle", [])
        from .config import parse_color
        self.create_cycle = [parse_color(str(c)) for c in cyc] if isinstance(cyc, list) else []
        self._cycle_i = 0

    def color_for(self, kind):
        if kind == "create" and self.create_cycle:
            c = self.create_cycle[self._cycle_i % len(self.create_cycle)]
            self._cycle_i += 1
            return c
        return self.colors.get(kind, (1, 1, 1))


def draw_glow(cr, st: Style, x, y, w, h, color):
    """霓虹外发光：只画在卡片外侧（偶奇裁剪），多层描边叠加出平滑衰减，最后加一圈很亮的贴边光。"""
    if st.glow <= 0 or st.strength <= 0:
        return
    cr.save()
    cr.set_fill_rule(cairo.FILL_RULE_EVEN_ODD)
    cr.rectangle(x - st.pad, y - st.pad, w + 2 * st.pad, h + 2 * st.pad)
    rounded_rect(cr, x, y, w, h, st.radius)
    cr.clip()
    r, g, b = color
    steps = max(4, int(st.glow))
    for i in range(steps, 0, -1):
        d = st.glow * i / steps
        # 每层的透明度：越靠近边越亮（叠加后近似高斯衰减）
        k = 1.0 - i / (steps + 1)
        alpha = min(1.0, 0.06 * st.strength * (0.35 + 2.6 * k * k))
        cr.set_source_rgba(r, g, b, alpha)
        cr.set_line_width(2 * d)
        rounded_rect(cr, x, y, w, h, st.radius)
        cr.stroke()
    # 贴边的高亮光圈（霓虹灯管的“芯”）
    for lw, al in ((8.0, 0.22), (5.0, 0.40), (3.0, 0.70), (1.6, 0.95)):
        cr.set_source_rgba(r, g, b, min(1.0, al * st.strength))
        cr.set_line_width(lw)
        rounded_rect(cr, x, y, w, h, st.radius)
        cr.stroke()
    cr.restore()


def render_card(st: Style, kind: str, name: str, detail: str, icon_surface, scale: int = 1):
    color = st.color_for(kind)
    P = st.pad
    W, H = st.w, st.h
    sw, sh = W + 2 * P, H + 2 * P
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, int(sw * scale), int(sh * scale))
    surf.set_device_scale(scale, scale)
    cr = cairo.Context(surf)
    x, y = P, P

    draw_glow(cr, st, x, y, W, H, color)

    # 深色半透明“毛玻璃”底：深色渐变 + 一点主题色 + 顶部高光
    rounded_rect(cr, x, y, W, H, st.radius)
    cr.save()
    cr.clip()
    bg = cairo.LinearGradient(0, y, 0, y + H)
    o = st.bg_opacity
    bg.add_color_stop_rgba(0, 0.10, 0.12, 0.17, o)
    bg.add_color_stop_rgba(1, 0.04, 0.05, 0.08, min(1.0, o + 0.06))
    cr.set_source(bg)
    cr.paint()
    f = st.fill
    c2 = _shift_hue(color)
    # 1) 整张卡片的彩色光晕填充：左边最亮，向右过渡到第二色并减弱
    fill = cairo.LinearGradient(x, 0, x + W, 0)
    fill.add_color_stop_rgba(0.00, *color, min(1.0, 0.72 * f))
    fill.add_color_stop_rgba(0.45, *_mix(color, c2, 0.6), min(1.0, 0.56 * f))
    fill.add_color_stop_rgba(1.00, *c2, min(1.0, 0.52 * f))
    cr.set_source(fill)
    cr.paint()
    # 2) 左侧的辉光“光斑”
    bloom = cairo.RadialGradient(x + W * 0.12, y + H * 0.5, 0, x + W * 0.12, y + H * 0.5, W * 0.55)
    bloom.add_color_stop_rgba(0, *_mix(color, (1, 1, 1), 0.35), min(1.0, 0.30 * f))
    bloom.add_color_stop_rgba(1, *color, 0.0)
    cr.set_source(bloom)
    cr.paint()
    # 3) 文字区域稍微压暗一点，保证白字清晰
    tx0 = x + 12 + st.icon
    shade = cairo.LinearGradient(tx0 - 10, 0, tx0 + 40, 0)
    shade.add_color_stop_rgba(0, 0, 0, 0, 0.0)
    shade.add_color_stop_rgba(1, 0, 0, 0, 0.16)
    cr.set_source(shade)
    cr.rectangle(tx0 - 10, y, x + W - tx0 + 10, H)
    cr.fill()
    # 4) 顶部高光（玻璃感）
    sheen = cairo.LinearGradient(0, y, 0, y + H * 0.5)
    sheen.add_color_stop_rgba(0, 1, 1, 1, 0.16)
    sheen.add_color_stop_rgba(1, 1, 1, 1, 0.0)
    cr.set_source(sheen)
    cr.rectangle(x, y, W, H * 0.5)
    cr.fill()
    # 5) 从边缘向内的光晕（灯管照亮内部的感觉）
    for lw, al in ((22, 0.10), (14, 0.14), (8, 0.20), (4, 0.32)):
        cr.set_source_rgba(*_mix(color, (1, 1, 1), 0.15), min(1.0, al * f))
        cr.set_line_width(lw)
        rounded_rect(cr, x, y, W, H, st.radius)
        cr.stroke()
    # 左侧主题色细条（参考原版左边的彩色竖条）
    cr.set_source_rgba(*color, 0.95)
    cr.rectangle(x, y + 6, 2.5, H - 12)
    cr.fill()
    cr.restore()

    # 1px 细边（主题色偏白）
    rounded_rect(cr, x + 0.5, y + 0.5, W - 1, H - 1, st.radius)
    cr.set_source_rgba(*_mix(color, (1, 1, 1), 0.45), 0.95)
    cr.set_line_width(1.0)
    cr.stroke()

    # 图标
    ix = x + 12
    isz = st.icon
    if icon_surface is not None:
        cr.save()
        icon_w = icon_surface.get_width() / scale
        k = isz / icon_w if icon_w else 1
        cr.translate(ix, y + (H - isz) / 2)
        cr.scale(k, k)
        cr.set_source_surface(icon_surface, 0, 0)
        cr.paint()
        cr.restore()

    # 文字
    tx = ix + isz + 11
    tw = x + W - 10 - tx
    title = Pango.FontDescription.from_string(st.font)
    title.set_absolute_size(st.title_pt * 4 / 3 * Pango.SCALE)
    small = Pango.FontDescription.from_string(st.font)
    small.set_absolute_size(st.detail_pt * 4 / 3 * Pango.SCALE)

    l1 = PangoCairo.create_layout(cr)
    l1.set_font_description(title)
    l1.set_width(int(tw * Pango.SCALE))
    l1.set_ellipsize(Pango.EllipsizeMode.END)
    hexcol = "#%02x%02x%02x" % tuple(int(c * 255) for c in _mix(color, (1, 1, 1), 0.55))
    l1.set_markup(f'<span foreground="{hexcol}" weight="bold">{LABELS.get(kind, kind)}</span>'
                  f'  <span foreground="#ffffff" weight="bold">{GLib.markup_escape_text(name)}</span>', -1)
    l2 = PangoCairo.create_layout(cr)
    l2.set_font_description(small)
    l2.set_width(int(tw * Pango.SCALE))
    l2.set_ellipsize(Pango.EllipsizeMode.END)
    l2.set_single_paragraph_mode(True)
    l2.set_text(detail.replace("\n", " "), -1)

    h1 = l1.get_pixel_extents()[1].height
    h2 = l2.get_pixel_extents()[1].height
    spacing = 3
    ty = y + (H - (h1 + spacing + h2)) / 2

    # 深色描边 + 投影，保证在彩色填充上也清晰
    for (lay, yy) in ((l1, ty), (l2, ty + h1 + spacing)):
        cr.new_path()
        cr.move_to(tx + 0.7, yy + 1.0)
        PangoCairo.layout_path(cr, lay)
        cr.set_source_rgba(0, 0, 0, 0.45)
        cr.fill()
        cr.new_path()
        cr.move_to(tx, yy)
        PangoCairo.layout_path(cr, lay)
        cr.set_source_rgba(0.02, 0.03, 0.06, 0.55)
        cr.set_line_width(2.4)
        cr.set_line_join(cairo.LINE_JOIN_ROUND)
        cr.stroke()
    cr.move_to(tx, ty)
    PangoCairo.show_layout(cr, l1)
    cr.move_to(tx, ty + h1 + spacing)
    cr.set_source_rgba(0.88, 0.90, 0.94, 1.0)
    PangoCairo.show_layout(cr, l2)
    surf.flush()
    return surf
