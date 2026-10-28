import type { Performance } from "./types";
const known = (value: number | null | undefined) =>
  value == null ? "未知" : value.toLocaleString();
export default function ExecutionTimeline({ value }: { value: Performance }) {
  const cache = value.cache_summary,
    cost = value.cost_estimate;
  return (
    <section className="execution-timeline" aria-label="执行时间线与性能">
      <h3>缓存与成本</h3>
      <dl className="metric-list">
        <dt>缓存命中 token</dt>
        <dd>{known(cache?.cache_hit_tokens)}</dd>
        <dt>缓存未命中 token</dt>
        <dd>{known(cache?.cache_miss_tokens)}</dd>
        <dt>命中比例</dt>
        <dd>
          {cache?.hit_ratio == null
            ? "未知"
            : `${(cache.hit_ratio * 100).toFixed(1)}%`}
        </dd>
        <dt>估算费用</dt>
        <dd>
          {cost?.status === "estimated" && cost.amount != null
            ? `${cost.amount} ${cost.currency || ""}`
            : "未知"}
        </dd>
      </dl>
      <p className="fine-print">
        {cache
          ? `${cache.reported_requests}/${cache.requests} 次请求报告缓存用量；${cache.unknown_requests} 次未知。`
          : "供应商尚未报告缓存用量。"}
        {cost?.status !== "estimated" &&
          " 价格可在用户目录 prices.yaml 配置；未知价格不按零计算。"}
      </p>
      {!!value.batches?.length && (
        <>
          <h3>工具批次</h3>
          {value.batches.map((batch) => (
            <details className="timeline-batch" key={batch.event_id}>
              <summary>
                {batch.parallel ? "并行" : "顺序"}批次 · {batch.count} 个工具 ·{" "}
                {batch.duration_ms == null
                  ? "耗时未知"
                  : `${(batch.duration_ms / 1000).toFixed(2)} s`}
              </summary>
              <p className="fine-print">最大并发 {batch.max_concurrency}</p>
              {batch.tools.map((tool) => (
                <div className="timeline-tool" key={tool.tool_call_id}>
                  <code>{tool.name}</code>
                  <span>{tool.outcome || "尚无结果"}</span>
                  <small>
                    {tool.duration_ms == null
                      ? "耗时未知"
                      : `${(tool.duration_ms / 1000).toFixed(2)} s`}
                  </small>
                </div>
              ))}
            </details>
          ))}
        </>
      )}
      <h3>执行时间线</h3>
      {value.timeline?.length ? (
        <ol>
          {value.timeline.map((event) => (
            <li key={event.event_id}>
              <time>+{(event.offset_ms / 1000).toFixed(2)} s</time>
              <div>
                <strong>{event.type}</strong>
                <p>
                  {[
                    event.phase,
                    event.name,
                    event.model,
                    event.outcome,
                    event.purpose,
                  ]
                    .filter(Boolean)
                    .join(" · ")}
                </p>
                {event.duration_ms != null && (
                  <small>{(event.duration_ms / 1000).toFixed(2)} s</small>
                )}
                {event.before_tokens != null && event.after_tokens != null && (
                  <small>
                    上下文估算 {known(event.before_tokens)} →{" "}
                    {known(event.after_tokens)} token
                  </small>
                )}
              </div>
            </li>
          ))}
        </ol>
      ) : (
        <p className="fine-print">尚无时间线事件。</p>
      )}
    </section>
  );
}
