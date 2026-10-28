import { useEffect, useRef, useState } from "react";
import { post, request } from "./api";
import { Icon, Star } from "./icons";
import { dateLabel, Empty, Modal, Spinner } from "./ui";
import type { Fact, Job, Skill, ModelProvider, RunSelection } from "./types";
import { LatestRequest } from "./state";
import SelectionFields from "./SelectionFields";
import MemoryQuery from "./MemoryQuery";

type Props = {
  view: string;
  selected: string | null;
  ensureSession: () => Promise<string>;
  report: (error: unknown) => void;
  providers: ModelProvider[];
  selection: RunSelection;
};
type ToolResponse = {
  is_error: boolean;
  content: string;
  data: Record<string, any>;
};
function checked<T extends ToolResponse>(response: T) {
  if (response.is_error) throw new Error(response.content);
  return response;
}
const enc = encodeURIComponent;
export default function Management(props: Props) {
  return (
    <div className="management-scroll">
      <div className="management-page">
        {props.view === "skills" ? (
          <SkillsPanel {...props} />
        ) : props.view === "memory" ? (
          <MemoryPanel {...props} />
        ) : (
          <AutomationPanel {...props} />
        )}
      </div>
    </div>
  );
}
function Heading({
  kicker,
  title,
  description,
  action,
}: {
  kicker: string;
  title: string;
  description: string;
  action?: React.ReactNode;
}) {
  return (
    <div className="management-heading">
      <span className="eyebrow">{kicker}</span>
      <div>
        <h1>{title}</h1>
        {action}
      </div>
      <p>{description}</p>
    </div>
  );
}

function SkillsPanel({ ensureSession, report }: Props) {
  const [catalog, setCatalog] = useState<Skill[]>([]),
    [installed, setInstalled] = useState<Skill[]>([]),
    [loading, setLoading] = useState(true),
    [busy, setBusy] = useState(false),
    [filter, setFilter] = useState("");
  const [plan, setPlan] = useState<any>(null),
    [remove, setRemove] = useState<Skill | null>(null),
    [source, setSource] = useState(""),
    [local, setLocal] = useState(false);
  const refresh = async () => {
    const data = await request<{ catalog: Skill[]; installed: Skill[] }>(
      "/customization/skills",
    );
    setCatalog(data.catalog);
    setInstalled(data.installed);
  };
  useEffect(() => {
    void refresh()
      .catch(report)
      .finally(() => setLoading(false));
  }, []);
  const preview = async (source: string) => {
    setBusy(true);
    try {
      const sid = await ensureSession();
      const result = checked(
        await post<ToolResponse>(
          `/customization/sessions/${enc(sid)}/skill_package_preview`,
          { arguments: { source } },
        ),
      );
      setPlan({ ...result.data.package, session: sid });
      setLocal(false);
    } catch (e) {
      report(e);
    } finally {
      setBusy(false);
    }
  };
  const install = async () => {
    setBusy(true);
    try {
      checked(
        await post<ToolResponse>(
          `/customization/sessions/${enc(plan.session)}/skill_package_install`,
          {
            arguments: { plan_id: plan.plan_id, expected_digest: plan.digest },
            approved: true,
          },
        ),
      );
      setPlan(null);
      await refresh();
    } catch (e) {
      report(e);
    } finally {
      setBusy(false);
    }
  };
  const uninstall = async () => {
    if (!remove) return;
    setBusy(true);
    try {
      const sid = await ensureSession();
      checked(
        await post<ToolResponse>(
          `/customization/sessions/${enc(sid)}/skill_package_uninstall`,
          {
            arguments: { name: remove.name, expected_digest: remove.digest },
            approved: true,
          },
        ),
      );
      setRemove(null);
      await refresh();
    } catch (e) {
      report(e);
    } finally {
      setBusy(false);
    }
  };
  const descriptions: Record<string, string> = {
    "webapp-testing": "用真实浏览器验证页面、交互与下载结果。",
    "doc-coauthoring": "从资料收集、分段起草，到来源与读者检查。",
    "frontend-design": "创建有清晰层次、可访问且响应式的界面。",
    "theme-factory": "十套本地主题，让配色和排版保持一致。",
    "web-artifacts-builder": "制作可独立使用的 HTML 交互成果。",
    "pdf-reader": "结合文字提取和页图，带来源地阅读 PDF。",
    "mcp-builder": "构建有明确边界、可验证的 Python MCP 集成。",
    "skill-creator": "将工作方法整理为可复用、可检验的 Skill。",
  };
  return (
    <>
      <Heading
        kicker="可复用的工作方式"
        title="让 Aster 多一项拿手好戏。"
        description="Skills 将方法、模板和工具使用说明放在一起。安装前，你可以检查将要发生的变更。"
        action={
          <button className="button secondary" onClick={() => setLocal(true)}>
            <Icon name="plus" size={15} />
            本地导入
          </button>
        }
      />
      <div className="management-toolbar">
        <div className="search-field">
          <Icon name="search" size={17} />
          <input
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            placeholder="寻找一个 Skill"
            aria-label="搜索 Skills"
          />
        </div>
        <span>{installed.length} 个已安装</span>
      </div>
      {loading ? (
        <div className="loading-history">
          <Spinner />
          正在读取本地目录
        </div>
      ) : (
        <div className="skill-grid">
          {catalog
            .filter((s) =>
              (s.name + " " + s.description + " " + descriptions[s.name])
                .toLowerCase()
                .includes(filter.toLowerCase()),
            )
            .map((skill) => {
              const present = installed.find((s) => s.name === skill.name);
              return (
                <article className="skill-card" key={skill.name}>
                  <div className="skill-card-top">
                    <span className="skill-symbol">
                      <Star size={27} />
                    </span>
                    {present ? (
                      <span className="installed-tag">
                        <Icon name="check" size={12} />
                        {present.modified ? "有本地修改" : "已安装"}
                      </span>
                    ) : (
                      <span className="skill-license">{skill.license}</span>
                    )}
                  </div>
                  <h3>{skill.name}</h3>
                  <p>{descriptions[skill.name] || skill.description}</p>
                  <div className="skill-card-bottom">
                    <span>v{skill.version}</span>
                    {present ? (
                      <div>
                        <button
                          disabled={busy}
                          onClick={() => void preview("bundled:" + skill.name)}
                        >
                          检查变更
                        </button>
                        <button
                          aria-label={`卸载 ${skill.name}`}
                          disabled={busy}
                          onClick={() => setRemove(present)}
                        >
                          <Icon name="trash" size={14} />
                        </button>
                      </div>
                    ) : (
                      <button
                        disabled={busy}
                        onClick={() => void preview("bundled:" + skill.name)}
                      >
                        查看并安装
                        <Icon name="chevron" size={13} />
                      </button>
                    )}
                  </div>
                </article>
              );
            })}
        </div>
      )}
      {installed
        .filter((s) => !catalog.some((c) => c.name === s.name))
        .map((skill) => (
          <div className="custom-skill" key={skill.name}>
            <Icon name="grid" />
            <strong>{skill.name}</strong>
            <span>本地自定义 Skill</span>
            <button className="text-button" onClick={() => setRemove(skill)}>
              卸载
            </button>
          </div>
        ))}
      <p className="management-note">
        <Icon name="shield" size={15} />
        Skill 不会绕过工具授权。安装的内容在下一次运行中加载。
      </p>
      {plan && (
        <Modal title="检查安装内容" onClose={() => setPlan(null)}>
          <div className="package-preview">
            <Star size={33} />
            <div>
              <h3>{plan.name}</h3>
              <span>
                {plan.action === "upgrade"
                  ? "升级已有 Skill"
                  : "安装到本地 Skills 目录"}
              </span>
            </div>
          </div>
          <div className="change-list">
            {[
              ["新增", plan.added],
              ["修改", plan.changed],
              ["删除", plan.removed],
            ].map(([label, files]) => (
              <div key={String(label)}>
                <strong>
                  {String(label)} · {((files as string[]) || []).length}
                </strong>
                <ul>
                  {((files as string[]) || []).slice(0, 20).map((file) => (
                    <li key={file}>{file}</li>
                  ))}
                </ul>
                {((files as string[]) || []).length > 20 && (
                  <small>另有 {(files as string[]).length - 20} 项</small>
                )}
              </div>
            ))}
          </div>
          <label className="field">
            内容指纹<code className="digest">{plan.digest}</code>
          </label>
          <p className="fine-print">
            安装时会再次验证来源与指纹。这是对本次具体变更的明确确认，不授权执行包内脚本。
          </p>
          <div className="modal-actions">
            <button className="button secondary" onClick={() => setPlan(null)}>
              取消
            </button>
            <button
              className="button primary"
              disabled={busy}
              onClick={() => void install()}
            >
              {busy ? <Spinner /> : <Icon name="check" size={15} />}确认安装
            </button>
          </div>
        </Modal>
      )}
      {local && (
        <Modal title="导入本地 Skill" onClose={() => setLocal(false)}>
          <p className="modal-description">
            指定当前会话目录内的 Skill 文件夹或 UTF-8 ZIP
            包。会先检查内容，不会执行包内代码。
          </p>
          <label className="field">
            本地路径
            <input
              value={source}
              onChange={(e) => setSource(e.target.value)}
              placeholder="skills/my-workflow"
            />
          </label>
          <div className="modal-actions">
            <button
              className="button primary"
              disabled={busy || !source.trim()}
              onClick={() => void preview(source)}
            >
              {busy ? <Spinner /> : "检查内容"}
            </button>
          </div>
        </Modal>
      )}
      {remove && (
        <Modal title="卸载 Skill" onClose={() => setRemove(null)}>
          <p className="modal-description">
            将移除 <strong>{remove.name}</strong>{" "}
            的已安装文件。安装与卸载收据会保留，已有会话记录不受影响。
          </p>
          <div className="modal-actions">
            <button
              className="button secondary"
              onClick={() => setRemove(null)}
            >
              取消
            </button>
            <button
              className="button danger"
              disabled={busy}
              onClick={() => void uninstall()}
            >
              确认卸载
            </button>
          </div>
        </Modal>
      )}
    </>
  );
}

function MemoryPanel({ selected, ensureSession, report }: Props) {
  const requestGate = useRef(new LatestRequest());
  const [facts, setFacts] = useState<Fact[]>([]),
    [query, setQuery] = useState(""),
    [scope, setScope] = useState("agent"),
    [loading, setLoading] = useState(false),
    [busy, setBusy] = useState(false),
    [conflicts, setConflicts] = useState<any[]>([]);
  const [edit, setEdit] = useState<Fact | "new" | null>(null),
    [entity, setEntity] = useState(""),
    [relation, setRelation] = useState(""),
    [value, setValue] = useState(""),
    [evidence, setEvidence] = useState(""),
    [reason, setReason] = useState(""),
    [remove, setRemove] = useState<Fact | null>(null),
    [history, setHistory] = useState<any[] | null>(null);
  const [entityType, setEntityType] = useState(""),
    [targetType, setTargetType] = useState("");
  const currentView = useRef({ selected, scope });
  currentView.current = { selected, scope };
  const load = async (session = selected) => {
    if (
      currentView.current.scope !== scope ||
      (currentView.current.selected !== selected &&
        currentView.current.selected !== session)
    )
      return;
    const generation = requestGate.current.begin();
    const valid = () =>
      requestGate.current.current(generation) &&
      currentView.current.selected === session &&
      currentView.current.scope === scope;
    if (!session) {
      setFacts([]);
      return;
    }
    setLoading(true);
    try {
      const result = await request<{ facts: Fact[]; conflicts: any[] }>(
        `/customization/sessions/${enc(session)}/memory?scope=${scope}&query=${enc(query)}`,
      );
      if (!valid()) return;
      setFacts(result.facts);
      setConflicts(result.conflicts);
    } catch (e) {
      if (valid()) report(e);
    } finally {
      if (valid()) setLoading(false);
    }
  };
  useEffect(() => {
    setFacts([]);
    setConflicts([]);
    void load();
    return () => requestGate.current.invalidate();
  }, [selected, scope]);
  const startEdit = (fact: Fact | "new") => {
    setEdit(fact);
    setEntity(fact === "new" ? "" : fact.entity);
    setRelation(fact === "new" ? "" : fact.relation);
    setValue(
      fact === "new"
        ? ""
        : typeof fact.value === "string"
          ? fact.value
          : JSON.stringify(fact.value),
    );
    setEvidence("");
    setReason("");
    setEntityType(fact === "new" ? "" : fact.entity_type || "");
    setTargetType(fact === "new" ? "" : fact.target_type || "");
  };
  const save = async () => {
    setBusy(true);
    try {
      const sid = await ensureSession();
      const sources = [
        {
          kind: "user_supplied",
          label: "工作台操作员明确提供",
          quote: evidence,
        },
      ];
      const arguments_ =
        edit === "new"
          ? {
              entity,
              relation,
              value,
              sources,
              scope,
              ...(entityType ? { entity_type: entityType } : {}),
              ...(targetType ? { target_type: targetType } : {}),
            }
          : {
              fact_id: (edit as Fact).id,
              expected_version: (edit as Fact).version,
              value,
              sources,
              scope: (edit as Fact).scope,
              reason,
              ...(entityType ? { entity_type: entityType } : {}),
              ...(targetType ? { target_type: targetType } : {}),
            };
      checked(
        await post<ToolResponse>(
          `/customization/sessions/${enc(sid)}/${edit === "new" ? "memory_record" : "memory_update"}`,
          { arguments: arguments_, approved: true },
        ),
      );
      setEdit(null);
      await load(sid);
    } catch (e) {
      report(e);
    } finally {
      setBusy(false);
    }
  };
  const forget = async () => {
    if (!remove || !selected) return;
    setBusy(true);
    try {
      checked(
        await post<ToolResponse>(
          `/customization/sessions/${enc(selected)}/memory_forget`,
          {
            arguments: {
              fact_id: remove.id,
              expected_version: remove.version,
              scope: remove.scope,
              reason,
            },
            approved: true,
          },
        ),
      );
      setRemove(null);
      await load();
    } catch (e) {
      report(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <>
      <Heading
        kicker="有依据，才能记得更好"
        title="值得保留的事情。"
        description="明确记录事实、偏好与关系。每次修订保留来源；未验证的用户陈述不会被包装成已核实的证据。"
        action={
          <button className="button primary" onClick={() => startEdit("new")}>
            <Icon name="plus" size={15} />
            记录事实
          </button>
        }
      />
      <div className="management-toolbar">
        <form
          className="search-field"
          onSubmit={(e) => {
            e.preventDefault();
            void load();
          }}
        >
          <Icon name="search" size={17} />
          <input
            aria-label="搜索记忆"
            placeholder="搜索实体、关系或内容"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          <button aria-label="执行搜索">
            <Icon name="arrow" size={14} />
          </button>
        </form>
        <select
          aria-label="记忆作用域"
          value={scope}
          onChange={(e) => setScope(e.target.value)}
        >
          <option value="agent">当前 Agent</option>
          <option value="project">当前项目</option>
          <option value="global">全局共享</option>
        </select>
      </div>
      {scope === "global" && (
        <div className="soft-notice">
          <Icon name="globe" size={16} />
          全局记忆可供所有 Agent 查询，请只记录适合共享的内容。
        </div>
      )}
      <MemoryQuery selected={selected} scope={scope} report={report} />
      {!selected ? (
        <Empty
          icon="memory"
          title="先选择一段对话"
          detail="记忆按会话的 Agent 和项目隔离。也可以直接记录事实，创建默认会话。"
        />
      ) : loading ? (
        <div className="loading-history">
          <Spinner />
          正在召回事实
        </div>
      ) : !facts.length ? (
        <Empty
          icon="memory"
          title="这里还没有记录"
          detail="只在你明确记录后保留事实，不会自动猜测或提取个人信息。"
        />
      ) : (
        <div className="memory-list">
          {facts.map((fact) => (
            <article className="memory-card" key={fact.id}>
              <div className="memory-title">
                <span className="entity-symbol">
                  <Icon name="memory" size={19} />
                </span>
                <h3>{fact.entity}</h3>
                <span className="small-tag">{fact.relation}</span>
                <small>v{fact.version}</small>
              </div>
              <p className="fact-value">
                {typeof fact.value === "string"
                  ? fact.value
                  : JSON.stringify(fact.value)}
              </p>
              {conflicts.some((c) => c.fact_ids?.includes(fact.id)) && (
                <div className="conflict-note">
                  <Icon name="info" size={14} />
                  存在不同值，请核对来源后决定。
                </div>
              )}
              <details className="evidence-details">
                <summary>
                  {fact.evidence.length} 项来源 ·{" "}
                  {fact.evidence.every((e) => e.verified)
                    ? "来源已校验"
                    : "含用户提供的未验证来源"}
                  <Icon name="down" size={12} />
                </summary>
                {fact.evidence.map((e, i) => (
                  <blockquote key={i}>
                    <p>{e.quote}</p>
                    <cite>
                      {e.label || e.kind} ·{" "}
                      {e.verified ? "已校验引用" : "尚未独立核实"}
                    </cite>
                  </blockquote>
                ))}
              </details>
              <footer>
                <span>
                  {new Date(fact.observed_at).toLocaleDateString("zh-CN")}
                </span>
                <div>
                  <button
                    onClick={() =>
                      void request<{ revisions: any[] }>(
                        `/customization/sessions/${enc(selected!)}/memory/${enc(fact.id)}/history?scope=${fact.scope}`,
                      )
                        .then((r) => setHistory(r.revisions))
                        .catch(report)
                    }
                  >
                    修订历史
                  </button>
                  <button onClick={() => startEdit(fact)}>更新</button>
                  <button
                    onClick={() => {
                      setRemove(fact);
                      setReason("");
                    }}
                  >
                    撤回
                  </button>
                </div>
              </footer>
            </article>
          ))}
        </div>
      )}
      {edit && (
        <Modal
          title={edit === "new" ? "记录一条事实" : "更新这条事实"}
          onClose={() => setEdit(null)}
        >
          <p className="modal-description">
            内容由你明确提供，将作为未独立核实的来源保存。更新会产生新版本，并保留旧记录。
          </p>
          <div className="form-row">
            <label className="field">
              实体
              <input
                disabled={edit !== "new"}
                value={entity}
                onChange={(e) => setEntity(e.target.value)}
                placeholder="例如 项目名称"
              />
            </label>
            <label className="field">
              关系
              <input
                disabled={edit !== "new"}
                value={relation}
                onChange={(e) => setRelation(e.target.value)}
                placeholder="例如 负责人"
              />
            </label>
          </div>
          <label className="field">
            内容
            <textarea
              value={value}
              onChange={(e) => setValue(e.target.value)}
              rows={3}
            />
          </label>
          <label className="field">
            来源陈述
            <textarea
              value={evidence}
              onChange={(e) => setEvidence(e.target.value)}
              placeholder="请写出支持这条事实的明确陈述"
              rows={2}
            />
          </label>
          <details>
            <summary>明确记录的实体类型（可选）</summary>
            <div className="form-row">
              <label className="field">
                起始实体类型
                <input
                  value={entityType}
                  onChange={(e) => setEntityType(e.target.value)}
                />
              </label>
              <label className="field">
                目标实体类型
                <input
                  value={targetType}
                  onChange={(e) => setTargetType(e.target.value)}
                />
              </label>
            </div>
            <p className="fine-print">
              类型词必须出现在本次来源陈述中，不会根据名称猜测类型。
            </p>
          </details>
          {edit !== "new" && (
            <label className="field">
              更新原因
              <input
                value={reason}
                onChange={(e) => setReason(e.target.value)}
              />
            </label>
          )}
          <div className="modal-actions">
            <button className="button secondary" onClick={() => setEdit(null)}>
              取消
            </button>
            <button
              className="button primary"
              onClick={() => void save()}
              disabled={
                busy ||
                !entity.trim() ||
                !relation.trim() ||
                !value.trim() ||
                !evidence.trim() ||
                (edit !== "new" && !reason.trim())
              }
            >
              {busy ? <Spinner /> : <Icon name="check" size={15} />}确认记录
            </button>
          </div>
        </Modal>
      )}
      {remove && (
        <Modal title="撤回这条事实" onClose={() => setRemove(null)}>
          <p className="modal-description">
            撤回后不再参与普通召回。事实、来源与审计记录仍会保留，这不是物理删除。
          </p>
          <label className="field">
            撤回原因
            <input
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              placeholder="例如 信息已不再适用"
            />
          </label>
          <div className="modal-actions">
            <button
              className="button secondary"
              onClick={() => setRemove(null)}
            >
              取消
            </button>
            <button
              className="button danger"
              disabled={busy || !reason.trim()}
              onClick={() => void forget()}
            >
              确认撤回
            </button>
          </div>
        </Modal>
      )}
      {history && (
        <Modal title="可追溯的修订历史" onClose={() => setHistory(null)}>
          <div className="revision-list">
            {history.map((revision, i) => (
              <article key={i}>
                <span className="small-tag">
                  v{revision.version} · {revision.action}
                </span>
                <p>{revision.reason}</p>
                <blockquote>
                  {typeof revision.snapshot.value === "string"
                    ? revision.snapshot.value
                    : JSON.stringify(revision.snapshot.value)}
                </blockquote>
                <small>{revision.created_at}</small>
              </article>
            ))}
          </div>
        </Modal>
      )}
    </>
  );
}

function AutomationPanel({
  ensureSession,
  report,
  providers,
  selection,
}: Props) {
  const [jobs, setJobs] = useState<Job[]>([]),
    [loading, setLoading] = useState(true),
    [busy, setBusy] = useState(false),
    [form, setForm] = useState<Job | "new" | null>(null),
    [remove, setRemove] = useState<Job | null>(null);
  const [kind, setKind] = useState("schedule"),
    [prompt, setPrompt] = useState(""),
    [cron, setCron] = useState("0 9 * * 1-5"),
    [timezone, setTimezone] = useState(
      Intl.DateTimeFormat().resolvedOptions().timeZone || "Asia/Shanghai",
    ),
    [path, setPath] = useState(""),
    [pattern, setPattern] = useState("*.pdf"),
    [interval, setIntervalValue] = useState(300),
    [limit, setLimit] = useState(3);
  const [selectionMode, setSelectionMode] = useState("fixed"),
    [fixedSelection, setFixedSelection] = useState<RunSelection>(selection),
    [skills, setSkills] = useState<Skill[]>([]),
    [triggers, setTriggers] = useState<
      | {
          id: string;
          state: string;
          scheduled_at: number;
          selection: RunSelection | null;
          selection_mode: string | null;
          selection_captured: boolean;
        }[]
      | null
    >(null);
  const load = async () => {
    const value = await request<{ jobs: Job[] }>("/automations");
    setJobs(value.jobs);
  };
  useEffect(() => {
    void request<{ installed: Skill[] }>("/customization/skills")
      .then((value) => setSkills(value.installed))
      .catch(report);
    void load()
      .catch(report)
      .finally(() => setLoading(false));
  }, []);
  const start = (job: Job | "new") => {
    setForm(job);
    setKind(job === "new" ? "schedule" : job.kind);
    setPrompt(job === "new" ? "" : job.prompt);
    const config = job === "new" ? {} : job.config;
    setCron(String(config.cron || "0 9 * * 1-5"));
    setTimezone(
      String(
        config.timezone ||
          Intl.DateTimeFormat().resolvedOptions().timeZone ||
          "Asia/Shanghai",
      ),
    );
    setPath(String(config.path || (config.target as any)?.path || ""));
    setPattern(String(config.pattern || "*.pdf"));
    setIntervalValue(Number(config.interval_seconds || 300));
    setLimit(Number(config.max_runs_per_day || 3));
    setSelectionMode(String(config.selection_mode || "fixed"));
    setFixedSelection((config.selection as RunSelection) || selection);
  };
  const action = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    try {
      await fn();
      await load();
    } catch (e) {
      report(e);
    } finally {
      setBusy(false);
    }
  };
  const save = async () =>
    action(async () => {
      const original = form && form !== "new" ? form.config : {};
      const config = {
        ...(form === "new"
          ? { session_mode: "fresh", overlap: "skip", misfire: "coalesce" }
          : original),
        timezone,
        selection_mode: selectionMode,
        ...(selectionMode === "fixed" ? { selection: fixedSelection } : {}),
        max_runs_per_day: limit,
        ...(kind === "schedule"
          ? { cron }
          : kind === "watcher"
            ? { path, pattern }
            : {
                interval_seconds: interval,
                target:
                  (original.target as any)?.kind === "run"
                    ? original.target
                    : { kind: "file", path },
              }),
      };
      if (form === "new") {
        const sid = await ensureSession();
        await post("/automations", {
          kind,
          session_id: sid,
          prompt,
          config,
          enabled: true,
        });
      } else
        await request(`/automations/${enc((form as Job).id)}`, {
          method: "PATCH",
          body: JSON.stringify({ prompt, config }),
        });
      setForm(null);
    });
  const labels: Record<string, string> = {
    schedule: "定时任务",
    watcher: "文件观察",
    heartbeat: "变化检查",
  };
  return (
    <>
      <Heading
        kicker="把重复的事，交给有边界的自动化"
        title="在合适的时候，继续工作。"
        description="按时间、文件变化或明确目标启动任务。后台任务同样遵守工具权限，不会自动批准写入。"
        action={
          <button className="button primary" onClick={() => start("new")}>
            <Icon name="plus" size={15} />
            创建自动化
          </button>
        }
      />
      {loading ? (
        <div className="loading-history">
          <Spinner />
          正在读取自动化
        </div>
      ) : !jobs.length ? (
        <div className="automation-empty">
          <div className="automation-orbit">
            <Icon name="clock" size={40} />
            <i />
            <b />
          </div>
          <h2>少一点重复，多一点专注。</h2>
          <p>
            定期检查项目资料，或在新文件到达时开始处理。
            <br />
            你决定触发条件、目标和每天的执行次数。
          </p>
          <button className="button secondary" onClick={() => start("new")}>
            <Icon name="plus" size={15} />
            创建第一个自动化
          </button>
        </div>
      ) : (
        <div className="job-list">
          {jobs.map((job) => (
            <article className="job-card" key={job.id}>
              <div className="job-icon">
                <Icon
                  name={
                    job.kind === "schedule"
                      ? "clock"
                      : job.kind === "watcher"
                        ? "folder"
                        : "refresh"
                  }
                  size={23}
                />
              </div>
              <div className="job-main">
                <div>
                  <span className="eyebrow">
                    {labels[job.kind] || job.kind}
                  </span>
                  <span className={`job-state ${job.enabled ? "enabled" : ""}`}>
                    <i />
                    {job.enabled ? "已启用" : "已暂停"}
                  </span>
                </div>
                <h3>{job.prompt}</h3>
                <p>
                  {job.kind === "schedule"
                    ? `${job.config.cron} · ${job.config.timezone}`
                    : job.kind === "watcher"
                      ? String(job.config.path)
                      : `每 ${job.config.interval_seconds} 秒检查`}
                </p>
                <small>
                  {job.enabled && job.next_fire
                    ? `下次触发 ${dateLabel(job.next_fire)}`
                    : job.last_fire
                      ? `上次触发 ${dateLabel(job.last_fire)}`
                      : "尚未触发"}{" "}
                  · 每日上限 {String(job.config.max_runs_per_day)} 次
                </small>
              </div>
              <div className="job-actions">
                <button
                  className="icon-button"
                  aria-label="查看触发记录"
                  onClick={() =>
                    void request<{ triggers: NonNullable<typeof triggers> }>(
                      `/automations/${enc(job.id)}/triggers`,
                    )
                      .then((value) => setTriggers(value.triggers))
                      .catch(report)
                  }
                >
                  <Icon name="clock" size={17} />
                </button>
                <button
                  className="icon-button"
                  title={job.enabled ? "暂停任务" : "启用任务"}
                  aria-label={job.enabled ? "暂停任务" : "启用任务"}
                  disabled={busy}
                  onClick={() =>
                    void action(() =>
                      request(`/automations/${enc(job.id)}`, {
                        method: "PATCH",
                        body: JSON.stringify({ enabled: !job.enabled }),
                      }),
                    )
                  }
                >
                  <Icon name={job.enabled ? "pause" : "play"} size={17} />
                </button>
                <button
                  className="icon-button"
                  aria-label="编辑任务"
                  onClick={() => start(job)}
                >
                  <Icon name="settings" size={17} />
                </button>
                <button
                  className="icon-button"
                  aria-label="删除任务"
                  onClick={() => setRemove(job)}
                >
                  <Icon name="trash" size={16} />
                </button>
              </div>
            </article>
          ))}
        </div>
      )}
      <div className="soft-notice">
        <Icon name="info" size={17} />
        <p>
          Runtime
          运行期间才会检查触发条件。文件观察应把输入与输出目录分开，避免任务处理自己生成的文件。
        </p>
      </div>
      {form && (
        <Modal
          title={form === "new" ? "创建自动化" : "编辑自动化"}
          onClose={() => setForm(null)}
        >
          <div className="segmented">
            {Object.entries(labels).map(([key, label]) => (
              <button
                key={key}
                disabled={form !== "new" && kind !== key}
                className={kind === key ? "active" : ""}
                onClick={() => setKind(key)}
              >
                {label}
              </button>
            ))}
          </div>
          <label className="field">
            希望 Aster 做什么
            <textarea
              rows={3}
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              placeholder="清楚说明任务、输入和需要保留的结果"
            />
          </label>
          {kind === "schedule" ? (
            <div className="form-row">
              <label className="field">
                Cron 表达式
                <input value={cron} onChange={(e) => setCron(e.target.value)} />
                <small>分 时 日 月 周，例如工作日 9:00</small>
              </label>
              <label className="field">
                时区
                <input
                  value={timezone}
                  onChange={(e) => setTimezone(e.target.value)}
                />
              </label>
            </div>
          ) : (
            <>
              <label className="field">
                {kind === "watcher"
                  ? "观察目录的绝对路径"
                  : form !== "new" &&
                      (form?.config.target as any)?.kind === "run"
                    ? "检查运行 ID（保持原目标）"
                    : "检查文件的绝对路径"}
                <input
                  value={
                    form !== "new" &&
                    (form?.config.target as any)?.kind === "run"
                      ? String((form?.config.target as any).run_id)
                      : path
                  }
                  disabled={
                    form !== "new" &&
                    (form?.config.target as any)?.kind === "run"
                  }
                  onChange={(e) => setPath(e.target.value)}
                  placeholder="D:\Projects\inbox"
                />
              </label>
              {kind === "watcher" ? (
                <label className="field">
                  匹配文件
                  <input
                    value={pattern}
                    onChange={(e) => setPattern(e.target.value)}
                    placeholder="*.pdf"
                  />
                </label>
              ) : (
                <label className="field">
                  检查间隔（秒，至少 30）
                  <input
                    type="number"
                    min={30}
                    value={interval}
                    onChange={(e) => setIntervalValue(Number(e.target.value))}
                  />
                </label>
              )}
            </>
          )}
          <label className="field">
            每天最多执行次数
            <input
              type="number"
              min={1}
              max={10000}
              value={limit}
              onChange={(e) => setLimit(Number(e.target.value))}
            />
          </label>
          <label className="field">
            自动化模型选择
            <select
              aria-label="自动化模型选择"
              value={selectionMode}
              onChange={(e) => setSelectionMode(e.target.value)}
            >
              <option value="fixed">固定模型与 Skill</option>
              <option value="agent_default">每次使用 Agent 默认</option>
            </select>
          </label>
          {selectionMode === "fixed" ? (
            <SelectionFields
              value={fixedSelection}
              onChange={setFixedSelection}
              providers={providers}
              skills={skills}
            />
          ) : (
            <p className="fine-print">
              每次触发使用来源 Agent 的默认模型和
              Skill；工作台之后的模型切换不影响它。
            </p>
          )}
          <p className="fine-print">
            {form === "new"
              ? "每次触发创建新会话；已有任务执行时跳过重叠触发。创建后立即启用，但不会绕过后台审批。"
              : `保留原有会话策略 ${form.config.session_mode}、重叠策略 ${form.config.overlap} 与漏跑策略 ${form.config.misfire}。此次修改不会改变启用状态。`}
          </p>
          <div className="modal-actions">
            <button className="button secondary" onClick={() => setForm(null)}>
              取消
            </button>
            <button
              className="button primary"
              disabled={
                busy ||
                !prompt.trim() ||
                !limit ||
                (kind === "schedule"
                  ? !cron.trim()
                  : !path.trim() &&
                    !(
                      form !== "new" &&
                      (form?.config.target as any)?.kind === "run"
                    ))
              }
              onClick={() => void save()}
            >
              {busy ? <Spinner /> : <Icon name="check" size={15} />}{" "}
              {form === "new" ? "创建并启用" : "保存修改"}
            </button>
          </div>
        </Modal>
      )}
      {remove && (
        <Modal title="删除自动化" onClose={() => setRemove(null)}>
          <p className="modal-description">
            删除后不再产生新的触发。已有执行和会话记录仍然保留。
          </p>
          <div className="modal-actions">
            <button
              className="button secondary"
              onClick={() => setRemove(null)}
            >
              取消
            </button>
            <button
              className="button danger"
              disabled={busy}
              onClick={() =>
                void action(async () => {
                  await request(`/automations/${enc(remove.id)}`, {
                    method: "DELETE",
                  });
                  setRemove(null);
                })
              }
            >
              确认删除
            </button>
          </div>
        </Modal>
      )}
      {triggers && (
        <Modal title="自动化触发记录" onClose={() => setTriggers(null)}>
          {triggers.length ? (
            triggers.map((trigger) => (
              <article className="settings-card" key={trigger.id}>
                <p>
                  {dateLabel(trigger.scheduled_at)} · {trigger.state}
                </p>
                <p>
                  {trigger.selection_captured
                    ? `${trigger.selection_mode} · ${trigger.selection?.model} · ${trigger.selection?.reasoning_effort || "默认推理"} · Skill ${trigger.selection?.selected_skill || "无"}`
                    : "此旧记录尚未捕获选择"}
                </p>
                <small>{trigger.selection?.provider_id}</small>
              </article>
            ))
          ) : (
            <p>尚无触发记录。</p>
          )}
        </Modal>
      )}
    </>
  );
}
