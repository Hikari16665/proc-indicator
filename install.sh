#!/usr/bin/env bash
# proc-indicator 安装脚本（Arch / Nyarch / KDE Plasma）
#
#   ./install.sh                  安装到 ~/.local，并用 KDE autostart 开机自启
#   ./install.sh --deps           先用 pacman 安装依赖（会调用 sudo）
#   ./install.sh --systemd        改用 systemd --user 服务自启（代替 autostart .desktop）
#   ./install.sh --no-autostart   不设置开机自启
#   ./install.sh --setcap         把 helper 装到 /usr/local/lib 并 setcap cap_net_admin+ep
#                                 （只有内核 < 6.6 才需要；会调用 sudo）
#   ./install.sh --no-start       装完不立即启动
#   ./install.sh --uninstall [--purge]   卸载（--purge 同时删除配置）
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
PREFIX="${PREFIX:-$HOME/.local}"
APPDIR="$PREFIX/share/proc-indicator"
BINDIR="$PREFIX/bin"
LIBDIR="$PREFIX/lib/proc-indicator"
CONFDIR="${XDG_CONFIG_HOME:-$HOME/.config}"
SYS_HELPER=/usr/local/lib/proc-indicator/proc-events-helper
PACMAN_DEPS=(python python-gobject python-cairo gtk4 gtk4-layer-shell librsvg gcc libcap)

AUTOSTART=desktop SETCAP=0 DEPS=0 UNINSTALL=0 PURGE=0 START=1
for a in "$@"; do
  case "$a" in
    --deps) DEPS=1 ;;
    --systemd) AUTOSTART=systemd ;;
    --no-autostart) AUTOSTART=none ;;
    --setcap) SETCAP=1 ;;
    --no-start) START=0 ;;
    --uninstall) UNINSTALL=1 ;;
    --purge) PURGE=1 ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) echo "未知参数: $a" >&2; exit 2 ;;
  esac
done

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m警告:\033[0m %s\n' "$*" >&2; }

stop_running() {
  if command -v systemctl >/dev/null && systemctl --user is-active -q proc-indicator.service 2>/dev/null; then
    systemctl --user stop proc-indicator.service || true
  fi
  pkill -x proc-indicator 2>/dev/null || true
}

if [ "$UNINSTALL" = 1 ]; then
  say "卸载 proc-indicator"
  stop_running
  if command -v systemctl >/dev/null; then
    systemctl --user disable proc-indicator.service 2>/dev/null || true
  fi
  rm -f "$CONFDIR/systemd/user/proc-indicator.service" "$CONFDIR/autostart/proc-indicator.desktop" \
        "$HOME/.local/share/applications/proc-indicator.desktop" "$BINDIR/proc-indicator"
  rm -rf "$APPDIR" "$LIBDIR"
  if [ -e "$SYS_HELPER" ]; then sudo rm -rf "$(dirname "$SYS_HELPER")"; fi
  if [ "$PURGE" = 1 ]; then rm -rf "$CONFDIR/proc-indicator"; fi
  command -v systemctl >/dev/null && systemctl --user daemon-reload 2>/dev/null || true
  say "完成"
  exit 0
fi

if [ "$DEPS" = 1 ]; then
  say "安装依赖: ${PACMAN_DEPS[*]}"
  sudo pacman -S --needed "${PACMAN_DEPS[@]}"
fi

say "检查运行环境"
python3 - <<'PY' || { warn "Python 依赖不满足，可以运行 ./install.sh --deps"; exit 1; }
import sys
assert sys.version_info >= (3, 11), "需要 Python 3.11+（用到 tomllib）"
import gi, cairo
gi.require_version("Gtk", "4.0"); gi.require_foreign("cairo")
from gi.repository import Gtk
print(f"  Python {sys.version.split()[0]}，GTK {Gtk.get_major_version()}.{Gtk.get_minor_version()}.{Gtk.get_micro_version()}")
try:
    gi.require_version("Gtk4LayerShell", "1.0")
    from gi.repository import Gtk4LayerShell  # noqa
    print("  gtk4-layer-shell: 已安装")
except Exception:
    print("  gtk4-layer-shell: 未安装！Wayland 下无法置顶/定位（pacman -S gtk4-layer-shell）")
PY

say "复制程序到 $APPDIR"
stop_running
rm -rf "$APPDIR"
mkdir -p "$APPDIR" "$BINDIR" "$LIBDIR"
cp -r "$SRC/proc_indicator" "$SRC/data" "$SRC/helper" "$APPDIR/"
cp "$SRC/README.md" "$APPDIR/" 2>/dev/null || true
find "$APPDIR" -name '__pycache__' -prune -exec rm -rf {} +
sed "s#@APPDIR@#$APPDIR#" "$SRC/bin/proc-indicator" > "$BINDIR/proc-indicator"
chmod 755 "$BINDIR/proc-indicator"

HELPER=""
if command -v gcc >/dev/null; then
  say "编译 netlink helper"
  gcc -O2 -Wall -o "$LIBDIR/proc-events-helper" "$SRC/helper/proc-events-helper.c"
  HELPER="$LIBDIR/proc-events-helper"
else
  warn "没有 gcc，跳过 helper（将回退到轮询 /proc，拿不到退出码）。pacman -S gcc 后重新运行本脚本即可。"
fi

KVER="$(uname -r | cut -d. -f1-2)"
KMAJ="${KVER%%.*}"; KMIN="${KVER#*.}"; KMIN="${KMIN%%[^0-9]*}"
if [ -n "$HELPER" ]; then
  if [ "$SETCAP" = 1 ]; then
    say "安装有权限的 helper 到 $SYS_HELPER（sudo）"
    sudo install -Dm755 "$HELPER" "$SYS_HELPER"
    sudo setcap cap_net_admin+ep "$SYS_HELPER"
    getcap "$SYS_HELPER" 2>/dev/null || /usr/bin/getcap "$SYS_HELPER" || true
  elif [ "$KMAJ" -lt 6 ] || { [ "$KMAJ" -eq 6 ] && [ "$KMIN" -lt 6 ]; }; then
    warn "内核 $KVER < 6.6：普通用户订阅 proc connector 需要 CAP_NET_ADMIN，请加 --setcap 重新运行，否则会自动回退到轮询。"
  else
    say "内核 $KVER ≥ 6.6：普通用户即可订阅 proc connector，不需要 setcap"
  fi
fi

mkdir -p "$CONFDIR/proc-indicator"
if [ ! -e "$CONFDIR/proc-indicator/config.toml" ]; then
  cp "$SRC/data/default-config.toml" "$CONFDIR/proc-indicator/config.toml"
  say "已生成配置文件 $CONFDIR/proc-indicator/config.toml"
else
  say "保留已有配置 $CONFDIR/proc-indicator/config.toml"
fi

mkdir -p "$HOME/.local/share/applications"
sed -e "s#@BINDIR@#$BINDIR#g" "$SRC/packaging/proc-indicator.desktop" > "$HOME/.local/share/applications/proc-indicator.desktop"

rm -f "$CONFDIR/autostart/proc-indicator.desktop"
case "$AUTOSTART" in
  desktop)
    mkdir -p "$CONFDIR/autostart"
    cp "$HOME/.local/share/applications/proc-indicator.desktop" "$CONFDIR/autostart/proc-indicator.desktop"
    say "已设置 KDE 开机自启: $CONFDIR/autostart/proc-indicator.desktop"
    ;;
  systemd)
    mkdir -p "$CONFDIR/systemd/user"
    sed -e "s#@BINDIR@#$BINDIR#g" -e "s#@APPDIR@#$APPDIR#g" "$SRC/packaging/proc-indicator.service" \
      > "$CONFDIR/systemd/user/proc-indicator.service"
    if systemctl --user daemon-reload && systemctl --user enable proc-indicator.service; then
      say "已启用 systemd --user 服务 proc-indicator.service"
    else
      warn "systemctl --user 失败（不在图形会话里？）。登录 KDE 后执行: systemctl --user enable --now proc-indicator.service"
    fi
    ;;
  none) say "未设置开机自启" ;;
esac

if [ "$START" = 1 ] && { [ -n "${WAYLAND_DISPLAY:-}" ] || [ -n "${DISPLAY:-}" ]; }; then
  if [ "$AUTOSTART" = systemd ]; then
    systemctl --user restart proc-indicator.service
  else
    nohup "$BINDIR/proc-indicator" >/dev/null 2>&1 &
  fi
  say "已启动。试试在终端里运行几个命令，右下角会出现卡片。"
fi
say "安装完成。编辑 $CONFDIR/proc-indicator/config.toml 修改黑名单/颜色，保存后自动生效。"
