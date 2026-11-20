"""进程事件来源：netlink proc connector（经由 setcap 的小 helper）或轮询 /proc。

两种来源都在 GLib 主循环里运行（不开线程），输出统一的原始事件 dict 给 EventPipeline：
  {"e": "fork"|"exec"|"exit"|"create", "pid": ..., ...}
"""
from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from gi.repository import GLib

from .procinfo import read_proc

HELPER_NAME = "proc-events-helper"


def log(*a):
    print("[proc-indicator]", *a, file=sys.stderr, flush=True)


def find_helper(configured: str = "") -> str | None:
    here = Path(__file__).resolve().parent
    cands = [
        configured,
        os.environ.get("PROC_INDICATOR_HELPER", ""),
        "/usr/local/lib/proc-indicator/" + HELPER_NAME,
        "/usr/lib/proc-indicator/" + HELPER_NAME,
        os.path.expanduser("~/.local/lib/proc-indicator/" + HELPER_NAME),
        str(here.parent / "helper" / HELPER_NAME),
        shutil.which(HELPER_NAME) or "",
    ]
    for c in cands:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


class NetlinkSource:
    """启动 helper，读取它输出的 JSON 行。

    on_fail(reason) 在 helper 起不来 / 自检失败 / 中途退出时调用（用于回退到轮询）。
    """

    SELFTEST_TIMEOUT_MS = 2000

    def __init__(self, helper: str, on_event, on_fail, selftest=True):
        self.helper = helper
        self.on_event = on_event
        self.on_fail = on_fail
        self.selftest = selftest
        self.proc: subprocess.Popen | None = None
        self.buf = b""
        self.watch = 0
        self.verified = not selftest
        self.probe_pid = None
        self.probe_timer = 0
        self.ignore_pids: set[int] = set()
        self.name = "netlink"

    def start(self):
        try:
            self.proc = subprocess.Popen([self.helper], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                         stdin=subprocess.DEVNULL, close_fds=True)
        except OSError as e:
            GLib.idle_add(self._fail, f"无法启动 helper: {e}")
            return
        self.ignore_pids.add(self.proc.pid)
        fd = self.proc.stdout.fileno()
        fl = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)
        self.watch = GLib.io_add_watch(fd, GLib.PRIORITY_DEFAULT,
                                       GLib.IOCondition.IN | GLib.IOCondition.HUP | GLib.IOCondition.ERR,
                                       self._on_io)

    def stop(self):
        if self.watch:
            GLib.source_remove(self.watch)
            self.watch = 0
        if self.probe_timer:
            GLib.source_remove(self.probe_timer)
            self.probe_timer = 0
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(1)
            except Exception:
                self.proc.kill()
        self.proc = None

    def _fail(self, reason):
        self.stop()
        self.on_fail(reason)
        return False

    def _start_probe(self):
        # 自检：自己起一个 /bin/true，看能否在 2 秒内收到它的事件。
        # 用来识别“helper 起来了但收不到事件”的情况（没有 CAP_NET_ADMIN、在容器/非初始 netns 里等）。
        try:
            p = subprocess.Popen(["true"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
            self.probe_pid = p.pid
            self.ignore_pids.add(p.pid)
            p.wait()
        except OSError:
            self.verified = True
            return
        self.probe_timer = GLib.timeout_add(self.SELFTEST_TIMEOUT_MS, self._probe_timeout)

    def _probe_timeout(self):
        self.probe_timer = 0
        if not self.verified:
            self._fail("helper 已启动，但自检进程的事件没有收到（缺少 CAP_NET_ADMIN，或运行在容器/非初始网络命名空间）")
        return False

    def _on_io(self, fd, cond):
        try:
            chunk = os.read(fd, 1 << 18)
        except BlockingIOError:
            return True
        except OSError:
            chunk = b""
        if not chunk:
            err = b""
            try:
                err = self.proc.stderr.read() if self.proc else b""
            except Exception:
                pass
            self.watch = 0
            GLib.idle_add(self._fail, "helper 退出: " + (err.decode(errors="replace").strip() or "无输出"))
            return False
        data = self.buf + chunk
        lines = data.split(b"\n")
        self.buf = lines.pop()
        for line in lines:
            if not line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            e = ev.get("e")
            if e == "ready":
                if self.selftest:
                    self._start_probe()
                continue
            pid = ev.get("pid")
            if pid in self.ignore_pids:
                if pid == self.probe_pid and not self.verified:
                    self.verified = True
                    log("netlink 自检通过，使用 proc connector 事件源")
                if e == "exit":
                    self.ignore_pids.discard(pid)
                continue
            if e == "overflow":
                log("警告：内核 netlink 缓冲区溢出，部分事件丢失")
            self.on_event(ev)
        return True


class PollSource:
    """无权限回退：定期扫描 /proc，比较 PID 集合。拿不到退出码，会漏掉两次扫描之间生灭的进程。"""

    def __init__(self, interval: float, on_event):
        self.interval = max(0.1, float(interval))
        self.on_event = on_event
        self.known: dict[int, object] = {}
        self.timer = 0
        self.name = "poll"
        self.self_pid = os.getpid()

    @staticmethod
    def _pids():
        out = set()
        for n in os.listdir("/proc"):
            if n.isdigit():
                out.add(int(n))
        return out

    def start(self):
        for pid in self._pids():
            info = read_proc(pid)
            if info:
                self.known[pid] = info
        self.timer = GLib.timeout_add(int(self.interval * 1000), self._scan)

    def stop(self):
        if self.timer:
            GLib.source_remove(self.timer)
            self.timer = 0

    def _scan(self):
        cur = self._pids()
        prev = set(self.known)
        for pid in sorted(cur - prev):
            info = read_proc(pid)
            if info is None:
                continue
            self.known[pid] = info
            self.on_event({"e": "create", "info": info})
        for pid in prev - cur:
            info = self.known.pop(pid)
            self.on_event({"e": "exit", "pid": pid, "info": info, "code": None})
        return True
