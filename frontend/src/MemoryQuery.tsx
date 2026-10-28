import { useEffect, useRef, useState } from "react";
import { post, request } from "./api";
import { Modal, Spinner } from "./ui";
import { LatestRequest } from "./state";
import type { Fact, RuntimeManagement } from "./types";
type QueryMemory = {
  facts: Fact[];
  paths?: {
    entity: string;
    observed_path: {
      from: string;
      to: string;
      relation: string;
      inverse: boolean;
      fact_id: string;
      evidence: Fact["evidence"];
    }[];
  }[];
  aggregation?: { available: boolean; value: unknown; reason?: string };
  truncated?: boolean;
  scan_truncated?: boolean;
  query_truncated?: boolean;
  input_truncated_fact_ids?: string[];
  retrieval?: string;
  no_data?: boolean;
};
export default function MemoryQuery({
  selected,
  scope,
  report,
}: {
  selected: string | null;
  scope: string;
  report: (error: unknown) => void;
}) {
  const [mode, setMode] = useState("direct_relation"),
    [entity, setEntity] = useState(""),
    [relation, setRelation] = useState(""),
    [path, setPath] = useState(""),
    [type, setType] = useState(""),
    [aggregate, setAggregate] = useState("count"),
    [query, setQuery] = useState(""),
    [at, setAt] = useState(""),
    [result, setResult] = useState<QueryMemory | null>(null),
    [busy, setBusy] = useState(false),
    [authorize, setAuthorize] = useState(false);
  const gate = useRef(new LatestRequest()),
    context = useRef({ selected, scope });
  context.current = { selected, scope };
  useEffect(() => {
    gate.current.invalidate();
    setResult(null);
    setAuthorize(false);
    setBusy(false);
  }, [selected, scope, mode]);
  const search = async (approved = false) => {
    if (!selected) return;
    const generation = gate.current.begin(),
      sid = selected,
      currentScope = scope;
    const valid = () =>
      gate.current.current(generation) &&
      context.current.selected === sid &&
      context.current.scope === currentScope;
    setBusy(true);
    setResult(null);
    setAuthorize(false);
    try {
      if (mode === "semantic" && !approved) {
        const management = await request<RuntimeManagement>("/management");
        if (!valid()) return;
        if (management.extensions.embedding.backend !== "local") {
          setAuthorize(true);
          return;
        }
      }
      const arguments_ = {
        scopes: [scope],
        ...(at ? { at: new Date(at).toISOString() } : {}),
        ...(mode === "semantic"
          ? { query }
          : {
              mode,
              ...(entity ? { entity } : {}),
              ...(relation ? { relation } : {}),
              ...(mode === "path_query"
                ? {
                    path: path
                      .split(/[,\n]/)
                      .map((item) => item.trim())
                      .filter(Boolean),
                  }
                : {}),
              ...(mode === "typed_neighborhood"
                ? { candidate_type: type }
                : {}),
              ...(mode === "aggregate" ? { aggregate } : {}),
            }),
      };
      const value = await post<{
        is_error: boolean;
        content: string;
        data: { memory: QueryMemory };
      }>(
        `/customization/sessions/${encodeURIComponent(sid)}/${mode === "semantic" ? "memory_semantic_recall" : "memory_query"}`,
        { arguments: arguments_, approved },
      );
      if (value.is_error) throw new Error(value.content);
      if (valid()) setResult(value.data.memory);
    } catch (error) {
      if (valid()) report(error);
    } finally {
      if (valid()) setBusy(false);
    }
  };
  return (
    <details className="memory-query">
      <summary>关系与语义查询</summary>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void search();
        }}
      >
        <div className="form-row">
          <label className="field">
            记忆查询模式
            <select
              aria-label="记忆查询模式"
              value={mode}
              onChange={(e) => setMode(e.target.value)}
            >
              <option value="direct_relation">直接关系</option>
              <option value="path_query">关系路径</option>
              <option value="typed_neighborhood">指定类型的相邻实体</option>
              <option value="aggregate">关系聚合</option>
              <option value="semantic">语义查询</option>
            </select>
          </label>
          <label className="field">
            有效时间（留空为现在）
            <input
              type="datetime-local"
              value={at}
              onChange={(e) => setAt(e.target.value)}
            />
          </label>
        </div>
        {mode === "semantic" ? (
          <>
            <label className="field">
              语义查询文本
              <input value={query} onChange={(e) => setQuery(e.target.value)} />
            </label>
            <p className="fine-print">
              使用设置中的 embedding。选择本地模型时在本机计算；选择远程 API
              时会先请求授权，再发送当前作用域的记忆文本。
            </p>
          </>
        ) : (
          <>
            <div className="form-row">
              <label className="field">
                起始实体
                <input
                  value={entity}
                  onChange={(e) => setEntity(e.target.value)}
                />
              </label>
              {mode !== "path_query" && (
                <label className="field">
                  关系
                  <input
                    value={relation}
                    onChange={(e) => setRelation(e.target.value)}
                  />
                </label>
              )}
            </div>
            {mode === "path_query" && (
              <label className="field">
                关系路径（每行一跳）
                <textarea
                  aria-label="关系路径（每行一跳）"
                  rows={3}
                  placeholder={"负责人\n所在部门"}
                  value={path}
                  onChange={(e) => setPath(e.target.value)}
                />
                <small>逆向关系在名称后加 ^-1。</small>
              </label>
            )}
            {mode === "typed_neighborhood" && (
              <label className="field">
                目标实体类型
                <input
                  aria-label="目标实体类型"
                  value={type}
                  onChange={(e) => setType(e.target.value)}
                />
                <small>只匹配来源中明确记录的类型。</small>
              </label>
            )}
            {mode === "aggregate" && (
              <label className="field">
                聚合方式
                <select
                  aria-label="聚合方式"
                  value={aggregate}
                  onChange={(e) => setAggregate(e.target.value)}
                >
                  <option value="count">数量</option>
                  <option value="sum">数值总和</option>
                </select>
              </label>
            )}
          </>
        )}
        <button
          className="button secondary"
          disabled={
            busy ||
            !selected ||
            (mode === "semantic"
              ? !query.trim()
              : mode === "path_query"
                ? !entity.trim() || !path.trim()
                : mode === "typed_neighborhood"
                  ? !entity.trim() || !relation.trim() || !type.trim()
                  : false)
          }
        >
          {busy ? <Spinner /> : null}查询记忆
        </button>
      </form>
      {result && (
        <div className="memory-query-results" role="status">
          <p>
            {result.retrieval === "local_embedding_cosine"
              ? "本地语义检索"
              : result.retrieval === "configured_embedding_cosine"
                ? "远程语义检索"
                : result.retrieval || "本地关系查询"}{" "}
            · {result.facts.length} 条依据
            {result.no_data ? " · 未找到匹配记录" : ""}
          </p>
          {(result.truncated || result.scan_truncated) && (
            <p className="fine-print">
              {result.query_truncated || result.input_truncated_fact_ids?.length
                ? "部分查询或记忆文本超出长度上限，已截断后检索。"
                : "结果达到查询预算，当前展示为部分记录。"}
            </p>
          )}
          {result.aggregation && (
            <p>
              聚合结果：
              {result.aggregation.available
                ? String(result.aggregation.value)
                : result.aggregation.reason || "不可计算"}
            </p>
          )}
          {result.paths?.map((item, index) => (
            <article key={index}>
              <strong>{item.entity}</strong>
              {item.observed_path.map((hop, i) => (
                <details key={i}>
                  <summary>
                    {hop.from} → {hop.to} · {hop.relation}
                    {hop.inverse ? "（逆向）" : ""}
                  </summary>
                  <small>依据 {hop.fact_id}</small>
                  {hop.evidence.map((source, n) => (
                    <blockquote key={n}>{source.quote}</blockquote>
                  ))}
                </details>
              ))}
            </article>
          ))}
          {result.facts.map((fact) => (
            <article key={fact.id}>
              <p>
                {fact.entity} · {fact.relation} ·{" "}
                {typeof fact.value === "string"
                  ? fact.value
                  : JSON.stringify(fact.value)}
              </p>
              <details>
                <summary>查询依据</summary>
                {fact.evidence.map((source, index) => (
                  <blockquote key={index}>{source.quote}</blockquote>
                ))}
              </details>
            </article>
          ))}
        </div>
      )}
      {authorize && (
        <Modal title="授权语义记忆查询" onClose={() => setAuthorize(false)}>
          <p className="modal-description">
            将查询文本及“
            {scope === "project"
              ? "当前项目"
              : scope === "global"
                ? "全局共享"
                : "当前 Agent"}
            ”内符合时间条件的记忆发送到你配置的 embedding
            提供商。此操作可能产生供应商费用。
          </p>
          <div className="modal-actions">
            <button
              className="button secondary"
              onClick={() => setAuthorize(false)}
            >
              取消
            </button>
            <button
              className="button primary"
              onClick={() => void search(true)}
            >
              授权并查询
            </button>
          </div>
        </Modal>
      )}
    </details>
  );
}
