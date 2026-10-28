import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import type { FormEvent } from "react";
import {
  ApiError,
  download,
  post,
  readableError,
  request,
  streamRun,
} from "./api";
import { ModelOptions } from "./ComposerOptions";
import ContextMeter from "./ContextMeter";
import Conversation from "./Conversation";
import PendingCard from "./PendingCard";
import { Icon } from "./icons";
import { readSessionSnapshot } from "./sessionSnapshot";
import { sessionState } from "./SessionLifecycle";
import { StreamCache, SubmissionLedger } from "./state";
import { activeStatus, Markdown, sizeLabel, Spinner, Status } from "./ui";
import type {
  Artifact,
  Lineage,
  Mail,
  ModelProvider,
  Run,
  RunSelection,
  Session,
  SessionDetail,
} from "./types";

const enc = encodeURIComponent;

/** Each mounted panel owns one child session; hiding it never cancels its work. */
export default function SideChatPanel({
  session,
  visible,
  providers,
  lifecycleBusy,
  assistantName,
  options,
  onSelect,
  onCollapse,
  onRequestDelete,
  onSource,
  onChanged,
  onAuthError,
}: {
  session: Session;
  visible: boolean;
  providers: ModelProvider[];
  lifecycleBusy: boolean;
  assistantName: string;
  options: Session[];
  onSelect: (session: Session) => void;
  onCollapse: () => void;
  onRequestDelete: () => void;
  onSource: () => void;
  onChanged: () => Promise<void>;
  onAuthError: (error: unknown) => void;
}) {
  const sid = session.id;
  const [detail, setDetail] = useState<SessionDetail | null>(null),
    [run, setRun] = useState<Run | null>(null),
    [selection, setSelection] = useState<RunSelection | null>(null),
    [draft, setDraft] = useState(""),
    [files, setFiles] = useState<Artifact[]>([]),
    [mail, setMail] = useState<Mail[]>([]),
    [deliverables, setDeliverables] = useState<Artifact[]>([]),
    [loading, setLoading] = useState(true),
    [sending, setSending] = useState(false),
    [uploading, setUploading] = useState(false),
    [acting, setActing] = useState(false),
    [stream, setStream] = useState(""),
    [streamWarning, setStreamWarning] = useState(""),
    [error, setError] = useState(""),
    [scrolledUp, setScrolledUp] = useState(false);
  const mounted = useRef(false),
    generation = useRef(0),
    flight = useRef<Promise<void> | null>(null),
    dirty = useRef(false),
    runRef = useRef<Run | null>(null),
    selectionRef = useRef<RunSelection | null>(null),
    sendFlight = useRef(false),
    uploadFlight = useRef(false),
    actionFlight = useRef(false),
    cache = useRef(new StreamCache()),
    ledger = useRef(new SubmissionLedger()),
    scroll = useRef<HTMLDivElement>(null),
    content = useRef<HTMLDivElement>(null),
    input = useRef<HTMLTextAreaElement>(null),
    fileInput = useRef<HTMLInputElement>(null),
    reading = useRef(false),
    lastTop = useRef(0),
    firstScroll = useRef(true);
  const busy = activeStatus(run?.status),
    closed = !!detail?.lineage?.closed_at,
    locked =
      loading ||
      lifecycleBusy ||
      !detail ||
      closed ||
      sessionState(detail) !== "active",
    provider = providers.find((item) => item.id === selection?.provider_id),
    ready =
      !!provider?.configured &&
      provider.models.some((item) => item.id === selection?.model);
  const report = useCallback(
    (value: unknown) => {
      if (!mounted.current) return;
      if (value instanceof ApiError && value.status === 401) onAuthError(value);
      else setError(readableError(value));
    },
    [onAuthError],
  );
  const refresh = useCallback((): Promise<void> => {
    if (flight.current) {
      dirty.current = true;
      return flight.current;
    }
    const task = async () => {
      do {
        dirty.current = false;
        const ticket = ++generation.current;
        const valid = () => mounted.current && ticket === generation.current;
        const snapshot = await readSessionSnapshot(
          () => request<SessionDetail>(`/sessions/${enc(sid)}`),
          (id) => request<Run>(`/runs/${enc(id)}`),
          valid,
        );
        if (!snapshot || !valid()) continue;
        if ("retry" in snapshot) {
          dirty.current = true;
          continue;
        }
        const relation = await request<{ lineage: Lineage | null }>(
          `/sessions/${enc(sid)}/lineage`,
        );
        if (!valid()) continue;
        setDetail({
          ...snapshot.detail,
          lineage: relation.lineage || undefined,
        });
        runRef.current = snapshot.run;
        setRun(snapshot.run);
        if (!selectionRef.current && snapshot.detail.selection) {
          selectionRef.current = snapshot.detail.selection;
          setSelection(snapshot.detail.selection);
        }
        if (snapshot.run) {
          cache.current.synchronize(
            snapshot.run.id,
            snapshot.detail.messages,
            !activeStatus(snapshot.run.status),
          );
          setStream(cache.current.get(snapshot.run.id).text);
        } else setStream("");
        const extra = await Promise.all([
          request<{ messages: Mail[] }>(`/sessions/${enc(sid)}/mailbox`),
          request<{ deliverables: Artifact[] }>(
            `/sessions/${enc(sid)}/deliverables`,
          ),
        ]);
        if (valid()) {
          setMail(extra[0].messages);
          setDeliverables(extra[1].deliverables);
        }
      } while (dirty.current && mounted.current);
    };
    flight.current = task().finally(() => {
      flight.current = null;
    });
    return flight.current;
  }, [sid]);
  useEffect(() => {
    mounted.current = true;
    void refresh()
      .catch(report)
      .finally(() => {
        if (mounted.current) setLoading(false);
      });
    return () => {
      mounted.current = false;
      generation.current++;
    };
  }, [refresh, report]);
  useEffect(() => {
    if (!visible && !busy) return;
    void refresh().catch(report);
    const timer = setInterval(() => void refresh().catch(report), 1800);
    return () => clearInterval(timer);
  }, [visible, busy, session.archived_at, refresh, report]);
  useEffect(() => {
    if (!run?.id || !busy) return;
    const rid = run.id,
      controller = new AbortController();
    const valid = () =>
      mounted.current &&
      !controller.signal.aborted &&
      runRef.current?.id === rid;
    void (async () => {
      let failures = 0;
      while (valid()) {
        try {
          setStreamWarning("");
          await streamRun(
            rid,
            cache.current.get(rid).cursor,
            (event) => {
              if (!valid() || !cache.current.accept(rid, event)) return;
              setStream(cache.current.get(rid).text);
              if (
                [
                  "tool.started",
                  "tool.finished",
                  "approval.waiting",
                  "question.waiting",
                  "approval.requested",
                  "question.requested",
                  "run.phase",
                  "run.finished",
                  "run.status",
                  "plan.updated",
                ].includes(event.type)
              )
                void refresh().catch(report);
            },
            controller.signal,
          );
          if (!valid()) return;
          await refresh();
          void onChanged();
          if (!activeStatus(runRef.current?.status)) return;
        } catch (error) {
          if (!valid()) return;
          if (error instanceof ApiError && error.status === 401) {
            report(error);
            return;
          }
          failures++;
          setStreamWarning("实时连接暂时中断，正在同步已保存的消息…");
          if (failures > 5) return;
        }
        await new Promise((resolve) =>
          setTimeout(resolve, Math.min(4000, 800 * (failures + 1))),
        );
      }
    })();
    return () => controller.abort();
  }, [run?.id, busy, refresh, report, onChanged]);
  const toBottom = useCallback(() => {
    if (!scroll.current) return;
    scroll.current.scrollTop = scroll.current.scrollHeight;
    lastTop.current = scroll.current.scrollTop;
  }, []);
  useLayoutEffect(() => {
    if (!visible || loading) return;
    if (firstScroll.current || !reading.current) toBottom();
    firstScroll.current = false;
    const observer = new ResizeObserver(() => {
      if (!reading.current) toBottom();
    });
    if (content.current) observer.observe(content.current);
    if (scroll.current) observer.observe(scroll.current);
    return () => observer.disconnect();
  }, [visible, loading, toBottom]);
  useLayoutEffect(() => {
    if (visible && !reading.current) toBottom();
  }, [
    visible,
    stream,
    detail?.messages.length,
    run?.pending_requests?.length,
    toBottom,
  ]);
  useEffect(() => {
    if (visible && !loading) input.current?.focus({ preventScroll: true });
  }, [visible, loading]);
  useLayoutEffect(() => {
    if (!input.current || !visible) return;
    input.current.style.height = "auto";
    input.current.style.height = `${Math.min(140, input.current.scrollHeight)}px`;
  }, [draft, visible]);

  const act = async (action: () => Promise<unknown>) => {
    if (locked || actionFlight.current) return;
    actionFlight.current = true;
    setActing(true);
    setError("");
    try {
      await action();
    } catch (error) {
      report(error);
    } finally {
      generation.current++;
      await refresh().catch(report);
      void onChanged();
      actionFlight.current = false;
      setActing(false);
    }
  };
  const submit = async (event?: FormEvent) => {
    event?.preventDefault();
    if (
      locked ||
      sendFlight.current ||
      uploadFlight.current ||
      (!draft.trim() && !files.length) ||
      (!busy && !ready) ||
      !selection ||
      run?.status === "needs_reconciliation"
    )
      return;
    sendFlight.current = true;
    setSending(true);
    setError("");
    const text = draft,
      attachments = [...files];
    reading.current = false;
    setScrolledUp(false);
    toBottom();
    try {
      const attempt = ledger.current.prepare(
        sid,
        text,
        attachments.map((file) => file.attachment_id!),
        false,
        busy,
        selection,
      );
      const accepted = await post<Run>(attempt.endpoint, attempt.body);
      ledger.current.accepted(attempt);
      generation.current++;
      if (attempt.endpoint.endsWith("/runs")) {
        runRef.current = accepted;
        setRun(accepted);
        setStream("");
      }
      setDraft((value) => (value === text ? "" : value));
      setFiles((value) => value.filter((file) => !attachments.includes(file)));
      await refresh();
      void onChanged();
    } catch (error) {
      report(error);
    } finally {
      sendFlight.current = false;
      setSending(false);
    }
  };
  const upload = async (values: FileList | null) => {
    if (!values?.length || locked || uploadFlight.current || sendFlight.current)
      return;
    uploadFlight.current = true;
    setUploading(true);
    setError("");
    try {
      if (files.length + values.length > 20)
        throw new Error("每条消息最多添加 20 个附件。");
      for (const file of Array.from(values)) {
        if (file.size > 25 * 1024 * 1024)
          throw new Error(`${file.name} 超过 25 MiB，请选择更小的文件。`);
        const artifact = await request<Artifact>(
          `/sessions/${enc(sid)}/attachments?filename=${enc(file.name)}`,
          {
            method: "POST",
            headers: { "Content-Type": "application/octet-stream" },
            body: file,
          },
        );
        setFiles((old) => [...old, artifact]);
      }
    } catch (error) {
      report(error);
    } finally {
      uploadFlight.current = false;
      setUploading(false);
      if (fileInput.current) fileInput.current.value = "";
    }
  };
  // Inherited messages remain in the archive/model context. The panel displays
  // their source as a folded quote, followed only by this child's own turns.
  const inherited =
    detail?.messages.filter(
      (item) => item.message.metadata.inherited_from_session,
    ) || [];
  const quote =
    [...inherited]
      .reverse()
      .find((item) => item.message.role === "assistant" && item.message.content)
      ?.message.content || "此处之前的主对话";
  const records =
    detail?.messages.filter(
      (item) => !item.message.metadata.inherited_from_session,
    ) || [];
  return (
    <aside
      className="side-chat-panel"
      aria-label="侧边聊天"
      hidden={!visible}
      data-session-id={sid}
      onKeyDown={(event) => {
        if (
          event.key === "Escape" &&
          !event.defaultPrevented &&
          !(event.target as HTMLElement).closest('[role="dialog"]')
        ) {
          event.stopPropagation();
          onCollapse();
        }
      }}
    >
      <header className="side-chat-heading">
        <div>
          <Icon name="panel" size={18} />
          <h2>侧边聊天</h2>
          {busy && <Status status={run!.status} />}
        </div>
        <div className="side-chat-actions">
          <button
            className="side-chat-collapse"
            aria-label="收起侧边聊天"
            title="收起面板，保留消息、草稿和运行中的任务"
            onClick={onCollapse}
          >
            <Icon name="chevron" size={15} />
            <span>收起</span>
          </button>
          <button
            className="icon-button"
            aria-label="关闭侧边聊天"
            title="关闭并确认删除侧边聊天"
            disabled={sending || uploading || acting || lifecycleBusy}
            onClick={onRequestDelete}
          >
            <Icon name="close" size={18} />
          </button>
        </div>
      </header>
      {options.length > 1 && (
        <select
          className="side-chat-switcher"
          aria-label="切换侧边聊天"
          value={sid}
          onChange={(event) => {
            const item = options.find((item) => item.id === event.target.value);
            if (item) onSelect(item);
          }}
        >
          {options.map((item, index) => (
            <option key={item.id} value={item.id}>
              {index + 1}. {item.title || "侧边聊天"}
            </option>
          ))}
        </select>
      )}
      <div className="side-chat-context">
        <details>
          <summary>
            <Icon name="chat" size={14} />
            <span>
              来自主对话<span>{quote.slice(0, 100)}</span>
            </span>
            <Icon name="down" size={14} />
          </summary>
          <blockquote>
            <Markdown>{quote}</Markdown>
          </blockquote>
          <p>已带入截至此处的上下文，后续讨论保留在侧边聊天中。</p>
          <button className="text-button" onClick={onSource}>
            查看来源消息
          </button>
        </details>
      </div>
      {(error || session.selection_warning) && (
        <div className="side-chat-error" role="alert">
          <span>
            {error || "来源对话的模型或 Skill 不可用，请重新确认当前选择。"}
          </span>
          {error && (
            <button
              className="icon-button"
              aria-label="重试载入侧边聊天"
              onClick={() => {
                setError("");
                void refresh().catch(report);
              }}
            >
              <Icon name="refresh" size={15} />
            </button>
          )}
        </div>
      )}
      <div
        className="side-chat-scroll"
        ref={scroll}
        onScroll={(event) => {
          const el = event.currentTarget;
          if (el.scrollHeight - el.scrollTop - el.clientHeight < 100)
            reading.current = false;
          else if (el.scrollTop < lastTop.current) reading.current = true;
          lastTop.current = el.scrollTop;
          setScrolledUp(reading.current);
        }}
      >
        <div className="side-chat-messages" ref={content}>
          {loading ? (
            <div className="loading-history">
              <Spinner />
              正在载入侧边聊天
            </div>
          ) : (
            <>
              {!records.length && !busy && (
                <div className="side-chat-empty">
                  <Icon name="chat" size={26} />
                  <h3>继续聊聊这一点</h3>
                  <p>在这里追问、展开想法，主对话留在原处。</p>
                </div>
              )}
              <Conversation
                records={records}
                stream={stream}
                runId={run?.id || null}
                busy={busy}
                report={report}
              />
              {busy && !stream && (
                <div className="working-line">
                  <Spinner />
                  <span>
                    {run?.status === "waiting_approval"
                      ? "等待你的批准"
                      : run?.status === "waiting_input"
                        ? "等待你的回答"
                        : "正在处理侧边聊天…"}
                  </span>
                </div>
              )}
              {run?.pending_requests?.map((pending) => (
                <PendingCard
                  key={pending.id}
                  pending={pending}
                  busy={locked || acting}
                  submit={(answer) =>
                    act(async () => {
                      const result = await post<{ accepted: boolean }>(
                        pending.kind === "approval"
                          ? `/approvals/${enc(pending.id)}/decision`
                          : `/questions/${enc(pending.id)}/answer`,
                        pending.kind === "approval"
                          ? { approved: answer }
                          : { answer },
                      );
                      if (!result.accepted)
                        throw new Error(
                          "这项请求已经结束或过期，已刷新当前状态。",
                        );
                    })
                  }
                />
              ))}
              {run && !busy && run.status !== "completed" && (
                <div className={`run-result ${run.status}`}>
                  <Icon name="info" size={16} />
                  <div>
                    <Status status={run.status} />
                    <p>
                      {run.status === "needs_reconciliation"
                        ? "请先核对结果未知的操作，避免重复执行。"
                        : run.status === "cancelled"
                          ? "侧边任务已停止，记录已保留。"
                          : run.error || "这次任务未完整完成。"}
                    </p>
                  </div>
                </div>
              )}
              {deliverables.length > 0 && (
                <details className="side-chat-deliverables">
                  <summary>文件与成果 · {deliverables.length}</summary>
                  {deliverables.map((file) => (
                    <button
                      className="attachment-row"
                      key={file.artifact_id}
                      onClick={() =>
                        void download(
                          file.artifact_id,
                          file.filename || file.title || "artifact",
                        ).catch(report)
                      }
                    >
                      <Icon name="download" size={15} />
                      <span>
                        {file.title || file.filename || "文件"}
                        <small>{sizeLabel(file.size)}</small>
                      </span>
                    </button>
                  ))}
                </details>
              )}
            </>
          )}
        </div>
      </div>
      <div className="side-chat-compose">
        {scrolledUp && (
          <button
            className="side-chat-jump"
            onClick={() => {
              reading.current = false;
              setScrolledUp(false);
              toBottom();
            }}
          >
            <Icon name="down" size={13} />
            回到最新消息
          </button>
        )}
        {streamWarning && <div className="stream-warning">{streamWarning}</div>}
        {mail
          .filter((item) => item.state === "pending")
          .map((item) => (
            <div className="side-chat-queued" key={item.id}>
              <span>待发送 · {item.text || "附件消息"}</span>
              <button
                disabled={locked || acting}
                onClick={() =>
                  void act(async () => {
                    const result = await request<{ retracted: boolean }>(
                      `/sessions/${enc(sid)}/mailbox/${enc(item.id)}`,
                      { method: "DELETE" },
                    );
                    if (!result.retracted)
                      throw new Error("消息已被接收，无法撤回。");
                  })
                }
              >
                撤回
              </button>
            </div>
          ))}
        <form
          className={`composer ${locked ? "disabled" : ""}`}
          onSubmit={submit}
        >
          {files.length > 0 && (
            <div className="draft-attachments">
              {files.map((file) => (
                <div key={file.attachment_id}>
                  <Icon name="file" size={17} />
                  <span>
                    <strong>{file.filename}</strong>
                    <small>{sizeLabel(file.size)}</small>
                  </span>
                  <button
                    type="button"
                    aria-label={`移除 ${file.filename}`}
                    disabled={locked || sending}
                    onClick={() =>
                      setFiles((old) => old.filter((item) => item !== file))
                    }
                  >
                    <Icon name="close" size={13} />
                  </button>
                </div>
              ))}
            </div>
          )}
          <textarea
            ref={input}
            aria-label="侧边聊天消息"
            rows={2}
            value={draft}
            disabled={locked}
            placeholder={
              closed
                ? "侧边聊天已结束，记录已保留"
                : sessionState(detail) !== "active"
                  ? "已归档，仅供查看"
                  : "围绕这条消息继续讨论…"
            }
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              if (
                event.key === "Enter" &&
                !event.shiftKey &&
                !event.nativeEvent.isComposing
              ) {
                event.preventDefault();
                void submit();
              }
            }}
          />
          <div className="side-chat-identity">
            <output
              className="agent-label"
              aria-label="当前助手"
              title="创建后助手只读"
            >
              {assistantName}
            </output>
            {selection?.selected_skill && (
              <button
                type="button"
                className="selected-skill-chip"
                disabled={locked}
                aria-label="移除侧边聊天 Skill"
                onClick={() => {
                  const next = { ...selection, selected_skill: null };
                  selectionRef.current = next;
                  setSelection(next);
                }}
              >
                <Icon name="skills" size={12} />
                <span>{selection.selected_skill}</span>
                <Icon name="close" size={11} />
              </button>
            )}
          </div>
          <div className="side-chat-toolbar">
            <button
              type="button"
              className="icon-button"
              aria-label="添加侧边聊天附件"
              disabled={locked || uploading || sending}
              onClick={() => fileInput.current?.click()}
            >
              {uploading ? <Spinner /> : <Icon name="plus" size={20} />}
            </button>
            <div className="side-chat-options">
              {selection && (
                <>
                  <ContextMeter
                    sessionId={sid}
                    revision={`${detail?.messages.length}:${run?.updated_at}:${run?.status}`}
                    model={selection.model}
                    providerId={selection.provider_id}
                    active={busy && visible}
                  />
                  <ModelOptions
                    providers={providers}
                    selection={selection}
                    busy={busy}
                    disabled={locked}
                    onChange={(value) => {
                      selectionRef.current = value;
                      setSelection(value);
                    }}
                  />
                </>
              )}
              {busy && (
                <button
                  type="button"
                  className="cancel-button"
                  aria-label="停止侧边任务"
                  disabled={locked || acting || run?.status === "cancelling"}
                  onClick={() =>
                    void act(async () => {
                      const result = await post<{ accepted: boolean }>(
                        `/runs/${enc(run!.id)}/cancel`,
                      );
                      if (!result.accepted)
                        throw new Error("侧边任务已结束，已刷新状态。");
                    })
                  }
                >
                  <Icon name="stop" size={13} />
                </button>
              )}
              <button
                type="submit"
                className="send-button"
                aria-label="发送侧边聊天消息"
                disabled={
                  locked ||
                  sending ||
                  uploading ||
                  (!draft.trim() && !files.length) ||
                  (!busy && !ready) ||
                  run?.status === "needs_reconciliation"
                }
              >
                {sending ? <Spinner /> : <Icon name="arrow" size={19} />}
              </button>
            </div>
          </div>
        </form>
        <input
          ref={fileInput}
          type="file"
          hidden
          multiple
          aria-label="侧边聊天附件"
          onChange={(event) => void upload(event.target.files)}
        />
        <div className="side-chat-footer">
          <span>
            {closed
              ? "记录保留，可随时查看"
              : busy
                ? "补充消息仅发送到侧边聊天"
                : !ready
                  ? "请在设置中配置可用模型"
                  : "独立讨论 · 不写入主对话"}
          </span>
        </div>
      </div>
    </aside>
  );
}
