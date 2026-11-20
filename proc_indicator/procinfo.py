"""进程信息结构和 /proc 读取工具。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

PF_KTHREAD = 0x00200000
CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100


def uptime() -> float:
    try:
        with open("/proc/uptime") as f:
            return float(f.read().split()[0])
    except Exception:
        return 0.0


def _basename(p: str) -> str:
    if not p:
        return ""
    p = p.removesuffix(" (deleted)")
    return p.rsplit("/", 1)[-1]


@dataclass
class ProcInfo:
    pid: int
    ppid: int = 0
    comm: str = ""
    exe: str = ""
    cmdline: str = ""
    kthread: bool = False
    start_ticks: int = 0          # /proc/pid/stat 第 22 字段（开机后的 clock ticks）
    seen_uptime: float = field(default_factory=uptime)  # 第一次看到它的时间
    seen_t: float = 0.0           # helper 事件里的单调时钟时间（netlink 模式）

    @property
    def exe_name(self) -> str:
        return _basename(self.exe)

    @property
    def argv0_name(self) -> str:
        if not self.cmdline:
            return ""
        a0 = self.cmdline.split(" ", 1)[0]
        return "" if a0.startswith("-") else _basename(a0)

    @property
    def display_name(self) -> str:
        c = self.comm
        a0 = self.argv0_name
        # comm 最长 15 个字符，被截断时用 argv[0] / exe 的完整名字
        if c and len(c) >= 15:
            for cand in (a0, self.exe_name):
                if cand and cand.startswith(c):
                    return cand
        return c or a0 or self.exe_name or f"pid {self.pid}"

    def runtime(self, exit_t: float | None = None) -> float | None:
        if exit_t and self.seen_t:
            return max(0.0, exit_t - self.seen_t)
        if self.start_ticks:
            return max(0.0, uptime() - self.start_ticks / CLK_TCK)
        if self.seen_uptime:
            return max(0.0, uptime() - self.seen_uptime)
        return None


def read_proc(pid: int) -> ProcInfo | None:
    """读取一个进程的信息；进程已消失返回 None。"""
    base = f"/proc/{pid}"
    try:
        with open(base + "/stat", "rb") as f:
            stat = f.read().decode("utf-8", "replace")
    except OSError:
        return None
    info = ProcInfo(pid)
    try:
        lp, rp = stat.index("("), stat.rindex(")")
        info.comm = stat[lp + 1:rp]
        rest = stat[rp + 2:].split()
        info.ppid = int(rest[1])
        flags = int(rest[6])
        info.start_ticks = int(rest[19])
        info.kthread = bool(flags & PF_KTHREAD) or info.ppid == 2 or pid == 2
    except (ValueError, IndexError):
        pass
    if not info.kthread:
        try:
            with open(base + "/cmdline", "rb") as f:
                info.cmdline = f.read(2048).rstrip(b"\0").replace(b"\0", b" ").decode("utf-8", "replace")
        except OSError:
            pass
        try:
            info.exe = os.readlink(base + "/exe")
        except OSError:
            pass
    return info


def describe_signal(sig: int) -> str:
    import signal as _s
    try:
        return _s.Signals(sig).name
    except ValueError:
        return f"SIG{sig}"


def fmt_duration(sec: float | None) -> str:
    if sec is None:
        return ""
    if sec < 0.001:
        return "<1ms"
    if sec < 1:
        return f"{sec * 1000:.0f}ms"
    if sec < 60:
        return f"{sec:.1f}s"
    if sec < 3600:
        return f"{int(sec // 60)}m{int(sec % 60):02d}s"
    return f"{int(sec // 3600)}h{int(sec % 3600 // 60):02d}m"


def read_comm(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/comm", "rb") as f:
            return f.read().decode("utf-8", "replace").strip()
    except OSError:
        return ""
