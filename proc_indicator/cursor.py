"""全局光标位置（用于“悬停避让”）。

浮窗是点击穿透的（空 input region），自己收不到任何鼠标事件，所以光标位置要另外拿：

* KDE Plasma (Wayland/X11 都适用)：通过 D-Bus 让 KWin 加载一个很小的 KWin 脚本
  （org.kde.KWin /Scripting → org.kde.kwin.Scripting.loadScript）。脚本监听
  workspace.cursorPosChanged，只有当光标在“工作区右侧、浮窗所在那一条”里（或刚离开时）
  才用 callDBus() 把坐标推给本程序。坐标是 KWin 的全局逻辑坐标，和 GTK 的逻辑坐标同一尺度，
  HiDPI/分数缩放不需要换算。只在屏幕上有卡片时加载脚本，没卡片几秒后卸载。
* X11（非 KWin）：XQueryPointer 轮询，仅在有卡片时 30Hz。
* 其它情况（GNOME/sway 等 Wayland）：拿不到 → 安静地关闭避让功能。

回调 on_cursor(x, y) 给出相对浮窗左上角的逻辑坐标；None 表示光标不在浮窗附近/未知。
"""
from __future__ import annotations

import ctypes
import os
import sys
import tempfile

from gi.repository import Gio, GLib

BUS_NAME = "io.github.procindicator.Cursor"
OBJ_PATH = "/io/github/procindicator/Cursor"
IFACE = "io.github.procindicator.Cursor"
SCRIPT_NAME = "proc-indicator-cursor"

INTROSPECTION = f"""
<node>
  <interface name="{IFACE}">
    <method name="Update"><arg type="s" name="data" direction="in"/></method>
  </interface>
</node>
"""

# KWin 脚本（KWin 6 API；同时兼容 5.27 的大部分写法）。%BAND% 是浮窗宽度（逻辑像素）。
KWIN_SCRIPT = r"""
const BAND = %BAND%;
let inside = false;
function area(p) {
    let out = null;
    try { out = workspace.screenAt(p); } catch (e) {}
    if (!out) out = workspace.activeScreen;
    try { return workspace.clientArea(KWin.MaximizeArea, out, workspace.currentDesktop); }
    catch (e) { return workspace.clientArea(KWin.MaximizeArea, workspace.activeScreen, workspace.currentDesktop); }
}
function send() {
    const p = workspace.cursorPos;
    const a = area(p);
    const right = a.x + a.width;
    const near = p.x >= right - BAND - 2 && p.x <= right + 2 && p.y >= a.y - 2 && p.y <= a.y + a.height + 2;
    if (near || inside) {
        callDBus("%BUS%", "%PATH%", "%IFACE%", "Update",
                 [p.x, p.y, a.x, a.y, a.width, a.height, near ? 1 : 0].join(","));
    }
    inside = near;
}
workspace.cursorPosChanged.connect(send);
send();
"""


def log(*a):
    print("[proc-indicator]", *a, file=sys.stderr, flush=True)


class NullCursor:
    name = "none"

    def __init__(self, *a, **k):
        pass

    def set_active(self, active: bool):
        pass

    def set_band(self, width: int):
        pass

    def stop(self):
        pass


class X11Cursor:
    """XQueryPointer 轮询。win_origin() 返回浮窗左上角的根窗口坐标(物理像素)和缩放系数。"""
    name = "x11"

    def __init__(self, x11, win_origin, on_cursor):
        self.x11 = x11
        self.win_origin = win_origin
        self.on_cursor = on_cursor
        self.timer = 0
        lib = x11.x
        lib.XDefaultRootWindow.restype = ctypes.c_ulong
        lib.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        lib.XQueryPointer.restype = ctypes.c_int
        lib.XQueryPointer.argtypes = [ctypes.c_void_p, ctypes.c_ulong] + [ctypes.c_void_p] * 7
        self.root = lib.XDefaultRootWindow(x11.dpy)

    def _poll(self):
        lib = self.x11.x
        r, c = ctypes.c_ulong(), ctypes.c_ulong()
        rx, ry, wx, wy = ctypes.c_int(), ctypes.c_int(), ctypes.c_int(), ctypes.c_int()
        m = ctypes.c_uint()
        ok = lib.XQueryPointer(self.x11.dpy, self.root, ctypes.byref(r), ctypes.byref(c), ctypes.byref(rx),
                               ctypes.byref(ry), ctypes.byref(wx), ctypes.byref(wy), ctypes.byref(m))
        if ok:
            ox, oy, scale = self.win_origin()
            self.on_cursor((rx.value - ox) / scale, (ry.value - oy) / scale)
        return True

    def set_active(self, active: bool):
        if active and not self.timer:
            self.timer = GLib.timeout_add(33, self._poll)
        elif not active and self.timer:
            GLib.source_remove(self.timer)
            self.timer = 0
            self.on_cursor(None, None)

    def set_band(self, width: int):
        pass

    def stop(self):
        self.set_active(False)


class KWinCursor:
    """通过 KWin 脚本拿光标。window_info() 返回 (浮窗宽度, 浮窗所在显示器的逻辑几何 Gdk.Rectangle 或 None)。"""
    name = "kwin"
    UNLOAD_DELAY_MS = 3000

    def __init__(self, bus, window_info, on_cursor, band: int):
        self.bus = bus
        self.window_info = window_info
        self.on_cursor = on_cursor
        self.band = band
        self.loaded = False
        self.want = False
        self.unload_timer = 0
        self.busy = False
        self.reg_id = 0
        self.own_id = 0
        self.script_path = None
        node = Gio.DBusNodeInfo.new_for_xml(INTROSPECTION)
        self.reg_id = bus.register_object(OBJ_PATH, node.interfaces[0], self._on_call, None, None)
        self.own_id = Gio.bus_own_name_on_connection(bus, BUS_NAME, Gio.BusNameOwnerFlags.NONE, None, None)
        self._call_scripting("unloadScript", GLib.Variant("(s)", (SCRIPT_NAME,)))  # 清掉上次崩溃残留

    @staticmethod
    def available(bus) -> bool:
        try:
            r = bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                              "NameHasOwner", GLib.Variant("(s)", ("org.kde.KWin",)), None,
                              Gio.DBusCallFlags.NONE, 500, None)
            if not r.unpack()[0]:
                return False
            bus.call_sync("org.kde.KWin", "/Scripting", "org.freedesktop.DBus.Introspectable", "Introspect",
                          None, None, Gio.DBusCallFlags.NONE, 1000, None)
            return True
        except GLib.Error:
            return False

    # ---- D-Bus 方法：KWin 脚本推送光标 ----
    def _on_call(self, conn, sender, path, iface, method, params, invocation):
        if method == "Update":
            try:
                cx, cy, ax, ay, aw, ah, near = (float(v) for v in params.unpack()[0].split(","))
            except ValueError:
                invocation.return_value(None)
                return
            invocation.return_value(None)
            if not near:
                self.on_cursor(None, None)
                return
            width, mon = self.window_info()
            if mon is not None and not (mon.x <= cx < mon.x + mon.width and mon.y <= cy < mon.y + mon.height):
                self.on_cursor(None, None)   # 光标在别的显示器上
                return
            self.on_cursor(cx - (ax + aw - width), cy - ay)
        else:
            invocation.return_dbus_error("org.freedesktop.DBus.Error.UnknownMethod", method)

    # ---- 脚本加载/卸载 ----
    def _call_scripting(self, method, params, cb=None):
        def done(bus, res):
            try:
                r = bus.call_finish(res)
            except GLib.Error as e:
                r = None
                if method == "loadScript":
                    log("KWin 脚本加载失败，关闭悬停避让：", e.message)
            if cb:
                cb(r)
        self.bus.call("org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting", method, params, None,
                      Gio.DBusCallFlags.NONE, 3000, None, done)

    def _write_script(self):
        d = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
        path = os.path.join(d, "proc-indicator-cursor.js")
        src = (KWIN_SCRIPT.replace("%BAND%", str(int(self.band))).replace("%BUS%", BUS_NAME)
               .replace("%PATH%", OBJ_PATH).replace("%IFACE%", IFACE))
        with open(path, "w") as f:
            f.write(src)
        self.script_path = path
        return path

    def _load(self):
        if self.loaded or self.busy:
            return
        self.busy = True
        path = self._write_script()

        def loaded(r):
            self.busy = False
            if r is None:
                return
            sid = r.unpack()[0]
            if sid < 0:
                log("KWin 拒绝加载光标脚本，关闭悬停避让")
                return
            self.loaded = True
            for obj in (f"/Scripting/Script{sid}", f"/{sid}"):  # KWin 6 / KWin 5 的对象路径
                self.bus.call("org.kde.KWin", obj, "org.kde.kwin.Script", "run", None, None,
                              Gio.DBusCallFlags.NONE, 3000, None, lambda b, res: _ignore(b, res))
            if not self.want:
                self._unload()
        self._call_scripting("loadScript", GLib.Variant("(ss)", (path, SCRIPT_NAME)), loaded)

    def _unload(self):
        self.unload_timer = 0
        if self.want:
            return False
        self.loaded = False
        self._call_scripting("unloadScript", GLib.Variant("(s)", (SCRIPT_NAME,)))
        self.on_cursor(None, None)
        return False

    def set_active(self, active: bool):
        self.want = active
        if active:
            if self.unload_timer:
                GLib.source_remove(self.unload_timer)
                self.unload_timer = 0
            self._load()
        elif self.loaded and not self.unload_timer:
            self.unload_timer = GLib.timeout_add(self.UNLOAD_DELAY_MS, self._unload)

    def set_band(self, width: int):
        if int(width) != int(self.band):
            self.band = width
            if self.loaded:   # 重新加载以更新脚本里的宽度
                self.loaded = False
                self._call_scripting("unloadScript", GLib.Variant("(s)", (SCRIPT_NAME,)),
                                     lambda r: self.want and self._load())

    def stop(self):
        self.want = False
        if self.loaded:
            try:
                self.bus.call_sync("org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting", "unloadScript",
                                   GLib.Variant("(s)", (SCRIPT_NAME,)), None, Gio.DBusCallFlags.NONE, 1000, None)
            except GLib.Error:
                pass
        if self.own_id:
            Gio.bus_unown_name(self.own_id)
        if self.reg_id:
            self.bus.unregister_object(self.reg_id)
        if self.script_path:
            try:
                os.unlink(self.script_path)
            except OSError:
                pass


def _ignore(bus, res):
    try:
        bus.call_finish(res)
    except GLib.Error:
        pass


def create(mode: str, *, x11=None, win_origin=None, window_info=None, on_cursor=None, band=400):
    """mode: auto / kwin / x11 / none"""
    if mode == "none":
        return NullCursor()
    if mode in ("auto", "kwin"):
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except GLib.Error:
            bus = None
        if bus is not None and KWinCursor.available(bus):
            log("悬停避让：使用 KWin 脚本获取光标位置")
            return KWinCursor(bus, window_info, on_cursor, band)
    if mode in ("auto", "x11") and x11 is not None:
        log("悬停避让：使用 XQueryPointer 获取光标位置")
        return X11Cursor(x11, win_origin, on_cursor)
    if mode != "none":
        log("悬停避让：拿不到全局光标位置（不是 KWin 也不是 X11），已关闭")
    return NullCursor()
