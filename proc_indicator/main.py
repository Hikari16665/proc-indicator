"""入口：解析参数、选择事件来源、加载配置并监视变化、启动 GTK。"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import json
import os
import signal
import sys
from pathlib import Path

LAYER_LIB = "libgtk4-layer-shell.so.0"


def _want_wayland(args) -> bool:
    if args.backend == "x11":
        return False
    if args.backend == "wayland":
        return True
    return bool(os.environ.get("WAYLAND_DISPLAY")) or os.environ.get("XDG_SESSION_TYPE") == "wayland"


def _maybe_reexec_with_preload(args):
    """gtk4-layer-shell 必须在 libwayland-client 之前加载，Python 里只能用 LD_PRELOAD 实现。"""
    if not _want_wayland(args) or os.environ.get("PROC_INDICATOR_PRELOADED"):
        return
    lib = ctypes.util.find_library("gtk4-layer-shell") or LAYER_LIB
    env = dict(os.environ)
    env["LD_PRELOAD"] = (lib + " " + env.get("LD_PRELOAD", "")).strip()
    env["PROC_INDICATOR_PRELOADED"] = "1"
    os.execvpe(sys.executable, [sys.executable, "-m", "proc_indicator"] + sys.argv[1:], env)


def _set_process_name(name: bytes = b"proc-indicator"):
    try:
        libc = ctypes.CDLL(None)
        libc.prctl(15, ctypes.c_char_p(name), 0, 0, 0)  # PR_SET_NAME
    except Exception:
        pass


def parse_args(argv=None):
    p = argparse.ArgumentParser(prog="proc-indicator", description="带霓虹发光效果的进程事件桌面浮窗")
    p.add_argument("--config", type=Path, help="配置文件路径（默认 ~/.config/proc-indicator/config.toml）")
    p.add_argument("--source", choices=["auto", "netlink", "poll"], help="覆盖配置里的事件来源")
    p.add_argument("--backend", choices=["auto", "wayland", "x11"], default="auto",
                   help="强制使用 Wayland layer-shell 或 X11 回退")
    p.add_argument("--demo", action="store_true", help="演示模式：生成一些假事件（截图/调样式用）")
    p.add_argument("--replay", type=Path, help="把文件里的 helper JSON 行当作事件回放（测试用）")
    p.add_argument("--stats", action="store_true", help="每 5 秒在 stderr 打印事件统计")
    p.add_argument("--print-events", action="store_true", help="把要显示的卡片同时打印到 stdout")
    p.add_argument("--version", action="store_true")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if args.version:
        from . import __version__
        print(__version__)
        return 0
    _maybe_reexec_with_preload(args)
    _set_process_name()

    import gi
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gio, GLib, Gtk

    from . import config as cfgmod
    from .filters import Filter
    from .pipeline import EventPipeline
    from .sources import NetlinkSource, PollSource, find_helper, log
    from .window import Overlay

    cfg_path = args.config or cfgmod.ensure_user_config()
    cfg = cfgmod.load(cfg_path)
    for e in cfg.errors:
        log(e)

    layer = None
    if _want_wayland(args):
        try:
            gi.require_version("Gtk4LayerShell", "1.0")
            from gi.repository import Gtk4LayerShell as layer  # noqa: N813
        except (ValueError, ImportError) as e:
            log("没有找到 gtk4-layer-shell（", e, "），无法在 Wayland 上做置顶浮窗")
            layer = None

    app = Gtk.Application(application_id="io.github.procindicator",
                          flags=Gio.ApplicationFlags.NON_UNIQUE)
    state = {}

    def on_activate(app):
        nonlocal layer
        Gtk.Settings.get_default()
        if layer is not None and not layer.is_supported():
            log("当前合成器不支持 wlr-layer-shell，回退为普通窗口（可能无法置顶/定位）")
            layer = None
        overlay = Overlay(app, cfg, layer)
        overlay.show()
        GLib.idle_add(lambda: overlay.start_cursor() and False)
        flt = Filter(cfg)

        def sink(ev):
            if args.print_events:
                print(json.dumps({"kind": ev.kind, "name": ev.name, "detail": ev.detail},
                                 ensure_ascii=False), flush=True)
            overlay.add(ev)

        pipe = EventPipeline(cfg, flt, sink)
        state.update(overlay=overlay, pipe=pipe, source=None)

        def start_poll(reason=None):
            if reason:
                log("回退到轮询 /proc：", reason)
            src = PollSource(cfg.num("source", "poll_interval", 0.5, 0.1, 10.0), pipe.feed)
            src.start()
            state["source"] = src
            log(f"事件来源：轮询 /proc（间隔 {src.interval}s）")

        mode = args.source or str(cfg.get("source", "mode", "auto"))
        if args.demo:
            from .demo import Demo
            state["source"] = Demo(pipe.feed)
            state["source"].start()
        elif args.replay:
            from .demo import Replay
            state["source"] = Replay(args.replay, pipe.feed)
            state["source"].start()
        elif mode == "poll":
            start_poll()
        else:
            helper = find_helper(str(cfg.get("source", "helper", "")))
            if not helper:
                if mode == "netlink":
                    log("找不到 proc-events-helper，无法使用 netlink")
                    app.quit()
                    return
                start_poll("找不到 proc-events-helper")
            else:
                def failed(reason):
                    if mode == "netlink":
                        log("netlink 事件源失败：", reason)
                        app.quit()
                    else:
                        start_poll(reason)
                src = NetlinkSource(helper, pipe.feed, failed)
                src.start()
                state["source"] = src
                log("事件来源：netlink proc connector，helper =", helper)

        # 配置文件自动重载（监视目录，兼容编辑器“写临时文件再改名”的保存方式）
        reload_timer = [0]

        def do_reload():
            reload_timer[0] = 0
            new = cfgmod.load(cfg_path)
            for e in new.errors:
                log(e)
            overlay.apply_config(new)
            pipe.reconfigure(new, Filter(new))
            log("配置已重新加载")
            return False

        def on_changed(mon, f, other, ev):
            names = {f.get_basename() if f else None, other.get_basename() if other else None}
            if cfg_path.name in names and not reload_timer[0]:
                reload_timer[0] = GLib.timeout_add(300, do_reload)

        if cfg_path.parent.exists():
            mon = Gio.File.new_for_path(str(cfg_path.parent)).monitor_directory(
                Gio.FileMonitorFlags.WATCH_MOVES, None)
            mon.connect("changed", on_changed)
            state["monitor"] = mon

        if args.stats:
            def stats():
                s = dict(pipe.stats)
                s["frames"] = overlay.frames
                print("[proc-indicator] stats", json.dumps(s, ensure_ascii=False), file=sys.stderr, flush=True)
                return True
            GLib.timeout_add_seconds(5, stats)

    app.connect("activate", on_activate)
    app.hold()
    for sig in (signal.SIGINT, signal.SIGTERM):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, lambda *a: (app.quit(), False)[1])
    rc = app.run([sys.argv[0]])
    src = state.get("source")
    if src is not None:
        src.stop()
    ov = state.get("overlay")
    if ov is not None and ov.cursor is not None:
        ov.cursor.stop()   # 卸载 KWin 光标脚本
    return rc


if __name__ == "__main__":
    sys.exit(main())
