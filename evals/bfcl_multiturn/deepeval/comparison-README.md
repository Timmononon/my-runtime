# DeepEval：固定 40 题的封存配置离线对比

`run_comparison.py` 使用已安装的 DeepEval **4.3.2** 隔离环境，读取两次已经结束的真实运行。它不调用被测模型或评审模型，不下载模型，不上传轨迹，不读取用户默认 Runtime；LLM-as-a-judge 关闭。

```powershell
.\.venv\Scripts\python.exe evals/bfcl_multiturn/deepeval/run_comparison.py `
  --baseline artifacts/bfcl/compact-dev40-c20-20261030 `
  --candidate artifacts/bfcl/compact-dev40-a3-c20-20261030 `
  --output artifacts/bfcl/dev40-a0-a3-deepeval-comparison
```

输出目录必须不存在，父目录应已存在。省略 `--candidate` 可仅离线复核单一配置；`--test --output <新目录>` 在隔离环境中运行适配器测试，可用 `--no-junit` 跳过可选 XML 输出。

A4.1 封存后，将 `--candidate` 指向其 campaign，并使用 `--candidate-label A4.1`。分别以 A0 和 A3 为 `--baseline`、显式指定 `--baseline-label A0`／`A3`，输出到两个新目录。候选使用 `readiness_prompt_v1` 时，从冻结的 `readiness_prompt.py` AST 字面量重算提示词 SHA256，核对 plan／control／report／task／run 身份、实际请求前缀、仅 main 请求和关闭控制器的配置；不会执行快照中的 Python 代码。

每道完整 Multi-Turn 任务对应一个原生 `ConversationalTestCase`，保留实际 user／assistant 顺序和工具调用、返回值。完整轨迹投影经过凭据参数脱敏；参考答案仅用于评估侧的逐轮调用数量，不提供给 Agent。

主指标复用确定性 `BFCLSavedTaskMetric`，封装保存的官方子检查器组合结果。预算终止和明确标记的 `TaskControlError` 为失败；数据冲突、基础设施异常、未运行任务保持 null。若存在未评分任务，全量主成绩及 A0／A3 主成绩差值保持 null，另列可评分子集。

三个辅助指标分别运行原生 `deepeval.evaluate`，不加入主指标权重：

| 指标 | 确定性定义 |
|---|---|
| `no_premature_calls` | 参考要求零调用的轮次，实际完整结束且没有工具调用的比例 |
| `post_blocked_continuation` | 首个零调用轮之后，参考要求执行的轮次中，通过保存的官方组合检查的比例 |
| `nonempty_turn_checks` | 所有参考要求执行的轮次中，通过保存的官方组合检查的比例 |

辅助指标不适用的任务不评分；分别输出逐任务平均值和按轮次汇总的比例。这些指标不衡量自然语言澄清的语义质量；该评审仍关闭。

比较要求同一冻结 40 题、四类各 10 题、官方 `deepseek-flash`／none、20 并发及相同预算／评分规则。读取各自 `as-run-source` 的字节并验证，当前工作区后续修改不会使 A0 失去读取资格。配置身份包含源码、模型、并发和 `agent_policy`；A3 是三项改动的组合，不能据此声称分别测出了 A1／A2 收益。

输出包括主指标与各辅助指标的原生框架结果、对话测试结构、配置身份、逐题增益／回退、用量与耗时变化、独立离线守卫及本适配器源码快照。family4 语义疑点及 Base5／LC5 严格响应路径差异单独注记，原始主分数和分母保持。

能力检查候选 `capability_check_v1` 另外绑定冻结的 `capability_gate.py` 字面量、A4.1 原生提示词 SHA、检查频率和格式失败回退策略。每个完成用户轮必须保存一次 `capability_check` 请求，之后为 `main` 或合法缺能力答复；两者共同计入原每轮 20 次预算。输入验证核对实际无工具 envelope、schema 指纹、请求号和逐轮策略，不能只改标签。格式失败必须进入原生回退路径，不能改记成缺能力而获得零调用结果。

每组结果另外输出 `missing-functions-and-gate.json`：缺函数整题成绩、阻塞轮零业务调用、后续轮／首个恢复轮官方检查、其他 30 题结果，以及能力检查 ready／missing_capability／fallback 状态和请求计数。这些分解不参与主分权重，也不把减少探索或自报缺能力当作成功。原生 `ConversationalTestCase.metadata` 保留安全检查结构与输入／响应哈希，不复制原始检查回复或秘密字段。

封存新的真实运行后，可分别与 A0、A4.1 比较（输出目录必须不存在）：

```powershell
.\.venv\Scripts\python.exe evals/bfcl_multiturn/deepeval/run_comparison.py `
  --baseline artifacts/bfcl/compact-dev40-a41-c20-20261030 --baseline-label A4.1 `
  --candidate artifacts/bfcl/compact-dev40-mf-gate-c20-20261030 --candidate-label MF-Gate-v1 `
  --output artifacts/bfcl/a41-vs-mf-gate-new-offline-review
```

Windows 隔离 worker 在导入 DeepEval 前，以 `sys.getwindowsversion` 提供真实内核版本的有界平台元信息，避免 Python 3.12 经 `platform.system()` 触发挂起的 WMI 查询。该适配只作用于离线子进程的 `platform.uname`／`win32_ver`，未知主机与 CPU 字段为空，并写入 `host-platform.json`；依赖文件、用户环境和指标定义不改。网络、子进程和默认 Runtime 守卫照常启用。

**离线复核不是新增模型能力成绩。** 已有封存结果为 A0 40 题、11 通过／29 失败，A3 40 题、9 通过／31 失败，A4.1 40 题、13 通过／27 失败。能力检查候选的数值只能在其真实运行封存后生成。
