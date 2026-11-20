"""配置加载：默认值 + 用户 TOML 合并，并带简单校验。"""
from __future__ import annotations

import copy
import os
import shutil
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent
DATA_DIRS = [PKG_DIR.parent / "data", PKG_DIR / "data"]


def data_file(name: str) -> Path:
    for d in DATA_DIRS:
        p = d / name
        if p.exists():
            return p
    raise FileNotFoundError(name)


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "proc-indicator"


def config_path() -> Path:
    return config_dir() / "config.toml"


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def parse_color(s: str, fallback=(0.0, 0.9, 1.0)) -> tuple[float, float, float]:
    try:
        s = s.strip().lstrip("#")
        if len(s) == 3:
            s = "".join(c * 2 for c in s)
        return tuple(int(s[i:i + 2], 16) / 255.0 for i in (0, 2, 4))  # type: ignore
    except Exception:
        return fallback


@dataclass
class Config:
    raw: dict
    path: Path | None = None
    errors: list[str] = field(default_factory=list)

    # 便捷访问
    def get(self, section: str, key: str, default=None):
        return self.raw.get(section, {}).get(key, default)

    def num(self, section, key, default, lo=None, hi=None):
        v = self.get(section, key, default)
        try:
            v = type(default)(v)
        except Exception:
            self.errors.append(f"[{section}] {key} 的值无效: {v!r}，使用默认值 {default}")
            v = default
        if lo is not None:
            v = max(lo, v)
        if hi is not None:
            v = min(hi, v)
        return v

    def color(self, key: str):
        defaults = {"create": "#00e5ff", "exit": "#ff8c1a", "exec": "#d65cff", "merged": "#7d8cff"}
        return parse_color(str(self.get("colors", key, defaults.get(key, "#ffffff"))))

    @property
    def events(self) -> set[str]:
        ev = self.get("behavior", "events", ["create", "exec", "exit"])
        return {str(e).lower() for e in ev} if isinstance(ev, list) else {"create", "exec", "exit"}


def load_defaults() -> dict:
    with open(data_file("default-config.toml"), "rb") as f:
        return tomllib.load(f)


def load_builtin_blacklist() -> dict:
    with open(data_file("builtin-blacklist.toml"), "rb") as f:
        return tomllib.load(f)


def ensure_user_config(path: Path | None = None) -> Path:
    path = path or config_path()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(data_file("default-config.toml"), path)
    return path


def load(path: Path | None = None) -> Config:
    defaults = load_defaults()
    errors: list[str] = []
    user: dict = {}
    if path and path.exists():
        try:
            with open(path, "rb") as f:
                user = tomllib.load(f)
        except Exception as e:  # 配置写错时不崩溃，保留默认值并提示
            errors.append(f"配置文件解析失败: {e}")
            print(f"[proc-indicator] 配置文件解析失败，使用默认配置: {e}", file=sys.stderr)
    return Config(_merge(defaults, user), path, errors)
