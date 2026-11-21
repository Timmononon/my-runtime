# Aster 工作台前端

React + TypeScript + Vite；所有生产资源本地打包，无 CDN、远程字体或浏览器中的模型密钥。

```powershell
npm --prefix frontend ci
npm --prefix frontend run build
npm --prefix frontend test
npm --prefix frontend run format
```

Vite 将 HTML、脚本与样式构建到 `src/aster/web/static`。这些文件随 Python 包分发，由 Electron 从本机读取。开发时可用 `npm --prefix frontend run dev` 持续构建，再重新打开桌面窗口查看修改。

桌面启动器向本机 Runtime 申请一次性票据，Electron 主进程在加载界面前将其兑换为 HttpOnly 会话 cookie。工作台使用同源请求和 `X-Aster-UI: 1` 访问 API。登录过期后关闭窗口，再打开 Aster 即可重新登录。

页面直接读取 Runtime 的消息 record、执行状态、审批与问答。SSE 使用 fetch 流和 event_id 游标；不显示内部推理。Markdown 禁止原始 HTML，外部图片不会自动请求。成果下载经鉴权 fetch 后生成临时 blob URL；静态预览在新标签页打开可信预览外壳，不嵌入 iframe。

管理页支持本地 Skills 的预览/安装/卸载、范围内的记忆记录/查询/更新/撤回/历史，以及定时、文件观察、变化检查自动化。编辑自动化保留未暴露的已有策略。默认不提供交互脚本预览授权入口；此页只签发静态预览。

单元测试覆盖请求去重、运行优先级、历史分支、SSE 解码、桌面会话登录和 Markdown 渲染。Chromium 验收从磁盘读取同一份界面资源，并使用真实 Runtime API。桌面启动、登录和退出通过 `npm --prefix desktop test` 验证。
