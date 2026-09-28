# DSH 大肥鱼：参考文献与跨平台移植说明

本文记录本项目实际参考的公开仓库、借鉴的设计边界，以及本次封装和稳定性工作的验证范围。
参考仓库用于说明来源和设计启发，不代表本项目与这些项目存在官方关联。
README 的当前运行版介绍以 `src/`、`runtime/` 和本地测试为准；仓库中早期的 `docs/images/`
截图只作为历史验收素材保留，不作为当前 fork 的功能证据。

## 参考仓库

| 仓库 | 用途 | 本项目的对应实现 |
| --- | --- | --- |
| [deepseek-ai/deepseek-harness](https://github.com/deepseek-ai/deepseek-harness) | DSH 宿主、插件生命周期和会话事件的上游环境 | `src/plugin.js`、`src/companion-reducer.js`、`src/protocol.js` 将真实 session/task/tool 事件转换为桌宠状态；桌宠随 DSH Helper 启停 |
| [QCYTSN/ds-local-pet](https://github.com/QCYTSN/ds-local-pet) | 桌宠交互、移动模式、窗口落脚、动作分层和本地持久化的产品参考 | `runtime/helper.py`、`runtime/activity_director.py`、`runtime/layout_store.py` 实现三种移动模式、有限散步预算、拖拽/落地状态、历史对话和设置持久化 |
| [QCYTSN/dsh-dafeiyu](https://github.com/QCYTSN/dsh-dafeiyu) | 本项目自身的插件与发行仓库 | `src/`、`runtime/`、`native/`、`.github/workflows/` 和发布文档 |
| [PC2005-cloud/dsh-pet](https://github.com/PC2005-cloud/dsh-pet) | 当前角色动画素材与动作命名的公开来源 | `assets/pet/` 中的透明 WebP 帧；许可证随 `assets/dsh-pet-LICENSE.txt` 发布，完整素材边界见 `ASSET_LICENSE.md` |

## 已移植和加固的能力

- 动画：按素材原生帧率播放；行走起步、循环、收步分段；左右方向使用独立帧，不把右行简单镜像成左行；眨眼、观察、思考、开心、投喂、扫地、休息、拖拽、下落、落地和眩晕作为有限动作调度。
- 动作互斥：有限动作获得完整播放锁，动作中不会被普通散步抢占；下落只播放下落→落地，只有达到冲击阈值时才进入眩晕。
- 移动模式：跟随鼠标、安静陪伴和活泼陪伴有不同调度；散步受滚动一分钟最多两次的预算约束，避免持续走动或走到屏幕边缘空踏步。
- 交互：脸、头、身体、尾巴分区反馈；长按、连续戳、拖拽、抛掷、边界反弹、窗口顶部落脚和右键设置均通过同一状态机处理。
- 对话：历史记录固定高度可滚动、长文本自动换行；界面标签显示“蓝色大肥鱼”，Codex 仍是底层会话和回复接口。
- 生命周期：Helper 通过 `ready/pong/closed` 握手；stdin EOF、显式关闭、心跳超时、EPIPE、启动失败、READY 前崩溃、READY 后崩溃和背压都不会把异常传播到 DSH 主进程；重启次数有上限。
- 发布：Windows x64、Linux x64 和 macOS 产物在对应 GitHub-hosted runner 构建；最终 npm archive 会重新解包并进行 Helper 握手、视觉快照和 stdin EOF 烟测。

## 稳定性验证矩阵

| 检查 | 本地结果 | CI/发布结果 |
| --- | --- | --- |
| Python 单元测试（动画、布局、平台路径、Helper 逻辑） | `42` 项通过，`3` 项跳过 | Linux/Windows/macOS job 均执行 |
| Node 测试（协议、插件、事件、Helper 生命周期和崩溃恢复） | `91` 项通过 | PR 和发布 job 均执行 |
| Swift 核心测试 | 在 macOS 构建机执行 | macOS job 执行 |
| Linux x64 Helper | 本机不是 Linux，未冒充本地通过 | Ubuntu 22.04 原生构建、Xvfb 视觉烟测、解包后二次烟测 |
| Windows x64 Helper | 本机不是 Windows，未冒充本地通过 | Windows runner 原生 PyInstaller 构建和烟测 |
| npm archive | 本机通过源码和测试验证；跨平台二进制由 CI 组装 | Linux 组装并在 macOS runner 复验 |

## 运行和发布边界

- Windows 和 Linux 用户使用 release/npm 包内预构建 Helper，不需要自行安装 Python 或 PySide6。
- WSL2 的视觉模式通过 Windows `cmd.exe` 启动缓存到 `%LOCALAPPDATA%` 的 Windows Helper；WSL headless 模式仍使用 Linux Helper，避免把事件日志写到错误的文件系统。
- 资源只从包内 allowlist 读取；对话、布局和诊断日志写入用户目录，不上传遥测。
- macOS 仍标为实验性支持。其可视 Helper 的当前单一实现是 `runtime/helper.py`，Swift 目录保留核心逻辑和测试；不要把旧 Swift 可执行文件当成另一个并行桌宠启动。
- 本项目不替代 DSH，也不改变 Codex/DeepSeek 的底层模型接口；它只是 DSH 的桌面显示与交互插件。

## 可复现命令

```bash
# JavaScript / Python
pnpm install --frozen-lockfile
npm test
python3 -m unittest discover -s runtime/tests -t .

# macOS 核心和应用包
swift test
npm run build:helper:darwin
node scripts/test-packaged-helper.mjs

# Linux x64（在 Linux x86_64 上）
python3 -m pip install -r requirements.txt pyinstaller
npm run build:helper:linux

# Windows x64（在 Windows x64 PowerShell 上）
python -m pip install -r requirements.txt pyinstaller
npm run build:helper:windows
```

发布前再运行 `scripts/push-release.ps1`（Windows）或
`scripts/push-release.sh`（WSL），它会检查工作区、版本标签和远程分支关系，并原子推送
`main` 与 `v<version>` 标签。GitHub Actions 随后负责构建三平台产物、验证最终压缩包并创建
GitHub Release/npm 发布物。
