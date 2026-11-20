"""X11 回退：GTK4 没有窗口定位 API，这里用 ctypes 调 libX11 做 override-redirect、定位和置顶。"""
from __future__ import annotations

import ctypes
import ctypes.util


class _XSetWindowAttributes(ctypes.Structure):
    _fields_ = [
        ("background_pixmap", ctypes.c_ulong), ("background_pixel", ctypes.c_ulong),
        ("border_pixmap", ctypes.c_ulong), ("border_pixel", ctypes.c_ulong),
        ("bit_gravity", ctypes.c_int), ("win_gravity", ctypes.c_int),
        ("backing_store", ctypes.c_int), ("backing_planes", ctypes.c_ulong),
        ("backing_pixel", ctypes.c_ulong), ("save_under", ctypes.c_int),
        ("event_mask", ctypes.c_long), ("do_not_propagate_mask", ctypes.c_long),
        ("override_redirect", ctypes.c_int), ("colormap", ctypes.c_ulong),
        ("cursor", ctypes.c_ulong),
    ]


CW_OVERRIDE_REDIRECT = 1 << 9


class X11:
    def __init__(self):
        name = ctypes.util.find_library("X11")
        if not name:
            raise OSError("libX11 not found")
        self.x = ctypes.CDLL(name)
        self.x.XOpenDisplay.restype = ctypes.c_void_p
        self.x.XOpenDisplay.argtypes = [ctypes.c_char_p]
        for fn in ("XFlush", "XSync", "XRaiseWindow", "XMoveWindow", "XChangeWindowAttributes"):
            getattr(self.x, fn).restype = ctypes.c_int
        self.x.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.x.XFlush.argtypes = [ctypes.c_void_p]
        self.x.XRaiseWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        self.x.XMoveWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_int]
        self.x.XChangeWindowAttributes.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong,
                                                   ctypes.POINTER(_XSetWindowAttributes)]
        self.dpy = self.x.XOpenDisplay(None)
        if not self.dpy:
            raise OSError("XOpenDisplay failed")

    def override_redirect(self, xid: int):
        attrs = _XSetWindowAttributes()
        attrs.override_redirect = 1
        self.x.XChangeWindowAttributes(self.dpy, xid, CW_OVERRIDE_REDIRECT, ctypes.byref(attrs))
        self.x.XSync(self.dpy, 0)

    def move(self, xid: int, x: int, y: int):
        self.x.XMoveWindow(self.dpy, xid, x, y)
        self.x.XFlush(self.dpy)

    def raise_(self, xid: int):
        self.x.XRaiseWindow(self.dpy, xid)
        self.x.XFlush(self.dpy)
