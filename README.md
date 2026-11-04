# alia-runtime

Run Alia Pad carts on a PC the way the handheld runs them: the same Debian, libraries,
Python, sandbox rules, gamepad and pause behaviour, in a Docker image. For cart developers.
MIT licensed. *(Docs are in Chinese for now.)*

在电脑上按 Alia Pad 掌机的规则运行卡带：同样的 Debian、库、Python、沙箱规则、手柄和挂起行为，装在一个 Docker 镜像里。给卡带开发者用。

```bash
pip install alia-sdk          # 见 alia-devkit
acart run path/to/cart        # 需要 Docker 或 Podman
```

**相机**：清单里 `[permissions] camera = true` 的卡带有一个模拟的相机（和掌机同一套协议）：

```bash
acart run path/to/cart                          # 掌机相机录的一段（默认），按掌机传感器的样子出原始帧
acart run path/to/cart --camera scene           # 测试场景：色卡、灰阶、移动的圆（每次一样）
acart run path/to/cart --camera webcam          # 这台电脑的摄像头（Linux：/dev/video0；--browser：浏览器的摄像头）
acart run path/to/cart --camera none            # 没有相机（掌机没装相机时的回答）
acart run path/to/cart --camera-permission ask  # 像掌机第一次那样弹询问（deny：直接拒绝）
```

**状态**：见 [docs/design.md](docs/design.md)。

相关仓库：[alia-devkit](https://github.com/NU-Hachimi/alia-devkit)（卡带文档、SDK、`acart`）、[alia-engine](https://github.com/NU-Hachimi/alia-engine)（游戏引擎）。
