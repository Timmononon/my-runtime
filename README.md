# Aster

Aster 是面向 Windows 的本地 Python Agent Runtime，支持 DeepSeek Responses 和标准 OpenAI 兼容 Responses / Chat Completions 协议。浏览器工作台与 CLI 共用执行核心，提供对话、工具执行、会话管理、记忆和自动化。

## 功能

- **项目与对话**：按项目组织会话，支持附件、历史搜索、分支、侧边聊天、归档和永久删除；每个已保存会话固定所属助手。
- **工具执行**：文件检索与编辑、PowerShell / WSL 进程、HTTP 请求、浏览器操作，以及 PDF、DOCX、XLSX、PPTX 内容提取。
- **上下文管理**：自动压缩、手动本地压缩和模型摘要压缩；保留原始历史，显示当前模型上下文使用率。
- **Agent 与扩展**：可配置助手指令、模型和权限，支持 Skills、MCP、Hooks 与命名提示词模板。
- **记忆与检索**：带来源和作用域的结构化记忆；可选远程 embedding 或本地 SentenceTransformers 模型，包括离线 Safetensors 权重和 CPU/GPU 选择。
- **自动化与后台服务**：持久化定时任务、目录监听、心跳、运行队列，以及 HTTP/SSE 接口。
- **执行记录**：批准、取消、中途补充、检查点、用量与性能时间线，以及副作用待核验状态。

## 快速开始

需要 Windows、Python 3.12+ 和 PowerShell。浏览器工具需要 Playwright Chromium；安装时添加 `-InstallBrowser` 可同时安装。

在项目根目录打开 PowerShell：

```powershell
.\scripts\Install-Aster.ps1 -PythonExe python -InstallBrowser
$asterPython = (Resolve-Path '.\.venv\Scripts\python.exe').Path
$workspace = (Get-Location).Path

& $asterPython -m aster doctor
& $asterPython -m aster configure --model deepseek-flash
& $asterPython -m aster web --cwd $workspace
```

`configure` 隐藏输入 API 密钥；`doctor` 不发送模型请求。设置页面也可添加和管理模型提供商。正常发送任务会调用所选模型，并可能产生 API 费用。

安装脚本根据 `requirements.lock` 安装到项目虚拟环境；可通过 `-VenvPath` 指定其他环境。仓库包含构建后的前端，日常运行不需要 Node.js。

CLI 任务与交互模式：

```powershell
& $asterPython -m aster run --cwd $workspace '列出当前目录，说明主要文件用途'
& $asterPython -m aster run --cwd $workspace
& $asterPython -m aster sessions --json
& $asterPython -m aster run --session 'SESSION_ID' '继续检查结果'
```

交互模式输入 `/exit` 或 `/quit` 退出。也可运行 `.\scripts\Open-Aster.ps1 -Cwd $workspace` 打开工作台。

## 工作台

侧栏按项目组织对话。项目行的加号打开新对话，可在首次发送或上传前选择助手；已有对话显示其所属助手。点击左下“我的工作空间 → 设置”管理提供商、Agent、MCP、Hooks、权限、搜索、记忆及已归档对话。

主对话右上角“…”菜单可打开右侧聊天面板。“收起”或 Esc 保留消息、草稿、附件和正在执行的任务；“×”在确认后永久删除侧边聊天。普通分支会创建独立对话。

输入框旁的环形指示显示当前上下文使用率，点击可查看估算明细。“+”菜单中的本地压缩不调用模型；模型摘要压缩使用当前模型并计入用量。两者都保留原始历史。

工作台默认监听 `127.0.0.1:8765`，由一次性登录票据建立 HttpOnly 会话 Cookie；浏览器不保存 Runtime token 或模型密钥。重新登录可再次运行 `aster web`。

## 配置与数据

默认数据目录为用户主目录下的 `.aster`。目录优先级为 `--data-dir` → `ASTER_DATA_DIR` → `ASTER_HOME` → `~/.aster`。显式 `--data-dir` 必须放在子命令之前：

```powershell
$asterData = Join-Path $env:USERPROFILE 'AsterData'
& $asterPython -m aster --data-dir $asterData configure --model deepseek-flash
& $asterPython -m aster --data-dir $asterData web --cwd $workspace
```

主要数据文件：

```text
<data_dir>/
  config.yaml          # 模型、预算和全局权限
  credentials.yaml     # API 密钥，与普通配置分开保存
  INSTRUCTIONS.md      # 全局指令
  agents/              # 助手指令与配置
  projects/            # 项目规则与配置
  skills/              # 已安装技能
  memory/              # 按助手和项目隔离的记忆
  runtime.db           # 会话、运行记录与自动化
  artifacts/           # 附件、截图和其他产物
```

配置按全局、Agent、项目、本次运行的顺序合并。每次运行固定提供商、模型、推理强度和选中 Skill，运行中修改选项只影响后续任务。密钥只写入独立凭据文件，HTTP 接口不回显密钥。

全局 `config.yaml` 示例：

```yaml
model: deepseek-flash
reasoning_effort: low
max_output_tokens: 4096
runtime:
  max_iterations: 20
  max_tool_calls: 40
  deadline_seconds: 900
permissions:
  ask_tools: [shell_exec, process_exec]
```

语义记忆默认关闭。远程 embedding 需要外部调用授权；本地模型按需加载，不自动下载模型。安装本地 embedding 依赖可运行 `& $asterPython -m pip install '.[local-embedding]'`，然后在设置中选择已下载的模型目录和运行设备。

## 权限与恢复

默认读取操作可执行；写入、Shell 和有外部影响的动作需要批准。`allowed_tools` 定义工具可用范围；自动执行由全局 `permissions.auto_approve_tools` 显式授予，Agent 与项目只能收紧权限。Shell 和第三方 MCP 获准后拥有本机进程权限。

同一数据目录只有一个运行服务，同一会话串行执行。后台服务支持启动、状态查询与停止：

```powershell
& $asterPython -m aster daemon start
& $asterPython -m aster daemon status
& $asterPython -m aster daemon stop
```

结果未知的外部动作不会自动重放。遇到 `needs_reconciliation`，先核对真实文件或外部回执，再登记结果并恢复：

```powershell
& $asterPython -m aster reconcile 'EXECUTION_ID' --content '已核验的实际结果' --evidence '核验方式与回执标识'
& $asterPython -m aster recover 'RUN_ID'
```

交付物默认静态预览并禁用脚本。交互预览需要显式授权，不能视为完整网络沙箱。项目不包含桌面鼠标键盘控制、任意 Office 排版编辑或自动多 Agent 分工。

## 备份

```powershell
& $asterPython -m aster daemon stop
& $asterPython -m aster backup '.\aster-backup.zip'
& $asterPython -m aster restore '.\aster-backup.zip' --destination '.\aster-restored'
```

完整备份包含配置、会话、记忆、产物和 `credentials.yaml`，应作为含密钥的私密文件保存。恢复目标必须是不存在的新目录；用户工作目录和浏览器登录状态不包含在备份中。升级前建议停止任务并备份。

## 开发

Python 检查和离线测试：

```powershell
& $asterPython -m pip check
& $asterPython -m ruff check src tests
& $asterPython -m pytest -m 'not live' -q --tb=short
```

测试使用临时数据目录；浏览器、Windows 集成及可选依赖场景可能需要额外环境。`live` 测试需要显式配置凭据和 API 预算。

BFCL 提供独立 CLI：`python -m aster bfcl --help` 与 `python -m aster bfcl v4 --help`，支持输入校验、版本固定规则下的离线评测和保存结果导出；原 `evaluate` 入口保持。先安装可选依赖 `& $asterPython -m pip install '.[bfcl]'`。V4 使用固定 revision 的检查器与模拟器，并校验来源、题目和 split 的身份摘要；离线检查或合成场景通过不代表真实模型能力成绩、完整 BFCL 榜单或所有能力均已实测。

真实模型运行需要独立测试凭据目录及显式请求预算，不复用隐式用户 Runtime；新增可选控制策略默认关闭。[promptfoo](evals/bfcl_multiturn/promptfoo/README.md) 与 [DeepEval](evals/bfcl_multiturn/deepeval/README.md) 适配器需按各自锁文件独立安装工具环境，使用前还要准备所需的本地题库、历史轨迹或可校验的保存结果 bundle。仓库中的历史路径示例不代表新检出自带这些输入，保存结果对比也不会重新运行模型。

修改前端需要 Node.js：

```powershell
npm --prefix frontend ci
npm --prefix frontend run test
npm --prefix frontend run build
```

源码位于 `src/aster`，前端位于 `frontend/src`，测试位于 `tests`，安装与维护脚本位于 `scripts`。

## 第三方许可

随附资产的来源与许可证见[内置技能声明](src/aster/packages/bundled/THIRD_PARTY_NOTICES.md)及[评测声明](src/aster/evaluation/NOTICE.md)。对应许可包括 [Apache-2.0 技能许可](src/aster/packages/bundled/webapp-testing/LICENSE.txt)、[PDF 读取技能 MIT 许可](src/aster/packages/bundled/pdf-reader/LICENSE.txt)和[评测 MIT 许可](src/aster/evaluation/LICENSE-KOCORO.txt)。

BFCL V4 的来源、适配边界与 Apache-2.0 许可见 [BFCL NOTICE](src/aster/evaluation/_vendor/bfcl_v4/NOTICE.md) 和 [LICENSE](src/aster/evaluation/_vendor/bfcl_v4/LICENSE)。
