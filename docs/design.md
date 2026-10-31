# alia-runtime 设计

> 2026-10-31，草案，待审。

## 1. 是什么

在电脑上按 Alia Pad 掌机的规则运行卡带，给卡带开发者测游戏用。目标是"电脑上没问题"尽量等于"掌机上没问题"。

```bash
acart run examples/hello-python                 # 开一个掌机分辨率的窗口，卡带在里面运行
acart run mygame --lang en --browser            # 浏览器模式：画面和手柄都在浏览器里
```

`acart run` 在开发包的 SDK（alia-sdk）里，只负责拼出 `docker run`。所有逻辑都在这个仓库构建的镜像 `ghcr.io/nu-hachimi/alia-runtime` 里。

**不做的**：
- 不模拟性能。电脑比掌机快一个数量级，性能在真机上测。
- 不模拟系统界面：没有主页、详情页、设置，只有游戏中用得到的那部分（暂停画面、快捷菜单）。
- 不模拟硬件外设：电池、温度、倾斜之类。这些是系统开发者的内部模拟器管的。
- 不跑 `.acart` 的签名和安装流程。运行的是卡带目录（开发中的形态）。

### 1.1 开发者怎么用

**不需要推送**：卡带目录直接挂进容器（只读的 `/cart`），改完文件重新 `acart run` 就是新版本。

```bash
pip install alia-sdk                  # 一次：得到 acart
acart run mygame                      # 第一次会拉镜像（几百 MB），以后秒开
#   窗口里玩；H 是 Home（挂起、"已暂停"），长按 H 是快捷菜单（继续、退出、语言）
#   卡带的输出打在终端里；关窗口或在快捷菜单里退出，容器就结束
acart run mygame --fresh              # 用一个空存档目录
acart run mygame --lang en            # 换系统语言
```

- **Python 卡带**：改了直接重跑。
- **原生卡带**：在 x86_64 的电脑上要先编一份 x86_64 的放进卡带目录（引擎已经同时发布两种架构的模块）；在 arm64 的电脑上（Apple Silicon 的 Mac）直接用给掌机编的那份。
- **存档**留在电脑上（`~/.local/share/alia-runtime/saves/<id>`），下次运行还在，和掌机上的存档互不相干。
- **用引擎的**：`alia run` 调用 `acart run`，用法不变。

**到真机上**还是原来的路：`acart --board push DIR --launch`（开发机）或者 `acart pack` 后 `acart --board install`。模拟器和真机读的是同一个卡带目录，中间不用转换。

## 2. 和掌机一致的规则从哪来

卡带的运行规则只有一处来源：alia-sdk 里的 `alia/runtime.toml`（路径、环境变量、超时、挂起信号、声音格式、手柄映射），和平台基线 `alia/platform.toml` 放在一起。掌机的启动器和这里的镜像都读它。镜像构建时装固定版本的 alia-sdk，所以**镜像版本决定了规则版本**。这个仓库不另抄规则。

## 3. 镜像

- **基础**：`debian:trixie`，和掌机同一个 Debian 版本。
- **平台基线里的包**：按 `platform.toml` 装（libc、libstdc++、EGL/GLES、Mesa、Wayland、SDL3、ALSA、Python 3.13），版本和掌机对齐。掌机上的 Mesa 来自 trixie-backports，镜像也一样。
- **SDK**：固定版本的 alia-sdk，装在系统的 Python 里，卡带能 `from alia import save`，和掌机一样。
- **我们的部分**：合成器（sway）、监督程序 `alia-runtime-supervisor`（§4）、暂停画面和快捷菜单、浏览器模式的服务端。
- **架构**：amd64 和 arm64 两份（多架构镜像）。在 arm64 的电脑上（Apple Silicon 的 Mac 经 Docker Desktop，arm64 Linux），容器原生运行 arm64，**直接跑和掌机完全相同的原生程序**。在 x86_64 的电脑上，原生卡带要另编一份 x86_64 的（引擎已经这样发布）。
- **标签**：`1.0` 指向基线 1.0 的最新镜像，`1.0-r3` 是固定的某一版。`acart run` 默认用卡带清单里 `platform` 对应的标签。

不放进镜像的：Shell、alia-inputd、alia-core、padlink，以及任何系统内部的东西。

## 4. 容器里有什么在运行

```
host                                container (alia-runtime)
────                                ──────────────────────────────────────────────
Wayland 合成器  ◄── 一个窗口 ───────  sway（嵌套，WLR_BACKENDS=wayland）
                                     ├─ 卡带（uid 1000，/cart 只读，/save，/cache）
                                     └─ 浮层：暂停画面、快捷菜单（layer-shell）
键盘 → sway → overlay 表面 ──────►  supervisor ──uinput──► 虚拟 "Alia Pad Gamepad"（1P）
真手柄 /dev/input ───────────────►  supervisor ──uinput──► 虚拟 "Alia Pad Gamepad 2"…
PipeWire ◄── socket ─────────────── 卡带的声音（media.role = Game）
/dev/dri ────────────────────────► Mesa（GPU）
```

**supervisor**（Python，容器里的 1 号程序之后的主进程）做掌机上 alia-session 和 alia-inputd 在卡带面前做的事：
- **启动**：按 `runtime.toml` 准备环境变量，在 sway 里启动卡带，等它开窗口（最多 `window_timeout_s`）。
- **挂起 / 恢复**：Home 冻结进程组（SIGSTOP），盖上"已暂停 · 按任意键继续"，按键后 SIGCONT；`pause = "game"` 的卡带先 SIGUSR1，0.3 s 后冻结，恢复后发 SIGUSR2，不盖暂停画面。
- **快捷菜单**（长按 Home）：继续、退出、语言（下次启动生效）、模拟熄屏（冻结 + 黑屏，按任意键回来）。
- **退出**：SIGTERM，3 s 后 SIGKILL，和掌机一样；卡带自己退出时容器结束，打印退出码。
- **输入**：把键盘和真手柄变成虚拟手柄（§5）。
- **日志**：卡带的标准输出和错误直接打到终端。

**卡带的隔离**：容器本身就是沙箱。`--network none`（清单 `network = true` 时才给网络），只读的 `/cart`，独立的 `/save` 和 `/cache`，卡带以非 root 用户运行，环境变量从零重建。镜像里只有平台基线里的库，卡带用了基线以外的系统库会直接加载失败，这正是我们想提前发现的。

## 5. 输入

- **虚拟手柄**：supervisor 在容器里经 `/dev/uinput` 创建和掌机同样 ID 的 "Alia Pad Gamepad"，所以 `runtime.toml` 里的 SDL 映射原样生效，卡带看到的手柄和掌机上一模一样。
- **键盘**：supervisor 在 sway 的 overlay 层放一个看不见的 1×1 表面，独占键盘，逐个键收到按下和松开（游戏收不到键盘事件，和掌机一样只有手柄）。不用 sway 的按键绑定：绑定按"当前按住的整组键"匹配，松开只认最后按下的那个键，按住 W 再按别的键，W 的松开就丢了。键名经合成器给的键盘布局（libxkbcommon）换算，和引擎直接运行时 SDL 的键名含义一致。默认映射（`runtime.toml` 的 `[keyboard]`）：

  | 键 | 掌机 |
  |---|---|
  | 方向键 | 十字键 |
  | `X` / `Z` | A（右） / B（下） |
  | `C` / `V` | X（上） / Y（左） |
  | `E` / `Q` | L / L2 |
  | `U` / `O` | R / R2 |
  | `W A S D` | 左摇杆（按住 Shift 推一半） |
  | `I J K L` | 右摇杆 |
  | `F` / `N` | L3 / R3 |
  | `Enter` / `Backspace` | START / SELECT |
  | `H` | Home（长按：快捷菜单） |

  和系统开发者的控制面板同一套（参照 Ryujinx 等电脑模拟器的习惯：左手左摇杆和左肩键，右手右摇杆和右肩键），可以用 `--keys FILE` 换。
- **真手柄**：电脑上的手柄（`/dev/input`，只读挂进容器）由 supervisor 用 SDL3 读，按位置转发。**电脑上的第一个手柄是 1P**，替代键盘；再多的排 2P、3P，规则和掌机以后接外置手柄一样（`runtime.toml`，alia-inputd 的规划）。Xbox 手柄的 A 在下面，在 Alia Pad 上就是 B，窗口标题栏显示当前按下的掌机按键，避免误会。
- **没有 uinput 权限时**：`acart run` 提前检查，给出一行 udev 规则。大部分发行版已经给登录用户开了 uinput 的权限（uaccess）。

## 6. 画面、GPU、声音

- **窗口**：嵌套的 sway 开一个窗口，分辨率默认 1280×720（掌机屏幕定了以后跟着改），`--size` 可以换，窗口可以缩放。
- **GPU**：挂 `/dev/dri`，容器里的 Mesa 驱动 Intel 和 AMD。NVIDIA 闭源驱动下 Mesa 用不了它，自动退到软件渲染（llvmpipe），提示一次；装了 NVIDIA 容器工具的可以用 `--gpu nvidia`。`--software` 强制软件渲染。
- **声音**：挂电脑的 PipeWire socket，卡带的流带 `media.role = Game`，48 kHz 立体声，和掌机一样。没有 PipeWire 时静音运行并提示。

## 7. 浏览器模式（第二期）

给 macOS、Windows，以及不想给容器开 Wayland 的人：

- 容器里的 sway 用 headless 后端，supervisor 把画面编码成 H.264（有 GPU 用 VA-API，否则软件编码），声音编码成 Opus，经 WebRTC 发给浏览器。
- 浏览器页面（容器自己提供，`http://localhost:7300`）显示画面、播放声音，用 **Gamepad API** 读手柄、用键盘事件读键盘，经 WebRTC 数据通道发回 supervisor，再进虚拟手柄。Gamepad API 在所有系统上都能读手柄，所以 macOS 和 Windows 也能用真手柄。
- 没有 GPU 时全程软件渲染：2D 和简单 3D 够用，帧率不能参考。
- 在 Apple Silicon 的 Mac 上用 arm64 镜像，原生程序直接跑。
- **Windows**：经 WSL2 的话，WSLg 提供 Wayland，第一期的窗口模式可能直接能用（GPU 经 `/dev/dxg` 和 Mesa 的 d3d12 驱动）；不行就用浏览器模式。

## 8. 存档和语言

- 存档放在电脑上的 `~/.local/share/alia-runtime/saves/<id>`，挂成容器里的 `/save`，配额按清单检查（SDK 的 `alia.save` 本来就会查）。`acart run --fresh` 用一个临时的空存档目录。
- 语言：`--lang`，默认跟随电脑的 `LANG`（`zh_CN.UTF-8` → `zh-CN`，其他 → `en`）。快捷菜单里切换，下次启动生效，和掌机一样。

## 9. Docker 和权限

- 支持 Docker 和 Podman（命令兼容）。推荐 rootless Docker 或 Podman：加入 `docker` 组等于拥有 root 权限，文档里要写明。
- 容器拿到的设备：`/dev/dri`、`/dev/uinput`、只读的 `/dev/input`（真手柄）、Wayland 和 PipeWire 的 socket。没有特权模式。

## 10. 仓库结构

```
image/            Dockerfile、包的版本锁、构建脚本
supervisor/       alia_runtime：启动、挂起、输入、浮层（Python）
web/              浏览器模式的页面（第二期）
docs/             本文、使用说明
tests/            在 CI 里跑一张测试卡带：启动、按键、挂起、退出（软件渲染，headless）
.github/          构建多架构镜像推到 ghcr.io；测试
```

## 11. 分期

1. **第一期（Linux，窗口模式）**：镜像、supervisor、键盘和真手柄、挂起和快捷菜单、声音；alia-sdk 0.2.0 加 `acart run`；alia-engine 的 `alia run` 改成调用它。先做一个最小验证：容器里的嵌套 sway 能在电脑的 Wayland 上开窗、用 GPU 渲染、uinput 手柄能被卡带的 SDL 看到。
2. **第二期（浏览器模式）**：WebRTC 画面和声音、Gamepad API、Mac 和 Windows 的说明。
3. **以后**：掌机屏幕定了改默认分辨率；外置手柄的规则随 alia-inputd 一起落地；也许支持直接运行 `.acart`（容器里用 squashfuse 挂载）。

## 12. 待定

- 引擎 `alia run` 现在的键位如果不同，统一成上面这套（已问引擎侧）。
- 浮层用什么画：倾向用 Python 加一个很小的 layer-shell 客户端，不引入 Qt。
- 镜像大小：预计压缩后 300–400 MB（Mesa 和 LLVM 占大头）。
