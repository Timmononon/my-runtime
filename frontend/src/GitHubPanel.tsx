import { useCallback, useEffect, useRef, useState } from "react";
import { post, readableError, request } from "./api";
import { Icon } from "./icons";
import { dateLabel, Empty, Markdown, Modal, Spinner } from "./ui";
import { LatestRequest } from "./state";
import type { TaskTicket } from "./taskTypes";
import { taskLabel } from "./taskTypes";
import type {
  GithubCheck,
  GithubDraft,
  GithubEvent,
  GithubPoll,
  GithubSource,
  GithubSourceDraft,
} from "./githubTypes";
import {
  draftActions,
  githubDeliveryLabel,
  githubDraftLabel,
  githubEventLabel,
  githubHealthLabel,
  newGithubSource,
  safeGithubUrl,
  sourceEditor,
  sourcePayload,
} from "./githubTypes";
import "./github.css";
import TaskTriageSettingsModal, {
  TaskTriageDecisionCard,
} from "./TaskTriageSettings";

const enc = encodeURIComponent;
type Props = {
  sourceWritable: boolean;
  ensureSession: () => Promise<string>;
  onOpenSession: (id: string) => void;
  onOpenTicket: (id: string) => void;
  initialSourceId?: string | null;
};
type DraftForm = {
  id?: string;
  revision?: number;
  kind: "comment" | "pull_request";
  number: string;
  body: string;
  title: string;
  head: string;
  base: string;
  ticket_id: string;
};
const newDraft = (): DraftForm => ({
  kind: "comment",
  number: "",
  body: "",
  title: "",
  head: "",
  base: "main",
  ticket_id: "",
});
type ToolResponse = {
  is_error?: boolean;
  outcome?: string;
  error_category?: string;
  content?: string;
  data?: Record<string, unknown>;
};

export default function GitHubPanel({
  sourceWritable,
  ensureSession,
  onOpenSession,
  onOpenTicket,
  initialSourceId = null,
}: Props) {
  const [sources, setSources] = useState<GithubSource[]>([]),
    [loading, setLoading] = useState(true),
    [error, setError] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(initialSourceId),
    [source, setSource] = useState<GithubSource | null>(null);
  const [events, setEvents] = useState<GithubEvent[]>([]),
    [drafts, setDrafts] = useState<GithubDraft[]>([]),
    [tickets, setTickets] = useState<TaskTicket[]>([]);
  const [detailLoading, setDetailLoading] = useState(false),
    [tab, setTab] = useState<"events" | "drafts" | "tasks">("events");
  const eventCursor = useRef(0);
  const [eventTrail, setEventTrail] = useState<number[]>([0]);
  const [busy, setBusy] = useState(false),
    [notice, setNotice] = useState("");
  const [triageSettingsOpen, setTriageSettingsOpen] = useState(false);
  const [editor, setEditor] = useState<"new" | GithubSource | null>(null),
    [form, setForm] = useState<GithubSourceDraft>(newGithubSource);
  const [token, setToken] = useState(""),
    [removeToken, setRemoveToken] = useState(false),
    [formError, setFormError] = useState("");
  const [draftForm, setDraftForm] = useState<DraftForm | null>(null),
    [draftError, setDraftError] = useState("");
  const [review, setReview] = useState<GithubDraft | null>(null),
    [reviewError, setReviewError] = useState("");
  const [bindingOpen, setBindingOpen] = useState(false),
    [bindingTicket, setBindingTicket] = useState(""),
    [bindingNumber, setBindingNumber] = useState("");
  const listGate = useRef(new LatestRequest()),
    detailGate = useRef(new LatestRequest()),
    mounted = useRef(true),
    operation = useRef(false);
  const selectedRef = useRef(selectedId),
    draftRequest = useRef<string | null>(null);
  selectedRef.current = selectedId;

  const loadList = useCallback(async (signal?: AbortSignal) => {
    const epoch = listGate.current.begin();
    try {
      const result = await request<{ sources: GithubSource[] }>(
        "/github/sources",
        { signal },
      );
      if (mounted.current && listGate.current.current(epoch)) {
        setSources(result.sources);
        setLoading(false);
      }
    } catch (failure) {
      if (
        !signal?.aborted &&
        mounted.current &&
        listGate.current.current(epoch)
      ) {
        setError(readableError(failure));
        setLoading(false);
      }
    }
  }, []);
  const loadDetail = useCallback(async (id: string, signal?: AbortSignal) => {
    const epoch = detailGate.current.begin();
    try {
      const [configuration, incoming, outgoing] = await Promise.all([
        request<GithubSource>(`/github/sources/${enc(id)}`, { signal }),
        request<{ events: GithubEvent[] }>(
          `/github/sources/${enc(id)}/events?after=${eventCursor.current}&limit=100`,
          { signal },
        ),
        request<{ drafts: GithubDraft[] }>(
          `/github/sources/${enc(id)}/drafts`,
          { signal },
        ),
      ]);
      if (
        !mounted.current ||
        !detailGate.current.current(epoch) ||
        selectedRef.current !== id
      )
        return;
      setSource(configuration);
      setEvents(incoming.events);
      setDrafts(outgoing.drafts);
      setDetailLoading(false);
      const tasks = await request<{ tickets: TaskTicket[] }>(
        `/tasks?source_session_id=${enc(configuration.source_session_id)}`,
        { signal },
      );
      if (
        mounted.current &&
        detailGate.current.current(epoch) &&
        selectedRef.current === id
      )
        setTickets(tasks.tickets);
    } catch (failure) {
      if (
        !signal?.aborted &&
        mounted.current &&
        detailGate.current.current(epoch)
      ) {
        setError(readableError(failure));
        setDetailLoading(false);
      }
    }
  }, []);
  useEffect(() => {
    mounted.current = true;
    const control = new AbortController();
    void loadList(control.signal);
    let reading = false;
    const timer = window.setInterval(async () => {
      if (reading || operation.current || document.hidden) return;
      reading = true;
      try {
        await loadList(control.signal);
      } finally {
        reading = false;
      }
    }, 3000);
    return () => {
      mounted.current = false;
      control.abort();
      window.clearInterval(timer);
      listGate.current.invalidate();
      detailGate.current.invalidate();
    };
  }, [loadList]);
  useEffect(() => {
    setNotice("");
    setError("");
    setSource(null);
    setEvents([]);
    setDrafts([]);
    setTickets([]);
    if (!selectedId) return;
    const control = new AbortController();
    setDetailLoading(true);
    void loadDetail(selectedId, control.signal);
    let reading = false;
    const timer = window.setInterval(async () => {
      if (reading || operation.current || document.hidden) return;
      reading = true;
      try {
        await loadDetail(selectedId, control.signal);
      } finally {
        reading = false;
      }
    }, 3000);
    return () => {
      control.abort();
      window.clearInterval(timer);
      detailGate.current.invalidate();
    };
  }, [selectedId, loadDetail]);
  const refresh = async () => {
    await loadList();
    if (selectedRef.current) await loadDetail(selectedRef.current);
  };
  const selectSource = (id: string) => {
    eventCursor.current = 0;
    setEventTrail([0]);
    selectedRef.current = id;
    setSelectedId(id);
    setTab("events");
  };
  const eventPage = (trail: number[]) => {
    eventCursor.current = trail[trail.length - 1];
    setEventTrail(trail);
    if (selectedRef.current) void loadDetail(selectedRef.current);
  };
  const act = async (
    work: () => Promise<void>,
    fail: (text: string) => void = setError,
  ) => {
    if (operation.current) return;
    operation.current = true;
    setBusy(true);
    setError("");
    setNotice("");
    listGate.current.invalidate();
    detailGate.current.invalidate();
    try {
      await work();
    } catch (failure) {
      if (mounted.current) fail(readableError(failure));
    } finally {
      if (mounted.current) await refresh();
      operation.current = false;
      if (mounted.current) setBusy(false);
    }
  };
  const openEditor = (value: "new" | GithubSource) => {
    setEditor(value);
    setForm(value === "new" ? newGithubSource() : sourceEditor(value));
    setToken("");
    setRemoveToken(false);
    setFormError("");
  };
  const saveSource = () =>
    act(async () => {
      if (!editor) return;
      const values = sourcePayload(form);
      if (removeToken && token)
        throw new Error("移除令牌与保存新令牌请分别操作。");
      const secret = token.trim();
      const credentials = {
        ...(secret ? { token: secret } : {}),
        ...(removeToken ? { remove_token: true } : {}),
      };
      const saved =
        editor === "new"
          ? await post<GithubSource>("/github/sources", {
              source_session_id: await ensureSession(),
              ...values,
              ...credentials,
            })
          : await request<GithubSource>(`/github/sources/${enc(editor.id)}`, {
              method: "PATCH",
              body: JSON.stringify({
                expected_revision: editor.revision,
                ...values,
                ...credentials,
              }),
            });
      if (!mounted.current) return;
      setToken("");
      setEditor(null);
      selectSource(saved.id);
    }, setFormError);
  const checkSource = () => {
    if (!source) return;
    const current = source;
    void act(async () => {
      const result = await post<GithubCheck>(
        `/github/sources/${enc(current.id)}/check`,
      );
      if (
        result.status !== "ok" ||
        result.repository.full_name.toLowerCase() !==
          current.repository.toLowerCase()
      )
        throw new Error("GitHub 未返回已确认的仓库信息。");
      setNotice(
        `已连接 ${result.repository.full_name}；${result.account?.login ? `当前账号 ${result.account.login}` : "匿名读取公有仓库"}。`,
      );
    });
  };
  const pollSource = (readOnly: boolean) => {
    if (!source) return;
    const current = source;
    void act(async () => {
      const result = await post<GithubPoll>(
        `/github/sources/${enc(current.id)}/poll`,
        readOnly ? { read_only: true } : {},
      );
      if (
        result.source.id !== current.id ||
        (readOnly && result.read_only !== true)
      )
        throw new Error("读取回执未确认本次只读范围，请检查来源状态。");
      const counts =
        typeof result.requests === "number" && typeof result.events === "number"
          ? `本次${result.requests}次只读请求，记录${result.events}条事件`
          : "本次读取已完成";
      setNotice(
        `${counts}${result.read_only ? "；没有启动模型任务" : `；接入${result.accepted}项任务`}。${result.baseline ? "首次读取已建立历史基线。" : ""}${result.pending_pages ? "仍有分页待继续读取。" : ""}`,
      );
    });
  };
  const openDraft = (value?: GithubDraft) => {
    setDraftError("");
    draftRequest.current = null;
    setDraftForm(
      value
        ? {
            id: value.id,
            revision: value.revision,
            kind: value.kind,
            number: value.number ? String(value.number) : "",
            body: value.body,
            title: value.title || "",
            head: value.head || "",
            base: value.base || "main",
            ticket_id: value.ticket_id || "",
          }
        : newDraft(),
    );
  };
  const changeDraft = (changes: Partial<DraftForm>) => {
    setDraftForm((current) => (current ? { ...current, ...changes } : current));
    draftRequest.current = null;
  };
  const saveDraft = () =>
    act(async () => {
      if (!source || !draftForm) return;
      if (!draftForm.body.trim()) throw new Error("请填写准备发送的正文。");
      if (
        draftForm.kind === "comment" &&
        (!/^[1-9][0-9]*$/.test(draftForm.number) ||
          !Number.isSafeInteger(Number(draftForm.number)))
      )
        throw new Error("请填写有效的 Issue 或 PR 编号。");
      if (
        draftForm.kind === "pull_request" &&
        (!draftForm.title.trim() ||
          !draftForm.head.trim() ||
          !draftForm.base.trim())
      )
        throw new Error("请填写 PR 标题、来源分支与目标分支。");
      const fields = {
        body: draftForm.body,
        ...(draftForm.kind === "pull_request"
          ? {
              title: draftForm.title.trim(),
              head: draftForm.head.trim(),
              base: draftForm.base.trim(),
            }
          : {}),
      };
      if (draftForm.id)
        await request<GithubDraft>(`/github/drafts/${enc(draftForm.id)}`, {
          method: "PATCH",
          body: JSON.stringify({
            expected_revision: draftForm.revision,
            ...fields,
          }),
        });
      else {
        draftRequest.current ||= crypto.randomUUID();
        await post<GithubDraft>(`/github/sources/${enc(source.id)}/drafts`, {
          ...fields,
          kind: draftForm.kind,
          request_id: draftRequest.current,
          ...(draftForm.kind === "comment"
            ? { number: Number(draftForm.number) }
            : {}),
          ...(draftForm.ticket_id ? { ticket_id: draftForm.ticket_id } : {}),
        });
      }
      setDraftForm(null);
      setTab("drafts");
      setNotice("草稿已保存，尚未发送到 GitHub。");
    }, setDraftError);
  const sendDraft = () => {
    if (!review) return;
    const current = review;
    void act(async () => {
      const result = await post<ToolResponse>(
        `/github/drafts/${enc(current.id)}/send`,
        { expected_revision: current.revision },
      );
      if (
        typeof result.is_error !== "boolean" ||
        typeof result.outcome !== "string"
      )
        throw new Error("发送回执无法读取，请关闭窗口并重新核验草稿状态。");
      if (result.outcome === "unknown" || result.outcome === "outcome_unknown")
        throw new Error(
          "发送结果尚未确定。请关闭此窗口并只读核验；不要再次发送。",
        );
      if (result.is_error)
        throw new Error(
          result.content || "GitHub 未确认发送，请重新读取草稿状态。",
        );
      setReview(null);
      setTab("drafts");
      setNotice("发送请求已处理。请以草稿的已发送状态和 GitHub 回执为准。");
    }, setReviewError);
  };
  const reconcile = (draft: GithubDraft) => {
    void act(async () => {
      const result = await post<{ verified: boolean; draft: GithubDraft }>(
        `/github/drafts/${enc(draft.id)}/reconcile`,
      );
      if (result.draft?.id !== draft.id || typeof result.verified !== "boolean")
        throw new Error("核验回执无法读取，请重新查看草稿状态。");
      setNotice(
        result.verified && result.draft.state === "sent"
          ? "只读核验找到了匹配回执，已更新发送状态。"
          : "尚未找到足够证据，继续保留待核验状态；没有重新发送。",
      );
    });
  };
  const bind = () =>
    act(async () => {
      if (!source || !bindingTicket || !/^[1-9][0-9]*$/.test(bindingNumber))
        throw new Error("请选择任务并填写有效的 Issue 或 PR 编号。");
      await post(`/github/sources/${enc(source.id)}/bindings`, {
        ticket_id: bindingTicket,
        number: Number(bindingNumber),
      });
      setBindingOpen(false);
      setNotice("已核验并关联仓库讨论，后续事件沿用任务授权边界。");
    });
  const linkedIds = new Set(
    [
      ...events.map((item) => item.ticket_id),
      ...drafts.map((item) => item.ticket_id),
    ].filter(Boolean),
  );
  const linkedTickets = tickets.filter(
    (item) => item.github?.source_id === source?.id || linkedIds.has(item.id),
  );
  const waitingDrafts = drafts.filter((item) =>
    ["draft", "rejected", "outcome_unknown"].includes(item.state),
  );
  const currentReview = review
    ? drafts.find((item) => item.id === review.id)
    : null;
  const canSendReview = !!(
    review &&
    currentReview &&
    source &&
    currentReview.revision === review.revision &&
    draftActions(currentReview, source).send
  );

  return (
    <div className="github-scroll">
      <div className="github-panel">
        <header className="github-heading">
          <div>
            <span className="eyebrow">仓库协作</span>
            <h1>GitHub</h1>
            <p>读取仓库进展、关联任务，并在审阅后回传结果。</p>
          </div>
          <div className="github-heading-actions">
            <button
              className="button secondary"
              onClick={() => setTriageSettingsOpen(true)}
            >
              任务判断模型
            </button>
            <button
              className="button primary"
              disabled={!sourceWritable}
              onClick={() => openEditor("new")}
            >
              <Icon name="plus" size={16} />
              添加仓库来源
            </button>
          </div>
        </header>
        {error && (
          <div className="github-error" role="alert">
            <span>{error}</span>
            <button
              className="text-button"
              onClick={() => {
                setError("");
                void refresh();
              }}
            >
              重新读取本地状态
            </button>
          </div>
        )}
        {notice && (
          <div className="github-notice" role="status">
            {notice}
          </div>
        )}
        {!sourceWritable && (
          <p className="github-note">
            当前对话不能作为新来源。可查看已有来源，或切换到普通可用对话后添加。
          </p>
        )}
        <div className="github-columns">
          <section className="github-sources" aria-label="GitHub 来源列表">
            <div className="github-list-heading">
              <span>{sources.length}个来源</span>
              <button
                className="icon-button"
                aria-label="刷新 GitHub 本地状态"
                disabled={busy}
                onClick={() => void refresh()}
              >
                <Icon name="refresh" size={16} />
              </button>
            </div>
            {loading ? (
              <div className="github-loading" role="status">
                <Spinner />
                正在读取来源
              </div>
            ) : !sources.length ? (
              <Empty
                icon="branch"
                title="从一个仓库开始"
                detail="新来源默认关闭后台轮询、只读且禁止发送。公有仓库可以匿名读取。"
              />
            ) : (
              sources.map((item) => (
                <button
                  className={`github-source-card ${selectedId === item.id ? "selected" : ""}`}
                  key={item.id}
                  aria-pressed={selectedId === item.id}
                  onClick={() => selectSource(item.id)}
                >
                  <h2>{item.repository}</h2>
                  <div>
                    <span className="github-tag">
                      {item.mode === "read_only" ? "只读" : "接入任务"}
                    </span>
                    <span className="github-tag">
                      {item.enabled ? "后台轮询开启" : "后台轮询关闭"}
                    </span>
                    <span
                      className={`github-tag ${item.allow_writes ? "write-enabled" : ""}`}
                    >
                      {item.allow_writes ? "允许审阅后发送" : "禁止发送"}
                    </span>
                  </div>
                  <p>{githubHealthLabel(item)}</p>
                  <small>
                    {item.configured
                      ? item.verified_login || "已保存令牌，尚未检查账号"
                      : "匿名读取 · 未保存令牌"}
                  </small>
                </button>
              ))
            )}
          </section>
          <section className="github-detail" aria-label="GitHub 来源详情">
            {!selectedId ? (
              <Empty
                icon="branch"
                title="选择一个仓库来源"
                detail="查看本地事件和草稿不会请求 GitHub。连接检查与抓取由你明确发起。"
              />
            ) : detailLoading ? (
              <div className="github-loading" role="status">
                <Spinner />
                正在读取本地记录
              </div>
            ) : (
              source && (
                <>
                  <div className="github-detail-heading">
                    <div>
                      <span className="eyebrow">
                        {source.mode === "read_only" ? "只读来源" : "任务来源"}
                      </span>
                      <h2>{source.repository}</h2>
                    </div>
                    <button
                      className="button secondary"
                      disabled={busy}
                      onClick={() => openEditor(source)}
                    >
                      <Icon name="settings" size={15} />
                      配置来源
                    </button>
                  </div>
                  <div className="github-source-status">
                    <span className={`github-health health-${source.health}`}>
                      <i />
                      {githubHealthLabel(source)}
                    </span>
                    <span>
                      {source.configured
                        ? `账号 ${source.verified_login || "尚未检查"}`
                        : "匿名读取 · 未保存令牌"}
                    </span>
                  </div>
                  {source.error && (
                    <p className="github-error" role="alert">
                      {source.error}
                    </p>
                  )}
                  <div className="github-source-facts">
                    <span>
                      后台轮询：
                      {source.enabled ? `每${source.poll_seconds}秒` : "关闭"}
                    </span>
                    <span>
                      最近抓取：
                      {source.last_poll_at
                        ? dateLabel(source.last_poll_at)
                        : "尚未抓取"}
                    </span>
                    {source.next_poll_at && source.enabled ? (
                      <span>下次抓取：{dateLabel(source.next_poll_at)}</span>
                    ) : null}
                  </div>
                  <div className="github-controls">
                    <button
                      className="button secondary"
                      disabled={busy}
                      onClick={checkSource}
                    >
                      <Icon name="globe" size={15} />
                      检查连接
                    </button>
                    <button
                      className="button secondary"
                      disabled={busy}
                      onClick={() => pollSource(true)}
                    >
                      <Icon name="download" size={15} />
                      只读抓取
                    </button>
                    {source.mode === "tasks" && (
                      <button
                        className="button secondary"
                        disabled={busy}
                        onClick={() => pollSource(false)}
                      >
                        抓取并按规则处理
                      </button>
                    )}
                    <button
                      className="text-button"
                      onClick={() => onOpenSession(source.source_session_id)}
                    >
                      来源对话
                      <Icon name="external" size={13} />
                    </button>
                  </div>
                  <p className="github-note">
                    只读抓取不会启动模型或向 GitHub
                    写入。首次抓取建立历史基线，后续新事件才按配置处理。
                  </p>
                  <nav className="github-tabs" aria-label="仓库来源内容">
                    {[
                      ["events", `事件 ${events.length}`],
                      ["drafts", `待审阅 ${waitingDrafts.length}`],
                      ["tasks", `关联任务 ${linkedTickets.length}`],
                    ].map(([value, label]) => (
                      <button
                        key={value}
                        aria-pressed={tab === value}
                        onClick={() => setTab(value as typeof tab)}
                      >
                        {label}
                      </button>
                    ))}
                  </nav>
                  {tab === "events" && (
                    <div className="github-events" aria-label="仓库事件">
                      <div className="github-event-pagination">
                        <span>
                          本批{events.length}条
                          {typeof source.event_count === "number"
                            ? ` · 本地共${source.event_count}条`
                            : ""}
                          ，按接收顺序显示
                        </span>
                        <div>
                          <button
                            className="text-button"
                            disabled={busy || eventTrail.length < 2}
                            onClick={() => eventPage(eventTrail.slice(0, -1))}
                          >
                            上一批
                          </button>
                          <button
                            className="text-button"
                            disabled={busy || events.length < 100}
                            onClick={() =>
                              eventPage([
                                ...eventTrail,
                                events[events.length - 1].seq,
                              ])
                            }
                          >
                            下一批
                          </button>
                        </div>
                      </div>
                      {!events.length ? (
                        <Empty
                          icon="clock"
                          title={
                            eventTrail.length > 1
                              ? "此批次没有更多事件"
                              : "尚无本地事件"
                          }
                          detail={
                            eventTrail.length > 1
                              ? "可以返回上一批查看已有记录。"
                              : "点击“只读抓取”从仓库建立历史基线。"
                          }
                        />
                      ) : (
                        events.map((item) => (
                          <article className="github-event" key={item.id}>
                            <div className="github-event-heading">
                              <span className="github-tag">
                                {githubEventLabel(item.kind)}
                                {item.number ? ` #${item.number}` : ""}
                              </span>
                              <span>
                                {item.baseline
                                  ? "历史基线"
                                  : item.ticket_id
                                    ? "已关联任务"
                                    : "已记录"}
                              </span>
                            </div>
                            <p className="github-event-byline">
                              {item.actor || "自动事件"} ·{" "}
                              {typeof item.at === "number"
                                ? dateLabel(item.at)
                                : item.at}
                            </p>
                            {item.body && (
                              <details>
                                <summary>查看事件内容</summary>
                                <blockquote>{item.body}</blockquote>
                              </details>
                            )}
                            {item.triage && (
                              <TaskTriageDecisionCard value={item.triage} />
                            )}
                            <div className="github-event-links">
                              {safeGithubUrl(item.url, source.repository) && (
                                <a
                                  href={safeGithubUrl(
                                    item.url,
                                    source.repository,
                                  )!}
                                  target="_blank"
                                  rel="noopener noreferrer"
                                >
                                  在 GitHub 查看
                                  <Icon name="external" size={13} />
                                </a>
                              )}
                              {item.ticket_id && (
                                <button
                                  className="text-button"
                                  onClick={() => onOpenTicket(item.ticket_id!)}
                                >
                                  打开任务
                                  <Icon name="chevron" size={13} />
                                </button>
                              )}
                            </div>
                          </article>
                        ))
                      )}
                    </div>
                  )}
                  {tab === "drafts" && (
                    <>
                      <div className="github-section-heading">
                        <p>草稿保存在本机；只有审阅确认后才会发送。</p>
                        <button
                          className="button secondary"
                          disabled={busy}
                          onClick={() => openDraft()}
                        >
                          <Icon name="plus" size={15} />
                          准备草稿
                        </button>
                      </div>
                      {!drafts.length ? (
                        <Empty
                          icon="file"
                          title="还没有待审阅的草稿"
                          detail="准备评论或 PR 文本，再决定是否发送。"
                        />
                      ) : (
                        drafts.map((draft) => (
                          <article
                            className={`github-draft draft-${draft.state}`}
                            key={draft.id}
                          >
                            <div className="github-draft-heading">
                              <div>
                                <span className="github-tag">
                                  {draft.kind === "comment"
                                    ? `评论 #${draft.number}`
                                    : "Pull request"}
                                </span>
                                <h3>
                                  {draft.title || `仓库讨论 #${draft.number}`}
                                </h3>
                              </div>
                              <span className="github-draft-state">
                                {githubDraftLabel(draft.state)}
                              </span>
                            </div>
                            {draft.kind === "pull_request" && (
                              <p className="github-draft-branches">
                                {draft.head} → {draft.base}
                              </p>
                            )}
                            <details
                              className="github-draft-content"
                              open={
                                draft.state === "outcome_unknown" || undefined
                              }
                            >
                              <summary>查看草稿正文</summary>
                              <Markdown>{draft.body}</Markdown>
                            </details>
                            {draft.error && (
                              <p className="github-draft-error">
                                {draft.error}
                              </p>
                            )}
                            {draft.state === "outcome_unknown" && (
                              <p className="github-note">
                                远端可能已经收到。只读核验不会重发；尚未找到回执时继续保留待核验状态。
                              </p>
                            )}
                            <div className="github-draft-actions">
                              {draftActions(draft, source).edit && (
                                <button
                                  className="button secondary"
                                  disabled={busy}
                                  onClick={() => openDraft(draft)}
                                >
                                  编辑草稿
                                </button>
                              )}
                              {draftActions(draft, source).send && (
                                <button
                                  className="button primary"
                                  disabled={busy}
                                  onClick={() => {
                                    setReview(draft);
                                    setReviewError("");
                                  }}
                                >
                                  审阅并发送
                                </button>
                              )}
                              {draftActions(draft, source).reconcile && (
                                <button
                                  className="button secondary"
                                  disabled={busy}
                                  onClick={() => reconcile(draft)}
                                >
                                  只读核验结果
                                </button>
                              )}
                              {safeGithubUrl(draft.url, source.repository) && (
                                <a
                                  className="button secondary"
                                  href={safeGithubUrl(
                                    draft.url,
                                    source.repository,
                                  )!}
                                  target="_blank"
                                  rel="noopener noreferrer"
                                >
                                  查看 GitHub 回执
                                  <Icon name="external" size={13} />
                                </a>
                              )}
                              {draft.ticket_id && (
                                <button
                                  className="text-button"
                                  onClick={() => onOpenTicket(draft.ticket_id!)}
                                >
                                  关联任务
                                </button>
                              )}
                            </div>
                            {draftActions(draft, source).edit &&
                              !draftActions(draft, source).send && (
                                <p className="github-fine-print">
                                  发送需要启用来源、任务模式、允许发送、已保存令牌和已检查账号；来源变更后需重新准备草稿。
                                </p>
                              )}
                          </article>
                        ))
                      )}
                    </>
                  )}
                  {tab === "tasks" && (
                    <>
                      <div className="github-section-heading">
                        <p>执行结果和 PR 交付状态分别跟踪。</p>
                        <button
                          className="button secondary"
                          disabled={busy || !tickets.length}
                          onClick={() => {
                            setBindingOpen(true);
                            setBindingTicket("");
                            setBindingNumber("");
                          }}
                        >
                          关联仓库讨论
                        </button>
                      </div>
                      {!linkedTickets.length ? (
                        <Empty
                          icon="grid"
                          title="还没有关联任务"
                          detail="任务模式下，获得授权的新请求可创建工单；也可核验并关联已有任务。"
                        />
                      ) : (
                        linkedTickets.map((item) => (
                          <article className="github-linked-task" key={item.id}>
                            <h3>{item.title}</h3>
                            <div>
                              <span className="github-tag">
                                执行：{taskLabel(item.state)}
                              </span>
                              <span className="github-tag">
                                {githubDeliveryLabel(
                                  item.github?.delivery_state,
                                )}
                              </span>
                            </div>
                            {item.attention && <p>{item.attention}</p>}
                            <button
                              className="text-button"
                              onClick={() => onOpenTicket(item.id)}
                            >
                              打开任务
                              <Icon name="chevron" size={14} />
                            </button>
                          </article>
                        ))
                      )}
                    </>
                  )}
                </>
              )
            )}
          </section>
        </div>
        {triageSettingsOpen && (
          <TaskTriageSettingsModal
            onClose={() => setTriageSettingsOpen(false)}
            onSaved={() =>
              setNotice("已保存独立任务判断模型，仅对后续新事件生效。")
            }
          />
        )}
        {editor && (
          <Modal
            title={editor === "new" ? "添加 GitHub 来源" : "配置 GitHub 来源"}
            wide
            onClose={() => {
              if (!busy) {
                setEditor(null);
                setToken("");
              }
            }}
          >
            <form
              onSubmit={(event) => {
                event.preventDefault();
                void saveSource();
              }}
            >
              <GithubSourceFields
                value={form}
                onChange={(changes) =>
                  setForm((current) => ({ ...current, ...changes }))
                }
                disabled={busy}
              />
              <div className="github-credential">
                <h3>访问令牌（可选）</h3>
                <p>
                  公有仓库可匿名读取。令牌仅在本次保存时写入本机凭据文件，保存后无法回读。
                </p>
                <label className="field">
                  <span>
                    {editor !== "new" && editor.configured
                      ? "替换令牌，留空保留现有凭据"
                      : "GitHub Token"}
                  </span>
                  <input
                    aria-label="GitHub Token"
                    type="password"
                    autoComplete="new-password"
                    value={token}
                    onChange={(event) => setToken(event.target.value)}
                    disabled={busy || removeToken}
                  />
                </label>
                {editor !== "new" && editor.configured && (
                  <label className="github-checkbox">
                    <input
                      type="checkbox"
                      checked={removeToken}
                      disabled={busy}
                      onChange={(event) => {
                        setRemoveToken(event.target.checked);
                        if (event.target.checked) setToken("");
                      }}
                    />
                    移除已保存的令牌，改为匿名读取
                  </label>
                )}
              </div>
              {formError && (
                <p className="inline-error" role="alert">
                  {formError}
                </p>
              )}
              <div className="modal-actions">
                <button
                  className="button secondary"
                  type="button"
                  disabled={busy}
                  onClick={() => {
                    setEditor(null);
                    setToken("");
                  }}
                >
                  取消
                </button>
                <button
                  className="button primary"
                  type="submit"
                  disabled={busy}
                >
                  {busy ? "正在保存…" : "保存来源"}
                </button>
              </div>
            </form>
          </Modal>
        )}
        {draftForm && source && (
          <Modal
            title={draftForm.id ? "编辑 GitHub 草稿" : "准备 GitHub 草稿"}
            wide
            onClose={() => {
              if (!busy) setDraftForm(null);
            }}
          >
            <form
              onSubmit={(event) => {
                event.preventDefault();
                void saveDraft();
              }}
            >
              <p className="modal-description">
                保存草稿只写入本机。发送前会单独展示目标和正文，请你确认。
              </p>
              <label className="field">
                <span>草稿类型</span>
                <select
                  aria-label="草稿类型"
                  value={draftForm.kind}
                  disabled={busy || !!draftForm.id}
                  onChange={(event) =>
                    changeDraft({
                      kind: event.target.value as DraftForm["kind"],
                    })
                  }
                >
                  <option value="comment">Issue／PR 评论</option>
                  <option value="pull_request">Pull request</option>
                </select>
              </label>
              {draftForm.kind === "comment" ? (
                <label className="field">
                  <span>Issue／PR 编号</span>
                  <input
                    inputMode="numeric"
                    value={draftForm.number}
                    onChange={(event) =>
                      changeDraft({ number: event.target.value })
                    }
                    required
                    disabled={busy || !!draftForm.id}
                  />
                </label>
              ) : (
                <>
                  <label className="field">
                    <span>PR 标题</span>
                    <input
                      value={draftForm.title}
                      onChange={(event) =>
                        changeDraft({ title: event.target.value })
                      }
                      required
                      disabled={busy}
                      maxLength={200}
                    />
                  </label>
                  <div className="github-form-two">
                    <label className="field">
                      <span>来源分支</span>
                      <input
                        value={draftForm.head}
                        onChange={(event) =>
                          changeDraft({ head: event.target.value })
                        }
                        required
                        disabled={busy}
                      />
                    </label>
                    <label className="field">
                      <span>目标分支</span>
                      <input
                        value={draftForm.base}
                        onChange={(event) =>
                          changeDraft({ base: event.target.value })
                        }
                        required
                        disabled={busy}
                      />
                    </label>
                  </div>
                </>
              )}
              {!draftForm.id && (
                <label className="field">
                  <span>关联任务（可选）</span>
                  <select
                    aria-label="关联任务（可选）"
                    value={draftForm.ticket_id}
                    onChange={(event) =>
                      changeDraft({ ticket_id: event.target.value })
                    }
                    disabled={busy}
                  >
                    <option value="">不关联任务</option>
                    {tickets.map((item) => (
                      <option key={item.id} value={item.id}>
                        {item.title}
                      </option>
                    ))}
                  </select>
                </label>
              )}
              <label className="field">
                <span>草稿正文</span>
                <textarea
                  rows={8}
                  value={draftForm.body}
                  onChange={(event) =>
                    changeDraft({ body: event.target.value })
                  }
                  required
                  disabled={busy}
                  maxLength={100000}
                />
              </label>
              {draftError && (
                <p className="inline-error" role="alert">
                  {draftError}
                </p>
              )}
              <div className="modal-actions">
                <button
                  className="button secondary"
                  type="button"
                  disabled={busy}
                  onClick={() => setDraftForm(null)}
                >
                  取消
                </button>
                <button
                  className="button primary"
                  type="submit"
                  disabled={busy}
                >
                  只保存草稿
                </button>
              </div>
            </form>
          </Modal>
        )}
        {review && source && (
          <Modal
            title="确认发送到 GitHub"
            wide
            onClose={() => {
              if (!busy) setReview(null);
            }}
          >
            <GithubDraftReview
              draft={review}
              repository={source.repository}
              account={source.verified_login || "尚未确认"}
            />
            <p className="github-note">
              这次确认仅用于当前版本正文。来源或草稿已变化时会拒绝发送，不会自动改用新版本。
            </p>
            {!canSendReview && (
              <p className="github-note">
                来源或草稿状态已更新，请返回检查当前回执和正文。
              </p>
            )}
            {reviewError && (
              <p className="inline-error" role="alert">
                {reviewError}
              </p>
            )}
            <div className="modal-actions">
              <button
                className="button secondary"
                disabled={busy}
                onClick={() => setReview(null)}
              >
                返回检查
              </button>
              <button
                className="button primary"
                disabled={busy || !canSendReview}
                onClick={sendDraft}
              >
                确认发送这份草稿
              </button>
            </div>
          </Modal>
        )}
        {bindingOpen && source && (
          <Modal
            title="关联仓库讨论"
            onClose={() => {
              if (!busy) setBindingOpen(false);
            }}
          >
            <p className="modal-description">
              只核验当前仓库的 Issue／PR；关联后事件进入该任务，沿用原任务授权。
            </p>
            <label className="field">
              <span>任务</span>
              <select
                aria-label="选择关联任务"
                value={bindingTicket}
                onChange={(event) => setBindingTicket(event.target.value)}
                disabled={busy}
              >
                <option value="">请选择任务</option>
                {tickets.map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.title}
                  </option>
                ))}
              </select>
            </label>
            <label className="field">
              <span>Issue／PR 编号</span>
              <input
                inputMode="numeric"
                value={bindingNumber}
                onChange={(event) => setBindingNumber(event.target.value)}
                disabled={busy}
              />
            </label>
            {error && (
              <p className="inline-error" role="alert">
                {error}
              </p>
            )}
            <div className="modal-actions">
              <button
                className="button secondary"
                disabled={busy}
                onClick={() => setBindingOpen(false)}
              >
                取消
              </button>
              <button
                className="button primary"
                disabled={busy || !bindingTicket || !bindingNumber}
                onClick={() => void bind()}
              >
                核验并关联
              </button>
            </div>
          </Modal>
        )}
      </div>
    </div>
  );
}

export function GithubSourceFields({
  value,
  onChange,
  disabled,
}: {
  value: GithubSourceDraft;
  onChange: (changes: Partial<GithubSourceDraft>) => void;
  disabled: boolean;
}) {
  return (
    <>
      <p className="modal-description">
        默认关闭后台轮询、只读且禁止发送。开启后台轮询会访问
        GitHub；任务模式可按授权和接单规则启动模型。
      </p>
      <label className="field">
        <span>仓库</span>
        <input
          aria-label="GitHub 仓库"
          placeholder="owner/repo"
          value={value.repository}
          onChange={(event) => onChange({ repository: event.target.value })}
          required
          disabled={disabled}
        />
      </label>
      <div className="github-form-two">
        <label className="field">
          <span>来源模式</span>
          <select
            aria-label="来源模式"
            value={value.mode}
            onChange={(event) =>
              onChange({
                mode: event.target.value as GithubSourceDraft["mode"],
              })
            }
            disabled={disabled}
          >
            <option value="read_only">只读：记录事件，不创建任务或发送</option>
            <option value="tasks">任务：允许按授权与规则创建工单</option>
          </select>
        </label>
        <label className="field">
          <span>轮询间隔（秒）</span>
          <input
            type="number"
            min={30}
            value={value.poll_seconds}
            onChange={(event) =>
              onChange({ poll_seconds: Number(event.target.value) })
            }
            disabled={disabled}
          />
        </label>
      </div>
      <div className="github-config-switches">
        <label className="github-checkbox">
          <input
            type="checkbox"
            checked={value.enabled}
            onChange={(event) => onChange({ enabled: event.target.checked })}
            disabled={disabled}
          />
          启用后台轮询
        </label>
        <label className="github-checkbox">
          <input
            type="checkbox"
            checked={value.allow_writes}
            onChange={(event) =>
              onChange({ allow_writes: event.target.checked })
            }
            disabled={disabled}
          />
          允许审阅后发送评论或 PR
        </label>
      </div>
      <fieldset className="github-observation">
        <legend>读取范围</legend>
        {(
          [
            ["observe_issues", "Issues"],
            ["observe_pulls", "Pull requests"],
            ["observe_comments", "评论与审核"],
            ["observe_ci", "CI 运行"],
          ] as const
        ).map(([key, label]) => (
          <label className="github-checkbox" key={key}>
            <input
              type="checkbox"
              checked={value[key]}
              disabled={disabled}
              onChange={(event) => onChange({ [key]: event.target.checked })}
            />
            {label}
          </label>
        ))}
      </fieldset>
      <div className="github-form-two">
        <label className="field">
          <span>允许提出工作请求的账号（每行一个）</span>
          <textarea
            rows={3}
            value={value.allowed_actors}
            onChange={(event) =>
              onChange({ allowed_actors: event.target.value })
            }
            disabled={disabled}
          />
          <small>名单以外的消息只能提供上下文。</small>
        </label>
        <label className="field">
          <span>允许观察的 Bot（每行一个）</span>
          <textarea
            rows={3}
            value={value.allowed_bots}
            onChange={(event) => onChange({ allowed_bots: event.target.value })}
            disabled={disabled}
          />
          <small>Bot 不获得人的授权，也不能恢复已结束任务。</small>
        </label>
      </div>
      <label className="field">
        <span>机器人账号（识别 @ 提及，可选）</span>
        <input
          value={value.bot_login}
          onChange={(event) => onChange({ bot_login: event.target.value })}
          disabled={disabled}
          placeholder="例如 aster-bot"
        />
      </label>
    </>
  );
}

export function GithubDraftReview({
  draft,
  repository,
  account,
}: {
  draft: GithubDraft;
  repository: string;
  account: string;
}) {
  return (
    <div className="github-review">
      <dl>
        <dt>仓库</dt>
        <dd>{repository}</dd>
        <dt>发送账号</dt>
        <dd>{account}</dd>
        <dt>目标</dt>
        <dd>
          {draft.kind === "comment"
            ? `Issue／PR #${draft.number} 的评论`
            : `${draft.head} → ${draft.base} 的 Pull request`}
        </dd>
      </dl>
      {draft.title && <h3>{draft.title}</h3>}
      <div className="github-review-body">
        <Markdown>{draft.body}</Markdown>
      </div>
    </div>
  );
}
