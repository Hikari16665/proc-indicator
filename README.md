# proc-indicator

在屏幕右下角用带炫光的半透明卡片，实时显示有哪些进程被创建或退出。

![预览](docs/preview.gif)

## 它能做什么

- 卡片出现在屏幕右下角、任务栏上方，从右侧滑入，停留几秒后向右滑出。
- **青色**表示进程被创建，**橙色**表示进程退出。已有进程执行了新程序时会显示为紫色；短时间内事件太多时，会合并成一张蓝紫色的提示卡。
- 鼠标可以穿过卡片，点击到下面的窗口，也不会抢走输入焦点。
- 把鼠标移到某张卡片上时，这张卡片会向右让开，移开后再回来，以免挡住屏幕上的内容。
- 同时最多显示 5 张卡片（可改）。超出上限时，最旧的卡片会渐隐消失；正常到时的卡片则向右滑出。

## 运行环境

- 基于 Arch 的发行版（例如 Arch Linux、Nyarch Linux）
- 推荐在 KDE Plasma 下使用，Wayland 或 X11 都可以
- “鼠标悬停时卡片让开”目前只在 KDE 以及 X11 环境下有效。GNOME、sway 等其他 Wayland 桌面上，这项功能会自动关闭，其余功能不受影响

## 安装

克隆仓库并进入项目目录后，运行：

```bash
git clone https://github.com/Hikari16665/proc-indicator.git
cd proc-indicator
./install.sh --deps
```

脚本会通过 pacman 安装依赖（需要输入 sudo 密码），安装程序，设置 KDE 开机自启，然后立即启动。

如果依赖已经装好，可以直接运行 `./install.sh`。也可以加上：

- `--no-autostart`：不设置开机自启
- `--systemd`：改用 systemd 用户服务来自启

程序会安装到 `~/.local/`，配置文件在 `~/.config/proc-indicator/config.toml`。

## 启动、停止和确认

安装完成后一般已经在运行。之后可以这样手动控制：

```bash
# 启动（如果提示找不到命令，请用下面的完整路径）
proc-indicator
# 或
~/.local/bin/proc-indicator

# 停止
pkill -x proc-indicator
```

如果安装时用了 `--systemd`：

```bash
systemctl --user start proc-indicator
systemctl --user stop proc-indicator
```

想确认是否正常工作：

- 在终端里随便运行一个命令，右下角应该会出现卡片。
- 如果想查看启动提示，先停掉再在终端里运行 `proc-indicator`。看到「netlink 自检通过」，说明退出码等信息都能拿到；如果提示回退到轮询，可以看下面的「常见问题」。

开机自启可以在「系统设置 → 开机与关机 → 自动启动」里开关。

## 卸载

在项目目录里运行：

```bash
./install.sh --uninstall
```

如果希望连配置一起删掉，加上 `--purge`：

```bash
./install.sh --uninstall --purge
```

## 配置

配置文件位于：

```
~/.config/proc-indicator/config.toml
```

用任何编辑器打开，改完保存即可，会自动重新加载，不用重启程序。

比较常用的选项：

```toml
[behavior]
timeout = 6.0          # 卡片停留秒数，到时后向右滑出
max_cards = 5          # 同时最多显示几张，超出时最旧的渐隐消失
hover_avoid = true     # 鼠标移到卡片上时，卡片向右让开

[appearance]
fill_strength = 1.0    # 卡片内部炫光强度，0.0 ~ 2.0（0 表示只保留边缘发光）
margin_bottom = 10     # 距离任务栏或屏幕底部的间距（像素）

[colors]
create = "#00e5ff"     # 创建：青色
exit   = "#ff8c1a"     # 退出：橙色
exec   = "#d65cff"     # 执行新程序：紫色
```

更多选项（动画时长、字体、图标主题、边距等）可以在配置文件的注释里找到。

## 黑名单和白名单

不想看到某些进程时，把它们加进黑名单。进程名可以写全称，也可以用 `*` 这样的通配符。还可以按可执行文件路径或完整命令行来匹配。

```toml
[blacklist]
names    = ["awk", "passt*", "ps", "pgrep"]
paths    = ["/usr/lib/systemd/*"]
cmdlines = ["*--type=renderer*"]
```

白名单优先于黑名单：即使某个进程在黑名单里，只要命中白名单，就仍会显示。

```toml
[whitelist]
names = ["firefox"]
```

默认已经内置了一份针对 KDE / Arch 的黑名单（systemd、PipeWire、常见的浏览器后台子进程等），内核线程默认也会隐藏。如果不想用内置名单，把 `use_builtin` 设为 `false`。

## 常见问题

**卡片挡住了任务栏，或位置太靠下。**  
把配置里的 `margin_bottom` 调大一些，保存后会自动生效。

**弹出的卡片太多，看不过来。**  
把刷屏的进程加进黑名单，或把 `max_cards` 调小。

**退出卡片上写着「退出代码未知」。**  
多半是内核版本低于 6.6。在项目目录里再运行一次 `./install.sh --setcap`（需要 sudo），然后重新启动程序。

**背景看起来像毛玻璃，但并没有把后面的内容模糊掉。**  
这是模拟出来的半透明光效，不是真正的背景模糊。

**鼠标移上去，卡片没有让开。**  
悬停避让目前只在 KDE 和 X11 上有效。如果用的是 GNOME、sway 等其他 Wayland 环境，这项功能会自动关闭，滑入滑出和事件显示都还能用。

## 许可证

本项目基于 [MIT 许可证](LICENSE) 开源。
