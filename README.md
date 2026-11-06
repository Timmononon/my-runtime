# Aster

Aster 是面向 Windows 的本地 Python Agent Runtime，支持 DeepSeek Responses 和标准 OpenAI 兼容 Responses / Chat Completions 协议。浏览器工作台与 CLI 共用执行核心，提供对话、工具执行、会话管理、记忆和自动化。

0.1.11 增加工单与 Worker、GitHub 事件接入、独立 DeepSeek 任务判断，以及后台任务的人工作业待办。GitHub 来源默认关闭、只读且禁止发送；启用任务模式后，新事件依次经过来源授权、规则和模型判断。

## 功能

- **项目与对话**：按项目组织会话，支持附件、历史搜索、分支、侧边聊天、归档和永久删除；每个已保存会话固定所属助手。
- **工具执行**：文件检索与编辑、PowerShell / WSL 进程、HTTP 请求、浏览器操作，以及 PDF、DOCX、XLSX、PPTX 内容提取。
- **上下文管理**：自动压缩、手动本地压缩和模型摘要压缩；保留原始历史，显示当前模型上下文使用率。
- **Agent 与扩展**：可配置助手指令、模型和权限，支持 Skills、MCP、Hooks 与命名提示词模板。
- **记忆与检索**：带来源和作用域的结构化记忆；可选远程 embedding 或本地 SentenceTransformers 模型，包括离线 Safetensors 权重和 CPU/GPU 选择。
- **自动记忆**：可选 Jev 逐轮判断，由 DeepSeek 提取有用户原文依据的事实，复用现有记忆权限和存储。
- **自动化与后台服务**：持久化定时任务、目录监听、心跳、运行队列，以及 HTTP/SSE 接口。
- **任务与 Worker**：持久工单、明确分配的多个 Worker、独立会话与工作目录、结果汇总，以及集中批准、回答、取消和重试。
- **GitHub 协作**：读取 Issue、PR、讨论和 GitHub Actions，独立判断是否开展工作，跟进已关联任务，准备本地评论或 PR 草稿并审阅发送。
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

## 任务与 GitHub

### 工单与 Worker

从侧栏“任务”创建工单，填写目标并明确划分 Worker 的工作范围。每个 Worker 使用独立会话和工作目录，继承来源会话的模型、权限、预算与选中 Skill；只读 Worker 在运行时受到限制。任务页汇总实际运行结果，并显示待批准、待回答和待核验事项。

普通对话中的主模型也可通过 `tool_search` 发现 `task_delegate` 并提交一组 Worker 目标。`task_list`、`task_get` 查询进度，`task_cancel` 取消工作。继续现有 Worker 时，从任务页追加要求；重试遵守原授权、剩余预算和副作用回执约束。

Worker 的目录位于来源会话获准的工作空间内，不自动复制完整仓库或聊天历史。多个 Worker 修改共享文件时，应明确各自负责的文件。工作目录分开用于组织产物，文件、Shell 和第三方 MCP 的实际能力仍由权限控制，这不是操作系统沙箱。

### 连接 GitHub 来源

一个 GitHub 来源绑定 `owner/repo` 和来源会话。准备本地修改前，先自行 clone 或准备对应的本地 checkout，再将来源会话放在该工作目录；绑定远程仓库不会自动 clone、pull 或同步本地文件。

1. 从侧栏“GitHub”添加来源，填写 `owner/repo` 并选择来源会话。公开仓库可匿名读取；私有仓库或发布需要独立配置具有相应仓库权限的 Token。
2. 使用“检查连接”和“立即读取”确认来源。初始模式为 `read_only`，后台轮询和发送默认关闭；首轮建立历史基线，不把全部旧事件转换成新任务。
3. 开展自动任务前，明确设置允许的人工账号、机器人账号、`bot_login` 与事件观察范围，再启用后台轮询和任务模式。
4. 在“任务判断模型”中配置独立模型、处理范围和预算。需要向 GitHub 发布时，另行开启审阅后发送并确认具体草稿。

Token 只保存到数据目录的独立 `credentials.yaml`，页面只显示是否已配置，不回显或复制到 Worker 快照。查看和保存配置不请求 GitHub 或模型；只读来源可以获取事件，但不创建 Worker 或调用判断模型。

支持 Issue、PR、普通评论、代码审查评论、审阅以及 GitHub Actions 状态。获准人工账号可通过配置的 `@bot_login` 或 `/aster` 明确请求工作；其他新候选还受事件观察开关与本地任务规则约束。规则要求匹配所有 `contains` 片段，默认 `observe` 只记录，`auto` 才接收符合规则的候选，`off` 关闭普通主动候选。未经授权的外部正文不提供身份或执行权限。

### 独立判断与任务跟进

```text
GitHub / Actions → 来源和规则检查 → 独立判断模型 → process → 默认 Worker
                                                └→ ignore：记录原因
```

判断节点默认使用现有内置 DeepSeek 提供商、`deepseek-flash` 和 `none` 推理强度，可独立选择其他已配置提供商及模型，不改变聊天或 Worker 的模型选择。它以无工具调用输出 `process/ignore` 和原因，判断是否开始工作；调查范围和需要补充的信息由 Worker 在执行时确认。判断与 Worker 运行可能产生所选提供商的 API 费用。

判断超时、输出不完整、配置不可用、禁用或额度耗尽时，保存失败状态，不以“忽略”代替失败，也不绕过判断执行。重复事件复用原回执；中断和结果不明的请求不会自动重放。任务模式下的 GitHub 自动触发默认创建一个 Worker；多个 Worker 通过任务页或主对话明确分配。

已有讨论与工单关联后，获准后续消息继续同一项工作。机器人和 CI 只更新仍有效的授权任务，不自行唤醒已经停止的工作。获准人工账号的独立 `/aster stop` 指令可停止已关联工单。仓库、来源凭据或权限变化会使旧授权失效，不能用旧绑定继续执行。

### 批准、工具与草稿

后台任务 Worker 遇到需要批准的操作或需要回答的问题时，会等待工作台任务待办处理。它仍保留无人值守分类，工具权限没有自动增加；明确拒绝、Skill、只读角色及未知副作用限制继续生效。来源的“允许审阅后发送”只控制 GitHub 发送能力，不授予本机文件写入或 Shell 权限。

Worker 获得经过授权检查的 GitHub 来源 ID、仓库、讨论编号和工作目录。`github_read` 的 `resource="sources"` 可离线查询当前会话或工单可用的来源；仓库名和 Issue ID 不能替代内部 `source_id`。读取或准备草稿时，只有当前授权范围内恰好一个来源才可省略 `source_id`；明确填错的 ID 会返回错误提示。展开对话里的失败工具行，可查看来源、参数、权限、HTTP 状态、文件或进程错误等安全原因。

评论和 PR 先准备为可查看、可编辑的本地草稿。`github_prepare` 仅准备草稿；`github_publish` 或界面发送操作发布精确审阅的版本。PR 使用同一仓库中已存在的远程分支，代码提交和分支推送由使用者另行完成；当前没有自动 commit、push、提交 PR 或合并 PR 的完整流程。来源写入开关与工具批准、凭据和草稿版本检查共同决定能否发送。

超时、异常成功响应或连接中断后，评论或 PR 可能已经创建。结果未知时保留原回执，通过只读核验查找同一账号、标记和内容；没有找到不能证明没有发生，也不会换请求 ID 自动重发。旧失败记录和原始对话保持，升级不把失败改成成功。

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

### Jev 自动记忆（0.1.6）

在“设置 → 搜索与记忆”保存 Jev API Key、选择 DeepSeek 提取模型并开启自动记忆。新安装默认关闭；它与 embedding 使用独立开关，不需要先配置 embedding。密钥保存在数据目录的独立凭证文件中，不通过管理接口回显。

普通用户对话成功完成后，每轮最多调用一次 Jev；达到阈值后，再调用一次 DeepSeek 提取事实。判断会发送本轮用户文本和有限对话语境给 Jev，通过后由 DeepSeek 提取，可能产生 API 费用。默认判断模型为 `jev-1.14.0`、阈值为 `0.7`，整个记忆步骤的超时为 30 秒。

事实必须有本轮用户原文依据；助手回复和附件正文不作为事实来源。临时侧聊、无人值守任务及失败的对话不自动采集，也不回填全部历史。精确重复、已撤回事实和冲突值会跳过，不静默覆盖旧记忆；权限、取消和来源校验仍生效。自动记忆失败不改变已完成的聊天结果。

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
