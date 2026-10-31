# DeepEval：BFCL V4 Multi-Turn 保存结果的离线接入

验证日期：2026-10-30（北京时间）。DeepEval **4.3.2**，Python **3.12.3**，Windows x64。
DeepEval、OpenAI SDK、Pydantic、pytest 和 telemetry 依赖仅安装在项目内
`.eval-tools/deepeval-venv/`，不加入 Aster 的 pyproject，不修改 `.venv` 或全局 Python。
完整传递依赖固定于 `requirements.lock.txt`；`requirements.in` 记录直接依赖。

在项目根目录重建工具环境：

```powershell
& .\evals\bfcl_multiturn\deepeval\install.ps1
```

首次安装允许访问依赖仓库。离线运行不会下载模型或权重，不实例化被测模型和评审模型。

先用共享导出器生成新的 bundle（已有 bundle 不要覆盖）：

```powershell
.\.venv\Scripts\python.exe -m aster.evaluation.bfcl_multiturn_export `
  --campaign 'artifacts/bfcl/v4-live-smoke-20261029' `
  --label 'as-run-deepseek-flash-low' `
  --output 'artifacts/bfcl/my-multiturn-bundle'
```

真实调用 DeepEval `evaluate`，消费保存结果，不重跑模型：

```powershell
.\.venv\Scripts\python.exe evals/bfcl_multiturn/deepeval/run_offline.py `
  --bundle 'artifacts/bfcl/multiturn-frameworks-20261029/bundle' `
  --output 'artifacts/bfcl/my-deepeval-offline'
```

独立隔离测试：

```powershell
.\.venv\Scripts\python.exe evals/bfcl_multiturn/deepeval/run_offline.py `
  --test --output 'artifacts/bfcl/my-deepeval-tests'
```

所有 output 必须不存在。活动源目录只读，输出不覆盖原轨迹。`run_offline.py` 是唯一推荐入口：
它把解释器指向独立 venv，子进程使用新的临时 CWD/HOME/cache 和白名单环境，不传递任何模型 key、
代理、Confident key、dotenv 配置。主 Aster 进程的 CWD 和环境不变。worker 在首次 DeepEval import
之前安装 Python audit guard，禁用网络、子进程、用户默认 Runtime 及输出/临时目录之外的写入。
telemetry、dotenv、legacy keyfile、更新检查和 inspect 提示均关闭，文件系统为 READ_ONLY。
该守卫覆盖 Python 审计操作，不是任意 native DLL 的网络沙箱；本适配器不构造 native transport 或模型。

`adapter.py` 将一道完整 BFCL 任务转换成一个 `ConversationalTestCase`。
其 `Turn` 保留实际 user/assistant 步骤顺序，`ToolCall` 保留输入及工具结果。
DeepEval 的 Turn 不支持 tool 角色，因此 tool 消息投影进 ToolCall.output；完整原始逻辑轮次和
工具账本仍保留在 metadata。未进入的未来用户轮次保留为 not_run 边界，不伪造实际对话。
转换用的是共享导出器的脱敏投影，不改变原始历史。

`BFCLSavedTaskMetric` 是确定性的整题结果指标，读取保存的官方子检查器结果和终止状态；
状态/响应的诊断结果单独保留，不能把“状态正确但强制终止”改成成功。源记录只保存组合检查时，
不编造独立 state/response 分数。数据冲突、基础设施错误、未运行的任务保持 null 并排除评分。
`evaluate_records` 使用固定 4.3.2 的 in-memory manager + `_skip_reset=True` 执行真实 evaluate，
返回全部本地结果，跳过 cloud/local-store finalization；不修改库源码或评分器。

输出包括 `conversations.json`、原生 `framework-results.json`、`summary.json`、`auxiliary.json` 和
`offline-guard.json`。summary 引用历史请求数/token/耗时/失败原因，同时明确本次 model/judge API 为 0。
只有已经实际运行的配置身份才能解释为配置对比，标签本身不证明优化存在。

澄清质量、缺函数无效探索的辅助指标在独立 `auxiliary_observations` 中预留扩展位置，默认 score=null、
status=not_implemented，未启用 LLM-as-a-judge。原始工具调用计数只是观察量，不作为澄清质量分数。
本次四题结果回放为 2 pass / 2 fail，是历史结果重现，**不是新的模型能力成绩或完整 BFCL 总分**。
