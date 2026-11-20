"""把原始事件整理成要显示的卡片：fork+exec 合并、过滤、限流。"""
from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass

from gi.repository import GLib

from .procinfo import ProcInfo, describe_signal, fmt_duration, read_comm, read_proc


@dataclass
class DisplayEvent:
    kind: str          # create / exec / exit / merged
    name: str
    detail: str
    pid: int = 0
    info: ProcInfo | None = None


class EventPipeline:
    def __init__(self, cfg, flt, sink):
        self.sink = sink            # callable(DisplayEvent)
        self.known: dict[int, ProcInfo] = {}
        self.pending: dict[int, tuple[ProcInfo, float]] = {}
        self.pending_timer = 0
        self.flush_timer = GLib.timeout_add(1000, self._flush_merged)
        self.merged = 0
        self.merged_names: Counter = Counter()
        self.stats = Counter()
        self.reconfigure(cfg, flt)

    def reconfigure(self, cfg, flt):
        self.cfg = cfg
        self.flt = flt
        self.events = cfg.events
        self.merge_window = cfg.num("behavior", "merge_window_ms", 40, 0, 1000) / 1000.0
        self.rate = cfg.num("behavior", "max_events_per_second", 20.0, 1.0, 1000.0)
        self.show_runtime = bool(cfg.get("behavior", "show_runtime", True))
        self.tokens = self.rate
        self.last_refill = time.monotonic()

    # ---------- 原始事件入口 ----------
    def feed(self, ev: dict):
        e = ev.get("e")
        self.stats["raw_" + str(e)] += 1
        if e == "fork":
            info = ProcInfo(ev["pid"], ev.get("ppid", 0), ev.get("comm", ""), kthread=bool(ev.get("k")),
                            seen_t=ev.get("t", 0.0))
            parent = self.known.get(info.ppid)
            if parent:
                info.exe, info.cmdline = parent.exe, parent.cmdline
                info.comm = info.comm or parent.comm
            if not info.comm and info.ppid:
                info.comm = read_comm(info.ppid)  # fork 出来的子进程和父进程同名
            self._remember(info)
            if info.kthread:
                return
            self.pending[info.pid] = (info, time.monotonic() + self.merge_window)
            if not self.pending_timer:
                self.pending_timer = GLib.timeout_add(max(5, int(self.merge_window * 500)), self._flush_pending)
        elif e == "exec":
            pid = ev["pid"]
            info = self.known.get(pid) or ProcInfo(pid, seen_t=ev.get("t", 0.0))
            info.ppid = ev.get("ppid", info.ppid)
            info.comm = ev.get("comm", "") or info.comm
            info.exe = ev.get("exe", "")
            info.cmdline = ev.get("cmd", "")
            self._remember(info)
            if pid in self.pending:
                del self.pending[pid]
                self._emit_create(info)
            else:
                self._emit("exec", info)
        elif e == "exit":
            pid = ev["pid"]
            info = ev.get("info") or self.known.get(pid)
            if info is None:
                info = ProcInfo(pid, comm=ev.get("comm", ""), kthread=bool(ev.get("k")),
                                start_ticks=ev.get("start", 0), seen_uptime=0.0)
            else:
                if ev.get("start"):
                    info.start_ticks = ev["start"]
                if not info.comm and ev.get("comm"):
                    info.comm = ev["comm"]
            self.known.pop(pid, None)
            if pid in self.pending:  # 极短命：fork 后还没 exec 就退出了
                del self.pending[pid]
                self._emit_create(info)
            self._emit_exit(info, ev.get("code"), ev.get("sig", 0), ev.get("core", 0), ev.get("t"))
        elif e == "create":  # 轮询模式
            info = ev["info"]
            self._emit_create(info)

    def _remember(self, info: ProcInfo):
        if len(self.known) > 65536:  # 防止漏掉 exit 时无限增长
            for k in list(self.known)[:16384]:
                del self.known[k]
        self.known[info.pid] = info

    def _flush_pending(self):
        now = time.monotonic()
        for pid, (info, deadline) in list(self.pending.items()):
            if now >= deadline:
                del self.pending[pid]
                # fork 之后没有 exec：子进程仍是父进程的程序（比如 shell 子 shell）
                fresh = read_proc(pid)
                if fresh and fresh.cmdline:
                    info.cmdline, info.exe = fresh.cmdline, fresh.exe or info.exe
                    info.start_ticks = fresh.start_ticks
                self._emit_create(info)
        if not self.pending:
            self.pending_timer = 0
            return False
        return True

    # ---------- 显示事件 ----------
    def _emit_create(self, info: ProcInfo):
        self._emit("create", info)

    def _emit_exit(self, info, code, sig, core, t=None):
        self._emit("exit", info, code=code, sig=sig, core=core, t=t)

    def _emit(self, kind, info: ProcInfo, code=None, sig=0, core=0, t=None):
        if kind not in self.events:
            return
        if self.flt.hidden(info):
            self.stats["filtered"] += 1
            return
        if not self._take_token():
            self.merged += 1
            self.merged_names[info.display_name] += 1
            return
        name = info.display_name
        if kind in ("create", "exec"):
            what = info.cmdline or info.exe or info.comm
            detail = f"({info.pid}) {what}"
        else:
            if code is None and not sig:
                rt = fmt_duration(info.runtime(t)) if self.show_runtime else ""
                detail = f"({info.pid}) 退出代码未知" + (f" · 运行 {rt}" if rt else "")
            elif sig:
                detail = f"({info.pid}) 被信号终止 {describe_signal(sig)}({sig})" + (" · core" if core else "")
            else:
                detail = f"({info.pid}) 退出代码 {code}"
            if (code is not None or sig) and self.show_runtime:
                rt = fmt_duration(info.runtime(t))
                if rt:
                    detail += f" · 运行 {rt}"
        self.stats["shown_" + kind] += 1
        self.sink(DisplayEvent(kind, name, detail, info.pid, info))

    def _take_token(self) -> bool:
        now = time.monotonic()
        self.tokens = min(self.rate, self.tokens + (now - self.last_refill) * self.rate)
        self.last_refill = now
        if self.tokens >= 1:
            self.tokens -= 1
            return True
        return False

    def _flush_merged(self):
        if self.merged:
            top = "、".join(f"{n}×{c}" for n, c in self.merged_names.most_common(4))
            self.sink(DisplayEvent("merged", f"+{self.merged} 个事件", f"限流合并：{top}"))
            self.stats["merged"] += self.merged
            self.merged = 0
            self.merged_names.clear()
        return True
