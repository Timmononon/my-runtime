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
脚本；默认 `identity=user`，可明确选择 `user`（本人）或 `bot`（应用机器人），不能用身份
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

### 先核实身份与目标

客户端内置“飞书助手”和CLI配置的自建应用bot是不同对象；`identity=bot` 指当前
配置应用的机器人，不代表内置助手。个人身份能看到聊天，不等于该机器人可以发送。
固定1.0.100的 [`im +chat-search`](https://github.com/larksuite/cli/blob/ac22a60a23d66c37d43a7ec6d9e99b99a4320356/shortcuts/im/im_chat_search.go)
只搜索可见的群／topic；消息搜索命中也不能被猜作用户指定的助手，需独立核实目标。
目前未验证通过CLI向内置助手发送的可用路径，不据此断言官方API绝对不支持。

## 向群自定义机器人发送纯文本

群自定义机器人的 Webhook 将消息发到创建机器人的群聊。
`feishu_cli` 的 `identity=bot` 使用的是已配置的应用机器人身份。
在目标群添加自定义机器人后，在终端配置。
`$asterData` 指向当前 Aster 使用的数据目录，`--group` 是目标群的显示名称。

```powershell
aster --data-dir $asterData feishu-webhook configure --group "项目需求群"
```

在终端提示时粘贴 Webhook URL。终端隐藏输入，Aster 将 URL 保存到所选数据目录的
`credentials.yaml`。重启 Aster 后，允许发送的对话会在首轮向模型提供 `feishu_webhook_send`。
也可通过 `tool_search` 查找该工具。调用时传入纯文本 `{"text":"任务已完成。"}`。Webhook URL 决定接收群，发送使用 Aster 的外部写入审批和执行账本。
超时或响应异常导致发送结果未知时，先在目标群核对结果，再决定下一次发送。

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

0.1.14 将已审计的 `im +messages-send` 发送前本地 `missing_scope` 拒绝记为
“未执行”，不锁住会话；真正结果未知仍须核验。核验保留历史run及status，界面
依据服务实时 `requires_reconciliation` 判断能否发新请求，不自动恢复或重试旧请求。
本人发送消息需要 `im:message.send_as_user`，应用权限与个人OAuth授权均须人工确认、按需补齐；
Aster 不自动发起OAuth，也不因缺权切换bot。

0.1.15 在终态待核验时做前台只读复查，恢复焦点时更新能力信息，混合快照有限
重读；权威解除后可发新请求，真正未决仍保护，历史run／status与原消息不改。

### 单次纯文本发送被拒绝时的判定

Aster 0.1.16 可识别机器人发送拒绝 `230002`。0.1.17 增加本人发送拒绝 `230027`。
两条规则仅适用于 `im +messages-send`，且原参数恰为一个合法 `--chat-id`、非空纯文本
`--text`、非空且最多 50 字符的 `--idempotency-key`。固定的 CLI 1.0.100 在这条路径
仅发出一次消息 `POST`，没有前置上传。
[发送实现](https://github.com/larksuite/cli/blob/ac22a60a23d66c37d43a7ec6d9e99b99a4320356/shortcuts/im/im_messages_send.go)、
[纯文本构造](https://github.com/larksuite/cli/blob/ac22a60a23d66c37d43a7ec6d9e99b99a4320356/shortcuts/im/helpers.go)。

CLI 须已启动并正常退出，stdout 无内容，进程无超时、取消或读取错误。stderr 须为
单一完整、无重复字段的 JSON 拒绝，含 `ok=false`、非空的 `error.message` 和
`error.log_id`，任意层级均无 `data` 或 `message_id`。请求的 `identity` 与响应顶层
的 `identity` 须一致，退出码与错误字段须符合对应规则。

- `bot` 发送须正常退出码 `1`，`error.type=api`、`error.subtype=unknown`、整数
  `error.code=230002`。`error.identity` 若存在，须为 `bot`。
- `user` 发送须正常退出码 `3`，`error.type=authorization`、
  `error.subtype=user_unauthorized`、整数 `error.code=230027`，以及 `error.identity=user`。

上述条件同时成立时，Runtime 将该次发送记为 `not_executed`（失败且无业务副作用）。
飞书将 `230002` 说明为应用机器人不在目标群，将 `230027` 说明为本次操作权限不足。
`230027` 本身无法定位具体缺失的 scope。请核对发送身份、应用权限和目标会话的发送限制。
[飞书开放平台发送消息文档](https://open.feishu.cn/document/server-docs/im-v1/message/create.md)。

`--dry-run` 预览请求结构，本地 scope 检查验证 CLI 可见的授权范围。实际发送能否获准，
由飞书平台按当前身份、应用权限和目标会话限制判定。
[CLI 预检流程](https://github.com/larksuite/cli/blob/ac22a60a23d66c37d43a7ec6d9e99b99a4320356/shortcuts/common/runner.go)。

超时、中断或异常响应可能意味着写入已经发生但结果未知，不能自动重新发送。
先在飞书客户端人工核对；Runtime 的未知副作用屏障仍保持生效。
缺少账号授权／scope、配置或命令错误会返回错误，
不能把“已启动 CLI”当作业务成功。CLI 原始 stdout／stderr 不另存为进程输出附件；
返回给模型的业务结果仍受正常对话记录和模型提供商的数据边界约束。
