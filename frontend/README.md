# Aster 工作台前端

React + TypeScript + Vite；所有生产资源本地打包，无 CDN、远程字体或浏览器中的模型密钥。

```powershell
npm --prefix frontend ci
npm --prefix frontend run build
npm --prefix frontend test
npm --prefix frontend run format
```

构建输出为 `src/aster/web/static/index.html` 和 `assets/`，由真实 Runtime 同源提供。运行 `aster web` 打开工作台。开发使用 `npm --prefix frontend run dev`（即 `vite build --watch`），然后刷新已打开的 daemon 页面；不要单独打开 Vite 开发端口，也不要放宽生产 Origin 检查来做代理。

CLI 签发的一次性 ticket 通过 URL fragment 到达页面，读取后立即清除，再交换 HttpOnly 会话 cookie。所有 API 请求使用同源 cookie 与 `X-Aster-UI: 1`。前端不持久保存 ticket、API token 或供应商凭证。登录过期时重新执行 `aster web`。

页面直接读取 Runtime 的消息 record、执行状态、审批与问答。SSE 使用 fetch 流和 event_id 游标；不显示内部推理。Markdown 禁止原始 HTML，外部图片不会自动请求。成果下载经鉴权 fetch 后生成临时 blob URL；静态预览在新标签页打开可信预览外壳，不嵌入 iframe。

管理页支持本地 Skills 的预览/安装/卸载、范围内的记忆记录/查询/更新/撤回/历史，以及定时、文件观察、变化检查自动化。编辑自动化保留未暴露的已有策略。默认不提供交互脚本预览授权入口；此页只签发静态预览。

单元测试验证不确定提交的 request_id 复用、运行优先级、流缓存去重、完整原始消息分支边界、逐字节 SSE/CRLF 解码、票据即时清除及安全 Markdown。真实浏览器验收由项目的本机 fixture 和 Playwright 测试承担，fixture 不进入生产 bundle。
