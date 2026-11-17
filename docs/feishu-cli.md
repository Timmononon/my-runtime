# 飞书 CLI

Aster 通过 `feishu_cli` 工具直接调用[官方飞书 CLI](https://github.com/larksuite/cli)，
固定使用 npm 包 [`@larksuite/cli@1.0.100`](https://www.npmjs.com/package/@larksuite/cli/v/1.0.100)。
首版支持文档、消息、日程和表格的部分读写命令，不包含群消息监听或自动分配 Worker。
安装和授权由你在本机完成，使用下面的只读状态检查核对当前服务。

## 安装

安装阶段需要 Node.js 与 npm。在 Aster 项目根目录运行：

```powershell
.\scripts\Install-FeishuCLI.ps1 -DryRun
.\scripts\Install-FeishuCLI.ps1
```

默认数据目录遵循 `-DataDir` → `ASTER_DATA_DIR` → `ASTER_HOME` → `~/.aster`。
使用自定义数据目录时，安装与运行 Aster 需选择同一个目录：

```powershell
$asterData = 'D:\AsterData'
.\scripts\Install-FeishuCLI.ps1 -DataDir $asterData
$asterPython = (Resolve-Path '.\.venv\Scripts\python.exe').Path
& $asterPython -m aster --data-dir $asterData web --cwd (Get-Location).Path
```

安装脚本固定版本，仅安装官方 npm 包及其 native binary。
可执行文件位于 `<data_dir>/tools/feishu-cli/lark-cli.exe`，npm 依赖和锁文件保留在
同目录的 `npm-package/` 下。不改系统 PATH，不运行账号设置向导，也不向其他
Agent 安装官方附带的26个 Skills。安装失败不代表已完成接入；未安装时工具会返回配置错误。

## 人工配置与授权

在终端使用同一份二进制。下面以默认数据目录为例，自定义目录时替换 `$asterData`：

```powershell
$asterData = Join-Path $HOME '.aster'
$feishu = Join-Path $asterData 'tools\feishu-cli\lark-cli.exe'
$env:LARKSUITE_CLI_CONFIG_DIR = Join-Path $asterData 'feishu-cli'
& $feishu --version
& $feishu config init --help
& $feishu auth login --help
```

上面的配置目录与 Aster 工具固定使用的目录一致，仅对当前终端生效。
人工终端也需清除已有 App ID／Secret、token、profile 等 CLI 账号环境覆盖，
并避免携带其他 Agent 上下文；工具会清除这些覆盖，不会沿用它们。
不要读取其他 Agent 或全局默认的 CLI 应用配置。本次为 Aster 单独设置应用，
由你运行向导，在浏览器同意后创建／绑定；随后按实际需要授权，例如日历域：

```powershell
& $feishu config init --new
& $feishu auth login --domain calendar
& $feishu auth status
```

日历域仅为示例，请在授权页核对具体权限；文档或其他业务需要相应权限。
1.0.100 的 `auth login` 不带范围参数或使用 `--recommend` 都会申请全部已知业务域，
因此先按业务选 `--domain`，需要更细范围时按帮助使用 `--scope`。
配置目录隔离应用／profile 的选择；认证仍由官方 CLI 的系统 keychain 管理，
service 为 `lark-cli`，按 App ID／用户 Open ID 绑定，同一个 App ID 可能共享官方
凭据。不把飞书令牌或 App Secret 填入 Aster 的提供商 `credentials.yaml`、对话
或工具参数。机器人身份还需配置应用本身的权限。

## 在 Aster 中使用

先通过 `tool_search` 找到 `feishu_cli`。参数 `argv` 是命令参数数组，不是 PowerShell
脚本；通过 `identity` 明确选择 `user`（本人）或 `bot`（应用机器人），不能用身份
切换绕过账号权限。0.1.13 起仅增加精确 `auth status` 的只读诊断例外；
`auth login`、其他授权管理、`config`、`profile`、安装和更新仍只在人工作业阶段执行。

先看帮助，再读取日程或文档，例如：

```json
{"argv":["calendar","+agenda","--help"],"identity":"user"}
```

```json
{"argv":["calendar","+agenda"],"identity":"user"}
```

```json
{"argv":["docs","+fetch","--doc","https://example.feishu.cn/docx/DOCUMENT_TOKEN","--doc-format","markdown"],"identity":"user"}
```

已获用户明确授权的文档创建属于外部写入，执行前仍按 Aster 当前权限进入审批：

```json
{"argv":["docs","+create","--doc-format","markdown","--title","项目周报","--content","# 本周进展\n完成需求梳理。"],"identity":"user"}
```

示例使用占位链接，不会自动运行。命令和参数以固定版本的 `--help` 为准；本地
`schema` 可查询接口字段。官方 CLI 支持的命令不代表 Aster 全部放行；适配器只接受
明确列入名单的业务命令及参数，不开放任意原始 API、Shell、文件下载或导出命令。
内联内容中的本地图片、白板／HTML资源文件引用及 `reference-map` 本地 `path`
不开放；纯文本、普通Markdown链接、受支持的HTTP(S)媒体和 `reference-map`
内联 `data` 保留，不能借内联内容扩大本地文件权限。

## 授权错误与当前服务诊断（0.1.13）

在 Aster 中可用下面的精确参数检查当前执行上下文的本地连接状态，不接受额外
`auth` 子命令、flag、环境变量或账号配置覆盖：

```json
{"argv":["auth","status"],"identity":"user"}
```

结果只提供是否安装、应用是否配置、user／bot 是否可用、令牌状态和已授权 scope
名称／数量，以及可选匿名绑定指纹；不返回账号ID、令牌或原始认证输出。
`business_access_verified=false` 表示尚未验证真实业务访问，不是“未授权”的结论。
即使本地显示 `ready`，也需核对正在运行的 daemon 是否能用同一身份执行只读业务。

管理客户端可在支持 `feishu_cli_status_v1` 的同一个运行中服务上显式调用
`POST /feishu/check`。空请求或 `{}` 只做本地检查；只有
`{"verify_business":true}` 会追加一次固定的 `calendar +agenda`、`identity=user`
只读验证。只有该调用成功，`business_access_verified` 才为 `true`；响应不含日程
内容。接口不调用模型、不新建对话／运行或写数据库，也不允许任意目标／命令。
认证与完整字段见[服务接口](service-api.md#feishu-cli-diagnostics-0113)。

遇到 `token_missing` 时，先比较实际 daemon 与前台终端的诊断；不要仅凭错误认定
缺 scope 或要求重新 OAuth。个人资源继续使用 `user`，不能因认证失败自动切换到
`bot`。服务进程中的失败、前台成功和本地 `ready` 是不同证据，应分别保留。

## 执行与结果

只读分类来自明确名单，未知命令不会因名称像 `get`／`list` 而获准。
发消息、创建或修改文档等业务写入使用现有 Runtime 权限、审批与执行账本；CLI 自身
要求额外确认时也会停下，不会自动追加确认参数或静默重试。审批并不扩大飞书账号本身的权限。

超时、中断或异常响应可能意味着写入已经发生但结果未知，不能自动重新发送。
先在飞书客户端人工核对；Runtime 的未知副作用屏障仍保持生效。
缺少账号授权／scope、配置或命令错误会返回错误，
不能把“已启动 CLI”当作业务成功。CLI 原始 stdout／stderr 不另存为进程输出附件；
返回给模型的业务结果仍受正常对话记录和模型提供商的数据边界约束。
