"""黑名单 / 白名单匹配。"""
from __future__ import annotations

import fnmatch
import re
import sys
from dataclasses import dataclass, field

from .config import Config, load_builtin_blacklist

GLOB_CHARS = set("*?[")


class _NameSet:
    """进程名：精确匹配走 set，含通配符的走 glob。"""

    def __init__(self, patterns):
        self.exact: set[str] = set()
        globs = []
        for p in patterns or []:
            p = str(p)
            if GLOB_CHARS & set(p):
                globs.append(fnmatch.translate(p))
            else:
                self.exact.add(p)
        self.rx = re.compile("|".join(globs)) if globs else None

    def match(self, s: str) -> bool:
        if not s:
            return False
        return s in self.exact or bool(self.rx and self.rx.match(s))


def _globs(patterns):
    pats = [fnmatch.translate(str(p)) for p in patterns or []]
    return re.compile("|".join(pats), re.S) if pats else None


def _regexes(patterns, where):
    out = []
    for p in patterns or []:
        try:
            out.append(re.compile(str(p)))
        except re.error as e:
            print(f"[proc-indicator] 忽略无效正则 {where}: {p!r} ({e})", file=sys.stderr)
    return out


@dataclass
class _Rules:
    names: _NameSet
    paths: re.Pattern | None
    cmds: re.Pattern | None
    name_rx: list = field(default_factory=list)
    path_rx: list = field(default_factory=list)
    cmd_rx: list = field(default_factory=list)

    @classmethod
    def build(cls, sections: list[dict]):
        def cat(key):
            out = []
            for s in sections:
                v = s.get(key, [])
                if isinstance(v, list):
                    out.extend(v)
            return out

        return cls(
            _NameSet(cat("names")), _globs(cat("paths")), _globs(cat("cmdlines")),
            _regexes(cat("name_regex"), "name_regex"),
            _regexes(cat("path_regex"), "path_regex"),
            _regexes(cat("cmdline_regex"), "cmdline_regex"),
        )

    def match(self, names: list[str], path: str, cmd: str) -> bool:
        for n in names:
            if self.names.match(n) or any(r.search(n) for r in self.name_rx):
                return True
        if path and ((self.paths and self.paths.match(path)) or any(r.search(path) for r in self.path_rx)):
            return True
        if cmd and ((self.cmds and self.cmds.match(cmd)) or any(r.search(cmd) for r in self.cmd_rx)):
            return True
        return False


class Filter:
    def __init__(self, cfg: Config):
        bl = cfg.raw.get("blacklist", {})
        sections = [bl]
        if bl.get("use_builtin", True):
            try:
                sections.append(load_builtin_blacklist())
            except Exception as e:
                print(f"[proc-indicator] 内置黑名单加载失败: {e}", file=sys.stderr)
        self.hide_kthreads = bool(bl.get("hide_kernel_threads", True))
        self.black = _Rules.build(sections)
        self.white = _Rules.build([cfg.raw.get("whitelist", {})])
        self._cache: dict[tuple, bool] = {}

    def hidden(self, info) -> bool:
        """info: ProcInfo。返回 True 表示不显示。"""
        if self.hide_kthreads and info.kthread:
            return True
        key = (info.comm, info.exe, info.cmdline)
        r = self._cache.get(key)
        if r is None:
            names = [n for n in {info.comm, info.exe_name, info.argv0_name} if n]
            r = self.black.match(names, info.exe, info.cmdline) and not self.white.match(
                names, info.exe, info.cmdline)
            if len(self._cache) > 4096:
                self._cache.clear()
            self._cache[key] = r
        return r
