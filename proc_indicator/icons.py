"""程序图标查找：.desktop 文件 → 图标主题 → 通用可执行文件图标。结果缓存为 cairo surface。"""
from __future__ import annotations

import configparser
import os
import sys
from pathlib import Path

import cairo
from gi.repository import GdkPixbuf, Gtk

LITTLE = sys.byteorder == "little"
FALLBACK_ICONS = ["application-x-executable", "application-x-executable-symbolic",
                  "system-run", "utilities-terminal", "image-missing"]


def _data_dirs():
    dirs = [os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")]
    dirs += (os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":")
    dirs += ["/var/lib/flatpak/exports/share", os.path.expanduser("~/.local/share/flatpak/exports/share")]
    return [d for d in dict.fromkeys(dirs) if d]


class DesktopIndex:
    """把 Exec 程序名 / desktop 文件名 / StartupWMClass 映射到 Icon=。"""

    def __init__(self):
        self.map: dict[str, str] = {}
        for d in reversed(_data_dirs()):
            appdir = Path(d) / "applications"
            if not appdir.is_dir():
                continue
            for f in appdir.rglob("*.desktop"):
                self._add(f)

    def _add(self, f: Path):
        cp = configparser.RawConfigParser(strict=False, interpolation=None)
        try:
            cp.read(f, encoding="utf-8")
            sec = cp["Desktop Entry"]
        except Exception:
            return
        icon = sec.get("Icon", "").strip()
        if not icon:
            return
        keys = {f.stem.lower(), f.stem.rsplit(".", 1)[-1].lower()}
        exe = sec.get("Exec", "").strip()
        if exe:
            parts = exe.split()
            # 跳过 env VAR=... 前缀
            while parts and (parts[0] == "env" or "=" in parts[0]):
                parts.pop(0)
            if parts:
                keys.add(os.path.basename(parts[0].strip('"')).lower())
        tryexec = sec.get("TryExec", "").strip()
        if tryexec:
            keys.add(os.path.basename(tryexec).lower())
        wm = sec.get("StartupWMClass", "").strip()
        if wm:
            keys.add(wm.lower())
        for k in keys:
            self.map.setdefault(k, icon)

    def lookup(self, name: str) -> str | None:
        return self.map.get(name.lower()) if name else None


class IconCache:
    def __init__(self, size: int = 32, scale: int = 1):
        self.size = size
        self.scale = scale
        self.desktop = None
        self.cache: dict[str, cairo.ImageSurface | None] = {}
        self.theme = None

    def _theme(self):
        if self.theme is None:
            from gi.repository import Gdk
            disp = Gdk.Display.get_default()
            self.theme = Gtk.IconTheme.get_for_display(disp)
        return self.theme

    def _desktop(self):
        if self.desktop is None:
            self.desktop = DesktopIndex()
        return self.desktop

    def _load_path(self, path: str):
        """依次尝试：librsvg（SVG）→ GTK 自带的纹理加载（PNG 等）→ GdkPixbuf。"""
        px = self.size * self.scale
        low = path.lower()
        if low.endswith((".svg", ".svgz")):
            surf = _load_svg(path, px)
            if surf is not None:
                surf.set_device_scale(self.scale, self.scale)
                return surf
        else:
            surf = _load_texture(path, px)
            if surf is not None:
                surf.set_device_scale(self.scale, self.scale)
                return surf
        try:
            pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, px, px, True)
        except Exception:
            return None
        return _pixbuf_to_surface(pb, px, self.scale)

    def _from_icon_name(self, icon: str):
        if not icon:
            return None
        if os.path.isabs(icon):
            return self._load_path(icon) if os.path.exists(icon) else None
        theme = self._theme()
        if not theme.has_icon(icon):
            return None
        paintable = theme.lookup_icon(icon, None, self.size, self.scale, Gtk.TextDirection.NONE, 0)
        f = paintable.get_file() if paintable else None
        path = f.get_path() if f else None
        if path:
            return self._load_path(path)
        return None

    def get(self, names: list[str]):
        key = "|".join(names)
        if key in self.cache:
            return self.cache[key]
        surf = None
        for n in names:
            if not n:
                continue
            icon = self._desktop().lookup(n)
            surf = self._from_icon_name(icon) if icon else None
            if surf is None:
                surf = self._from_icon_name(n) or self._from_icon_name(n.lower())
            if surf is not None:
                break
        if surf is None:
            for fb in FALLBACK_ICONS:
                surf = self._from_icon_name(fb)
                if surf is not None:
                    break
        if len(self.cache) > 512:
            self.cache.clear()
        self.cache[key] = surf
        return surf


def _pixbuf_to_surface(pb, px: int, scale: int):
    """GdkPixbuf(RGBA, 非预乘) → cairo ARGB32(预乘)。图标很小且有缓存，纯 Python 足够。"""
    if not pb.get_has_alpha():
        pb = pb.add_alpha(False, 0, 0, 0)
    w, h = pb.get_width(), pb.get_height()
    src = pb.get_pixels()
    stride = pb.get_rowstride()
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, px, px)
    dst = surf.get_data()
    dstride = surf.get_stride()
    ox, oy = (px - w) // 2, (px - h) // 2
    for y in range(h):
        so = y * stride
        do = (y + oy) * dstride + ox * 4
        for x in range(w):
            r, g, b, a = src[so], src[so + 1], src[so + 2], src[so + 3]
            if a:
                if a != 255:
                    r, g, b = r * a // 255, g * a // 255, b * a // 255
                if LITTLE:
                    dst[do], dst[do + 1], dst[do + 2], dst[do + 3] = b, g, r, a
                else:
                    dst[do], dst[do + 1], dst[do + 2], dst[do + 3] = a, r, g, b
            so += 4
            do += 4
    surf.mark_dirty()
    surf.set_device_scale(scale, scale)
    return surf


def _load_svg(path: str, px: int):
    try:
        import gi
        gi.require_version("Rsvg", "2.0")
        from gi.repository import Rsvg
        h = Rsvg.Handle.new_from_file(path)
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, px, px)
        cr = cairo.Context(surf)
        vp = Rsvg.Rectangle()
        vp.x, vp.y, vp.width, vp.height = 0, 0, px, px
        h.render_document(cr, vp)
        surf.flush()
        return surf
    except Exception:
        return None


def _load_texture(path: str, px: int):
    try:
        import io
        from gi.repository import Gdk
        tex = Gdk.Texture.new_from_filename(path)
        png = tex.save_to_png_bytes().get_data()
        src = cairo.ImageSurface.create_from_png(io.BytesIO(png))
        w, h = src.get_width(), src.get_height()
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, px, px)
        cr = cairo.Context(surf)
        k = min(px / w, px / h)
        cr.translate((px - w * k) / 2, (px - h * k) / 2)
        cr.scale(k, k)
        cr.set_source_surface(src, 0, 0)
        cr.get_source().set_filter(cairo.FILTER_GOOD)
        cr.paint()
        surf.flush()
        return surf
    except Exception:
        return None
