"""演示 / 回放事件源（用于截图和测试，不需要任何权限）。"""
from __future__ import annotations

import json
import random

from gi.repository import GLib

SAMPLES = [
    ("firefox", "/usr/lib/firefox/firefox", "/usr/lib/firefox/firefox --new-window https://archlinux.org"),
    ("konsole", "/usr/bin/konsole", "/usr/bin/konsole"),
    ("dolphin", "/usr/bin/dolphin", "/usr/bin/dolphin --new-window /home/user/Projects"),
    ("make", "/usr/bin/make", "make -j16 all"),
    ("gcc", "/usr/bin/gcc", "gcc -O2 -c src/main.c -o build/main.o"),
    ("git", "/usr/bin/git", "git status --porcelain=v2 --branch"),
    ("python3", "/usr/bin/python3.13", "python3 manage.py runserver 0.0.0.0:8000"),
    ("kate", "/usr/bin/kate", "/usr/bin/kate /home/user/.config/proc-indicator/config.toml"),
    ("code", "/opt/visual-studio-code/code", "/opt/visual-studio-code/code --unity-launch"),
    ("pacman", "/usr/bin/pacman", "pacman -Syu"),
    ("vlc", "/usr/bin/vlc", "/usr/bin/vlc --started-from-file video.mkv"),
    ("ls", "/usr/bin/ls", "ls --color=auto -la"),
]


class Demo:
    def __init__(self, feed):
        self.feed = feed
        self.pid = 41000
        self.alive = []
        self.timer = 0
        self.name = "demo"

    def start(self):
        self.timer = GLib.timeout_add(450, self._step)

    def stop(self):
        if self.timer:
            GLib.source_remove(self.timer)

    def _step(self):
        if self.alive and random.random() < 0.45:
            pid = self.alive.pop(random.randrange(len(self.alive)))
            r = random.random()
            code, sig = (0, 0) if r < 0.6 else ((1, 0) if r < 0.8 else (0, 15))
            self.feed({"e": "exit", "pid": pid, "code": code, "sig": sig})
        else:
            comm, exe, cmd = random.choice(SAMPLES)
            self.pid += random.randint(1, 40)
            self.feed({"e": "fork", "pid": self.pid, "ppid": 1234, "comm": "bash", "k": 0})
            self.feed({"e": "exec", "pid": self.pid, "ppid": 1234, "comm": comm, "exe": exe, "cmd": cmd})
            self.alive.append(self.pid)
        return True


class Replay:
    """读取 helper 格式的 JSON 行文件并按 t 字段的节奏回放。"""

    def __init__(self, path, feed):
        self.lines = [json.loads(line) for line in open(path) if line.strip()]
        self.feed = feed
        self.i = 0
        self.name = "replay"

    def start(self):
        GLib.timeout_add(300, self._next)

    def stop(self):
        pass

    def _next(self):
        if self.i >= len(self.lines):
            return False
        ev = self.lines[self.i]
        self.i += 1
        if ev.get("e") not in ("ready",):
            self.feed(ev)
        if self.i < len(self.lines):
            t0, t1 = ev.get("t"), self.lines[self.i].get("t")
            delay = int(max(0, (t1 - t0)) * 1000) if t0 is not None and t1 is not None else 0
            GLib.timeout_add(min(delay, 5000), self._next)
        return False
