# alia-runtime

Run Alia Pad carts on a PC the way the handheld runs them: the same Debian, libraries,
Python, sandbox rules, gamepad and pause behaviour, in a Docker image. For cart developers.
MIT licensed. *(Docs are in Chinese for now.)*

在电脑上按 Alia Pad 掌机的规则运行卡带：同样的 Debian、库、Python、沙箱规则、手柄和挂起行为，装在一个 Docker 镜像里。给卡带开发者用。

```bash
pip install alia-sdk          # 见 alia-devkit
acart run path/to/cart        # 需要 Docker 或 Podman
```

**状态**：设计阶段，见 [docs/design.md](docs/design.md)。

相关仓库：[alia-devkit](https://github.com/NU-Hachimi/alia-devkit)（卡带文档、SDK、`acart`）、[alia-engine](https://github.com/NU-Hachimi/alia-engine)（游戏引擎）。
