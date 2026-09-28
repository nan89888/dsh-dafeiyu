# DSH 大肥鱼当前运行版说明 / Current Runtime Overview

这份说明以 `nan89888/dsh-dafeiyu` 的当前代码和 v0.1.15 验证结果为准，专门区分“本 fork 已实现的运行逻辑”和“参考项目提供的设计/素材来源”。

This document describes the current `nan89888/dsh-dafeiyu` code and v0.1.15 validation results. It separates implemented runtime behavior from reference-project design and asset provenance.

## 实际运行链路 / Runtime path

1. DSH 插件接收真实 session、task、tool 和生命周期事件。
2. `src/companion-reducer.js` 将事件归一化为思考、工作、等待、完成或错误状态。
3. `src/helper-process.js` 启动单一桌面 Helper，并通过 `ready/pong/closed` 协议维护生命周期。
4. `runtime/helper.py` 绘制透明窗口、动画、对话历史和设置；macOS 的 Swift 代码不会与它并行启动。

1. The DSH plugin receives real session, task, tool, and lifecycle events.
2. `src/companion-reducer.js` normalizes them into thinking, working, waiting, success, or error states.
3. `src/helper-process.js` starts one desktop Helper and maintains it through the `ready/pong/closed` protocol.
4. `runtime/helper.py` renders the transparent window, animation, conversation history, and settings; macOS Swift code is not launched alongside it.

## 当前可见功能 / Visible behavior in this fork

| 中文 | English |
| --- | --- |
| 三种移动模式：跟随、安静陪伴、活泼陪伴 | Three movement modes: follow, quiet, and lively |
| 散步有一分钟预算，避免持续空走 | Walking uses a one-minute budget to avoid continuous empty pacing |
| 有限动作完整播放并阻止普通散步抢断 | Finite actions play to completion and block ordinary wandering from interrupting them |
| 长文本自动换行，历史对话区域可滚动 | Long text wraps automatically and conversation history scrolls |
| 回复显示名为“蓝色大肥鱼”，底层仍使用 Codex/DSH 会话 | Replies are labelled “蓝色大肥鱼”; the underlying Codex/DSH session remains unchanged |
| 拖拽、下落、落地和眩晕按冲击阈值处理 | Drag, fall, landing, and dizzy reactions use an impact threshold |
| 桌宠和气泡使用同一布局与窗口层级设置 | The pet and status bubble share layout and window-level settings |

## 验证边界 / Validation boundary

- 本地已完成：Node 测试、Python 测试、macOS `ready/pong` 协议烟测、打包后 Helper 解包烟测。
- Windows/Linux 的原生桌面构建和烟测由对应 GitHub Actions runner 执行；本机不是这两个系统，因此不把它们写成本地实机结果。
- 动画素材来自公开参考项目，来源和许可证见 [docs/REFERENCES.md](REFERENCES.md) 与 [ASSET_LICENSE.md](../ASSET_LICENSE.md)。

- Completed locally: Node tests, Python tests, macOS `ready/pong` protocol smoke tests, and an unpacked-package Helper smoke test.
- Native Windows/Linux desktop builds and smoke tests run on their matching GitHub Actions runners; this macOS host does not claim them as local desktop results.
- Animation assets come from public reference projects; see [docs/REFERENCES.md](REFERENCES.md) and [ASSET_LICENSE.md](../ASSET_LICENSE.md) for provenance and licensing.
