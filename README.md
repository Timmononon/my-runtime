# Aster

Aster 是面向 Windows 的本地 AI 助手，支持 DeepSeek 和 OpenAI 兼容模型。可通过 Electron 桌面工作台或 Python 命令行使用。

## 主要功能

- 按项目管理对话，支持附件、历史搜索、分支和侧边聊天。
- 读写文件、运行 PowerShell / WSL、操作浏览器、提取文档内容。
- 配置不同助手，通过 Skills 和 MCP 扩展能力。
- 保存带来源的记忆，按需开启自动记忆和语义检索。
- 创建自动化任务、分配 Worker，并连接 GitHub 跟进 Issue、PR 和 CI。
- 通过[飞书 CLI](docs/feishu-cli.md)操作文档、消息、日程和表格，写入按权限批准。
- 飞书群成员可以通过 @ 应用机器人提交工作请求，通过判断后创建工单并派发 Worker。[配置飞书任务来源](docs/feishu-task-sources.md)

## 快速开始

在 Windows 10 或 11 x64 上双击安装包，跟随向导安装，再从开始菜单打开 **Aster**。安装包内置 Electron、Python 和工作台，支持离线安装。[下载 Windows x64 安装包](https://github.com/Timmononon/my-runtime/releases/download/0.1.21/Aster-Setup-0.1.21-win-x64.exe) · [所有版本](https://github.com/Timmononon/my-runtime/releases)

首次启动会自动登录本机工作台。点击首页的「配置模型密钥」，在 DeepSeek 的「API Key」中填入自己的密钥，点「保存提供商」即可开始对话。其他兼容服务可以在同一页添加。之后可从「我的工作空间 → 设置 → 模型提供商」修改。飞书和 GitHub 按需连接到使用者自己的账号。

关窗后后台可继续执行任务。要完全退出，在窗口顶部选择「Aster → 退出并停止后台」。[Windows 安装与维护](docs/windows-operations.md)

### 从源代码运行

开发环境需要 Windows、Python 3.12+、PowerShell 和 Node.js 22.12+。在项目根目录运行以下命令。

```powershell
.\scripts\Install-Aster.ps1 -PythonExe python
$asterPython = (Resolve-Path '.\.venv\Scripts\python.exe').Path

& $asterPython -m aster configure --model deepseek-flash
.\scripts\Install-AsterDesktop.ps1
```

双击开发环境创建的 Aster 快捷方式即可打开工作台。[开发版启动选项](docs/windows-operations.md#本机桌面版)

需要使用网页自动化工具时，可给 Runtime 安装脚本加上 `-InstallBrowser` 以安装 Playwright 使用的 Chromium。

在命令行执行任务的示例。

```powershell
& $asterPython -m aster run --cwd (Get-Location).Path '列出当前目录，说明主要文件用途'
```

## 配置与数据

配置、对话、记忆和附件默认保存在 `~/.aster`。普通设置位于 `config.yaml`，API 密钥单独保存在 `credentials.yaml`。如需更换数据目录，在子命令前添加 `--data-dir <目录>`。

工具按配置的权限执行，需要批准的操作会在工作台等待确认。自动记忆、语义检索和 GitHub 连接默认关闭；GitHub 发布需要单独授权。模型和外部服务调用可能产生费用。

## 开发

后端代码位于 `src/aster`，前端位于 `frontend/src`，测试位于 `tests`。修改前端时需要 Node.js。

## 第三方许可

第三方组件的来源与许可证见[内置技能声明](src/aster/packages/bundled/THIRD_PARTY_NOTICES.md)、[评测声明](src/aster/evaluation/NOTICE.md)和 [BFCL 声明](src/aster/evaluation/_vendor/bfcl_v4/NOTICE.md)。
