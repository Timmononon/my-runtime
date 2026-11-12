# Aster

Aster 是面向 Windows 的本地 AI 助手，使用 Python 构建，支持 DeepSeek 和 OpenAI 兼容模型。可通过浏览器工作台或命令行使用。

## 主要功能

- 按项目管理对话，支持附件、历史搜索、分支和侧边聊天。
- 读写文件、运行 PowerShell / WSL、操作浏览器、提取文档内容。
- 配置不同助手，通过 Skills 和 MCP 扩展能力。
- 保存带来源的记忆，按需开启自动记忆和语义检索。
- 创建自动化任务、分配 Worker，并连接 GitHub 跟进 Issue、PR 和 CI。

## 快速开始

需要 Windows、Python 3.12+ 和 PowerShell。在项目根目录运行：

```powershell
.\scripts\Install-Aster.ps1 -PythonExe python -InstallBrowser
$asterPython = (Resolve-Path '.\.venv\Scripts\python.exe').Path

& $asterPython -m aster configure --model deepseek-flash
& $asterPython -m aster web --cwd (Get-Location).Path
```

按提示输入 API 密钥，随后会打开本地工作台。也可在“我的工作空间 → 设置”中管理模型、助手和权限。安装包自带前端，日常运行不需要 Node.js。

使用命令行执行任务：

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
