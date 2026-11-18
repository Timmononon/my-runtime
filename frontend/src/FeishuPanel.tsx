import { Alert, Button, Card, Chip } from "@heroui/react";
import { useEffect, useRef, useState } from "react";
import { post, readableError, request } from "./api";
import {
  feishuEventLabel,
  feishuEventReason,
  feishuHealthLabel,
  feishuSourceError,
  feishuSourceFields,
} from "./feishuTypes";
import type {
  FeishuDiscovery,
  FeishuEvent,
  FeishuEventPage,
  FeishuSource,
  FeishuSourceForm,
} from "./feishuTypes";
import { Icon } from "./icons";
import { LatestRequest } from "./state";
import TaskTriageSettingsModal, {
  TaskTriageDecisionCard,
} from "./TaskTriageSettings";
import { taskLabel } from "./taskTypes";
import type { TaskTicket } from "./taskTypes";
import { dateLabel, Empty, Modal, Spinner } from "./ui";
import "./github.css";
import "./feishu.css";

const sourcePath = (id: string) => `/feishu/sources/${encodeURIComponent(id)}`;
const sourceName = (source: FeishuSource) => source.name || source.chat_id;
const healthColors = {
  disabled: "default",
  connecting: "accent",
  listening: "success",
  setup_required: "warning",
  disconnected: "danger",
  identity_changed: "danger",
  group_unavailable: "danger",
} as const;

type SourceContext = { workspace: string; agent: string; conversation: string };
type SourceFields = ReturnType<typeof feishuSourceFields>;
type Detail = {
  source: FeishuSource;
  page: FeishuEventPage;
  tickets: TaskTicket[];
  before: number | null;
};

async function readDetail(
  id: string,
  before: number | null,
  signal: AbortSignal,
) {
  const path = sourcePath(id);
  const [source, page, linked] = await Promise.all([
    request<FeishuSource>(path, { signal }),
    request<FeishuEventPage>(
      `${path}/events?limit=50${before === null ? "" : `&before=${before}`}`,
      { signal },
    ),
    request<{ tickets: TaskTicket[] }>(`${path}/tickets`, { signal }),
  ]);
  if (
    source?.id !== id ||
    !Array.isArray(page?.events) ||
    (page.next_before !== null &&
      (!Number.isSafeInteger(page.next_before) ||
        page.next_before < 1 ||
        (before !== null && page.next_before >= before))) ||
    !Array.isArray(linked?.tickets)
  )
    throw new Error("飞书来源的本地记录无法读取。");
  return { source, page, tickets: linked.tickets, before };
}

async function patchSource(
  source: FeishuSource,
  fields: Partial<SourceFields> & { enabled?: boolean },
) {
  const saved = await request<FeishuSource>(sourcePath(source.id), {
    method: "PATCH",
    body: JSON.stringify({ expected_revision: source.revision, ...fields }),
  });
  if (
    saved?.id !== source.id ||
    !Number.isSafeInteger(saved.revision) ||
    saved.revision < source.revision ||
    (fields.enabled !== undefined && saved.enabled !== fields.enabled)
  )
    throw new Error("无法确认来源变更，请重新读取本地状态。");
  return saved;
}

export default function FeishuPanel({
  sourceWritable,
  sourceContext,
  ensureSession,
  onOpenSession,
  onOpenTicket,
  initialSourceId = null,
}: {
  sourceWritable: boolean;
  sourceContext: SourceContext;
  ensureSession: () => Promise<string>;
  onOpenSession: (id: string) => void;
  onOpenTicket: (id: string) => void;
  initialSourceId?: string | null;
}) {
  const [sources, setSources] = useState<FeishuSource[]>([]);
  const [listLoading, setListLoading] = useState(true);
  const [selectedId, setSelectedId] = useState(initialSourceId);
  const [detail, setDetail] = useState<Detail | null>(null);
  const [tab, setTab] = useState<"events" | "tasks">("events");
  const [pageTrail, setPageTrail] = useState<(number | null)[]>([null]);
  const [reload, setReload] = useState(0);
  const [listError, setListError] = useState("");
  const [detailError, setDetailError] = useState("");
  const [actionError, setActionError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [editor, setEditor] = useState<"new" | FeishuSource | null>(null);
  const [formError, setFormError] = useState("");
  const [triageSettingsOpen, setTriageSettingsOpen] = useState(false);
  const readGate = useRef(new LatestRequest());
  const operation = useRef(false);
  const mounted = useRef(false);
  const before = pageTrail.at(-1) ?? null;
  const current = detail?.source.id === selectedId ? detail : null;
  const refresh = () => {
    readGate.current.invalidate();
    setReload((value) => value + 1);
  };

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  useEffect(() => {
    const control = new AbortController();
    const { signal } = control;
    let reading = false;
    const load = async () => {
      if (reading || operation.current) return;
      reading = true;
      const epoch = readGate.current.begin();
      const valid = () => !signal.aborted && readGate.current.current(epoch);
      const [list, item] = await Promise.allSettled([
        request<{ sources: FeishuSource[] }>("/feishu/sources", { signal }),
        selectedId ? readDetail(selectedId, before, signal) : null,
      ]);
      reading = false;
      if (!valid()) return;
      setListLoading(false);
      if (list.status === "rejected") setListError(readableError(list.reason));
      else if (!Array.isArray(list.value?.sources))
        setListError("飞书来源列表无法读取。");
      else {
        setSources(list.value.sources);
        setListError("");
      }
      if (item.status === "rejected")
        setDetailError(readableError(item.reason));
      else {
        setDetail(item.value);
        setDetailError("");
      }
    };
    void load();
    const timer = window.setInterval(() => {
      if (!document.hidden) void load();
    }, 3000);
    return () => {
      control.abort();
      window.clearInterval(timer);
    };
  }, [selectedId, before, reload]);

  const selectSource = (id: string) => {
    readGate.current.invalidate();
    setSelectedId(id);
    setPageTrail([null]);
    setTab("events");
    setDetailError("");
    setNotice("");
    setActionError("");
  };
  const goToPage = (trail: (number | null)[]) => {
    readGate.current.invalidate();
    setPageTrail(trail);
    setDetailError("");
  };

  const commit = async (
    write: () => Promise<FeishuSource>,
    onSaved: (saved: FeishuSource) => void,
    onError: (message: string) => void = setActionError,
  ) => {
    if (operation.current) return;
    operation.current = true;
    readGate.current.invalidate();
    setBusy(true);
    setActionError("");
    setNotice("");
    try {
      const saved = await write();
      if (!mounted.current) return;
      setSources((list) =>
        list.some((source) => source.id === saved.id)
          ? list.map((source) => (source.id === saved.id ? saved : source))
          : [saved, ...list],
      );
      setDetail((value) =>
        value?.source.id === saved.id ? { ...value, source: saved } : value,
      );
      onSaved(saved);
    } catch (failure) {
      if (mounted.current) onError(readableError(failure));
    } finally {
      operation.current = false;
      if (mounted.current) {
        setBusy(false);
        refresh();
      }
    }
  };

  const saveSource = (chatId: string, fields: SourceFields) => {
    if (!editor) return;
    setFormError("");
    void commit(
      async () => {
        if (editor !== "new") return patchSource(editor, fields);
        if (!sourceWritable)
          throw new Error("请切换到可用的普通对话后添加来源。");
        const owner = await ensureSession();
        const { name, ...rules } = fields;
        const saved = await post<FeishuSource>("/feishu/sources", {
          source_session_id: owner,
          chat_id: chatId,
          ...(name ? { name } : {}),
          ...rules,
        });
        if (
          !saved?.id ||
          saved.enabled !== false ||
          saved.source_session_id !== owner ||
          saved.chat_id !== chatId
        )
          throw new Error("无法确认新来源，请刷新后核对来源状态。");
        return saved;
      },
      (saved) => {
        setEditor(null);
        if (editor === "new") selectSource(saved.id);
        setNotice(
          editor === "new" ? "来源已添加，等待启用。" : "来源配置已保存。",
        );
      },
      setFormError,
    );
  };

  const toggle = (source: FeishuSource) => {
    const enabled = !source.enabled;
    void commit(
      () => patchSource(source, { enabled }),
      () => setNotice(enabled ? "已启用来源。" : "已暂停接收新消息。"),
    );
  };

  const openEditor = (value: "new" | FeishuSource) => {
    setFormError("");
    setEditor(value);
  };

  return (
    <div className="github-scroll">
      <div className="github-panel feishu-panel">
        <header className="github-heading">
          <div className="github-heading-title">
            <span className="github-heading-icon">
              <Icon name="chat" size={23} />
            </span>
            <div>
              <h1>飞书</h1>
              <p>在指定群聊中接收获准成员 @ 机器人的任务消息。</p>
            </div>
          </div>
          <div className="github-heading-actions">
            <Button
              className="github-action"
              variant="outline"
              onPress={() => setTriageSettingsOpen(true)}
            >
              任务判断模型
            </Button>
            <Button
              className="github-action"
              variant="primary"
              isDisabled={!sourceWritable || busy}
              onPress={() => openEditor("new")}
            >
              <Icon name="plus" size={16} />
              添加飞书来源
            </Button>
          </div>
        </header>
        {[
          ...new Set([actionError, listError, detailError].filter(Boolean)),
        ].map((message) => (
          <Alert
            className="github-feedback"
            status="danger"
            role="alert"
            key={message}
          >
            <Alert.Indicator />
            <Alert.Content>
              <Alert.Description>{message}</Alert.Description>
            </Alert.Content>
            <Button
              variant="ghost"
              size="sm"
              isDisabled={busy}
              onPress={() => {
                setActionError("");
                refresh();
              }}
            >
              重新读取本地状态
            </Button>
          </Alert>
        ))}
        {notice && (
          <Alert className="github-feedback" status="success" role="status">
            <Alert.Indicator />
            <Alert.Content>
              <Alert.Description>{notice}</Alert.Description>
            </Alert.Content>
          </Alert>
        )}
        {!sourceWritable && (
          <p className="github-note">要添加来源，请先切换到可用的普通对话。</p>
        )}
        <div className="github-columns">
          <Card<"section">
            render={(props) => <section {...props} />}
            className="github-sources"
            aria-label="飞书来源列表"
          >
            <Card.Header className="github-list-heading">
              <Card.Title<"h2"> render={(props) => <h2 {...props} />}>
                {sources.length} 个来源
              </Card.Title>
              <Button
                isIconOnly
                variant="ghost"
                size="sm"
                className="github-refresh"
                aria-label="刷新飞书本地状态"
                isDisabled={busy}
                onPress={refresh}
              >
                <Icon name="refresh" size={16} />
              </Button>
            </Card.Header>
            {listLoading ? (
              <div className="github-loading" role="status">
                <Spinner /> 正在读取来源
              </div>
            ) : !sources.length ? (
              !listError && (
                <Empty
                  icon="chat"
                  title="尚未添加飞书来源"
                  detail="新来源默认暂停。保存允许的发送人后，可手动启用接收。"
                />
              )
            ) : (
              sources.map((source) => (
                <Card<"button">
                  render={(props) => <button type="button" {...props} />}
                  className={`github-source-card ${selectedId === source.id ? "selected" : ""}`}
                  key={source.id}
                  variant="transparent"
                  aria-pressed={selectedId === source.id}
                  disabled={busy}
                  onClick={() => selectSource(source.id)}
                >
                  <Card.Header<"span">
                    render={(props) => <span {...props} />}
                    className="github-source-card-heading"
                  >
                    <Card.Title<"span"> render={(props) => <span {...props} />}>
                      {sourceName(source)}
                    </Card.Title>
                    <Icon name="chevron" size={14} />
                  </Card.Header>
                  <Card.Content<"span">
                    render={(props) => <span {...props} />}
                    className="github-source-card-chips"
                  >
                    <FeishuSourceHealth source={source} />
                  </Card.Content>
                  <Card.Footer<"span">
                    render={(props) => <span {...props} />}
                    className="github-source-card-meta"
                  >
                    <Card.Description<"span">
                      render={(props) => <span {...props} />}
                    >
                      {source.bot_name || source.bot_open_id}
                    </Card.Description>
                    <small>
                      <code>{source.chat_id}</code>
                    </small>
                  </Card.Footer>
                </Card>
              ))
            )}
          </Card>
          <Card<"section">
            render={(props) => <section {...props} />}
            className="github-detail"
            aria-label="飞书来源详情"
          >
            {!selectedId ? (
              <Empty
                icon="chat"
                title="选择一个飞书来源"
                detail="在此查看连接状态、已接收消息和关联任务。"
              />
            ) : !current ? (
              !detailError && (
                <div className="github-loading" role="status">
                  <Spinner /> 正在读取本地记录
                </div>
              )
            ) : (
              <>
                <Card.Header className="github-detail-heading">
                  <Card.Title<"h2"> render={(props) => <h2 {...props} />}>
                    {sourceName(current.source)}
                  </Card.Title>
                  <Button
                    className="github-action"
                    variant="outline"
                    size="sm"
                    isDisabled={busy}
                    onPress={() => openEditor(current.source)}
                  >
                    <Icon name="settings" size={15} /> 配置来源
                  </Button>
                </Card.Header>
                <div className="github-source-status">
                  <FeishuSourceHealth source={current.source} />
                  <span>
                    机器人{" "}
                    {current.source.bot_name || current.source.bot_open_id}
                  </span>
                  <span>
                    应用 <code>{current.source.app_id}</code>
                  </span>
                </div>
                {current.source.error && (
                  <p className="github-error" role="status">
                    {feishuSourceError(current.source.error)}
                  </p>
                )}
                <div className="github-source-facts">
                  <span>
                    群聊 <code>{current.source.chat_id}</code>
                  </span>
                  <span>
                    允许 {current.source.allowed_senders.length} 位发送人
                  </span>
                  <span>
                    {current.source.last_event_at !== null
                      ? `最近接收 ${dateLabel(current.source.last_event_at)}`
                      : "尚未接收消息"}
                  </span>
                </div>
                <div className="github-controls">
                  <Button
                    className="github-action"
                    variant={current.source.enabled ? "outline" : "primary"}
                    size="sm"
                    isDisabled={
                      busy ||
                      (!current.source.enabled &&
                        !current.source.allowed_senders.length)
                    }
                    onPress={() => toggle(current.source)}
                  >
                    <Icon
                      name={current.source.enabled ? "pause" : "play"}
                      size={15}
                    />
                    {current.source.enabled ? "暂停接收" : "启用接收"}
                  </Button>
                  <button
                    className="text-button"
                    onClick={() =>
                      onOpenSession(current.source.source_session_id)
                    }
                  >
                    来源对话 <Icon name="external" size={13} />
                  </button>
                </div>
                {!current.source.allowed_senders.length && (
                  <p className="github-note">
                    配置至少一位允许的发送人后，即可启用来源。
                  </p>
                )}
                {current.source.health === "setup_required" && (
                  <FeishuSetupHint />
                )}
                <nav className="github-tabs" aria-label="飞书来源内容">
                  <button
                    aria-pressed={tab === "events"}
                    onClick={() => setTab("events")}
                  >
                    消息事件
                  </button>
                  <button
                    aria-pressed={tab === "tasks"}
                    onClick={() => setTab("tasks")}
                  >
                    关联任务 {current.tickets.length}
                  </button>
                </nav>
                {tab === "events" ? (
                  current.before === before ? (
                    <div aria-label="飞书消息事件">
                      {current.page.events.length ? (
                        current.page.events.map((event) => (
                          <FeishuEventCard
                            key={event.seq}
                            value={event}
                            onOpenTicket={onOpenTicket}
                          />
                        ))
                      ) : (
                        <Empty
                          icon="clock"
                          title="暂无消息记录"
                          detail="启用后，接收到的群消息会在这里显示处理结果。"
                        />
                      )}
                      {(pageTrail.length > 1 ||
                        current.page.next_before !== null) && (
                        <div className="github-event-pagination">
                          <span>本页 {current.page.events.length} 条消息</span>
                          <div>
                            <button
                              className="text-button"
                              disabled={busy || pageTrail.length < 2}
                              onClick={() => goToPage(pageTrail.slice(0, -1))}
                            >
                              上一页
                            </button>
                            <button
                              className="text-button"
                              disabled={
                                busy || current.page.next_before === null
                              }
                              onClick={() =>
                                goToPage([
                                  ...pageTrail,
                                  current.page.next_before,
                                ])
                              }
                            >
                              更早消息
                            </button>
                          </div>
                        </div>
                      )}
                    </div>
                  ) : (
                    !detailError && (
                      <div className="github-loading" role="status">
                        <Spinner /> 正在读取消息
                      </div>
                    )
                  )
                ) : current.tickets.length ? (
                  current.tickets.map((ticket) => (
                    <Card<"article">
                      render={(props) => <article {...props} />}
                      key={ticket.id}
                      className="github-linked-task"
                    >
                      <Card.Header>
                        <Chip size="sm" variant="soft">
                          {taskLabel(ticket.state)}
                        </Chip>
                        <Card.Title>{ticket.title}</Card.Title>
                      </Card.Header>
                      <Card.Footer className="github-linked-task-footer">
                        <button
                          className="text-button"
                          onClick={() => onOpenTicket(ticket.id)}
                        >
                          在任务看板打开 <Icon name="chevron" size={13} />
                        </button>
                      </Card.Footer>
                    </Card>
                  ))
                ) : (
                  <Empty icon="grid" title="暂无关联任务" />
                )}
              </>
            )}
          </Card>
        </div>
      </div>
      {editor && (
        <FeishuSourceEditor
          source={editor === "new" ? null : editor}
          context={sourceContext}
          busy={busy}
          writable={editor !== "new" || sourceWritable}
          error={formError}
          onClose={() => setEditor(null)}
          onSave={saveSource}
        />
      )}
      {triageSettingsOpen && (
        <TaskTriageSettingsModal
          includesFeishu
          onClose={() => setTriageSettingsOpen(false)}
          onSaved={() => setNotice("任务判断模型已保存。")}
        />
      )}
    </div>
  );
}

function FeishuSourceHealth({ source }: { source: FeishuSource }) {
  return (
    <Chip
      className="github-health"
      size="sm"
      variant="soft"
      color={healthColors[source.health]}
    >
      <i aria-hidden="true" />
      <Chip.Label>{feishuHealthLabel(source.health)}</Chip.Label>
    </Chip>
  );
}

function FeishuSetupHint() {
  return (
    <p className="github-note">
      请将已配置的飞书机器人加入目标群，在飞书开放平台订阅
      <code> im.message.receive_v1 </code> 并选择长连接接收。
    </p>
  );
}

export function FeishuEventCard({
  value,
  onOpenTicket,
}: {
  value: FeishuEvent;
  onOpenTicket: (id: string) => void;
}) {
  const ticketId = value.ticket_id;
  return (
    <Card<"article">
      render={(props) => <article {...props} />}
      className="github-event"
    >
      <Card.Header className="github-event-heading">
        <Chip
          size="sm"
          variant="soft"
          color={value.disposition === "accepted" ? "success" : "default"}
        >
          {feishuEventLabel(value.disposition)}
        </Chip>
        <time>{dateLabel(value.received_at)}</time>
      </Card.Header>
      <Card.Content>
        <p className="github-event-byline">
          {value.sender_id ? <code>{value.sender_id}</code> : "发送人未知"}
        </p>
        {value.reason && (
          <p className="github-event-byline">
            {feishuEventReason(value.reason)}
          </p>
        )}
        {value.text && (
          <details>
            <summary>查看消息正文</summary>
            <blockquote>{value.text}</blockquote>
          </details>
        )}
        {value.triage && <TaskTriageDecisionCard value={value.triage} />}
      </Card.Content>
      {ticketId && (
        <Card.Footer className="github-event-links">
          <button
            className="text-button"
            onClick={() => onOpenTicket(ticketId)}
          >
            查看关联任务 <Icon name="chevron" size={13} />
          </button>
        </Card.Footer>
      )}
    </Card>
  );
}

function FeishuSourceEditor({
  source,
  context,
  busy,
  writable,
  error,
  onClose,
  onSave,
}: {
  source: FeishuSource | null;
  context: SourceContext;
  busy: boolean;
  writable: boolean;
  error: string;
  onClose: () => void;
  onSave: (chatId: string, fields: SourceFields) => void;
}) {
  const [form, setForm] = useState<FeishuSourceForm>({
    name: source ? sourceName(source) : "",
    allowed_senders: source?.allowed_senders.join("\n") ?? "",
    required_keywords: source?.required_keywords.join("\n") ?? "",
  });
  const [chatId, setChatId] = useState(source?.chat_id ?? "");
  const [discovery, setDiscovery] = useState<FeishuDiscovery | null>(null);
  const [discoveryError, setDiscoveryError] = useState("");
  const [validationError, setValidationError] = useState("");
  const [reading, setReading] = useState(false);
  const discoverControl = useRef<AbortController | null>(null);
  useEffect(() => () => discoverControl.current?.abort(), []);

  const discover = async () => {
    if (discoverControl.current) return;
    const control = new AbortController();
    discoverControl.current = control;
    setReading(true);
    setDiscovery(null);
    setDiscoveryError("");
    try {
      const result = await request<FeishuDiscovery>("/feishu/discover", {
        method: "POST",
        body: "{}",
        signal: control.signal,
      });
      if (control.signal.aborted) return;
      if (
        !result?.identity?.app_id ||
        !result.identity.bot_open_id ||
        !Array.isArray(result.groups) ||
        result.groups.some(
          (group) => !group?.chat_id || typeof group.name !== "string",
        )
      )
        throw new Error("飞书未返回可确认的机器人和群聊信息。");
      if (
        source &&
        (result.identity.app_id !== source.app_id ||
          result.identity.bot_open_id !== source.bot_open_id)
      )
        throw new Error(
          "当前机器人的身份与此来源不同，请检查本地飞书 CLI 配置。",
        );
      setDiscovery(result);
      if (!source && !result.groups.some((group) => group.chat_id === chatId))
        setChatId("");
    } catch (failure) {
      if (!control.signal.aborted) setDiscoveryError(readableError(failure));
    } finally {
      discoverControl.current = null;
      if (!control.signal.aborted) setReading(false);
    }
  };

  const userId = discovery?.identity.user_open_id;
  const hasUser =
    !!userId && form.allowed_senders.split(/[\s,，]+/).includes(userId);
  const selectedGroup = discovery?.groups.find(
    (group) => group.chat_id === chatId,
  );
  const submit = () => {
    if (busy || !writable || (!source && !selectedGroup)) return;
    setValidationError("");
    try {
      const fields = feishuSourceFields(form, source?.enabled ?? false);
      if (source && !fields.name) throw new Error("请填写来源名称。");
      onSave(chatId, fields);
    } catch (failure) {
      setValidationError(readableError(failure));
    }
  };
  return (
    <Modal
      title={source ? "配置飞书来源" : "添加飞书来源"}
      wide
      onClose={() => {
        if (!busy) onClose();
      }}
    >
      <form
        onSubmit={(event) => {
          event.preventDefault();
          submit();
        }}
      >
        {source ? (
          <p className="github-note">
            群聊 {source.chat_id} · 机器人{" "}
            {source.bot_name || source.bot_open_id}
          </p>
        ) : (
          <>
            <p className="github-note">
              来源对话 · {context.workspace} · {context.agent} ·{" "}
              {context.conversation}
            </p>
            <FeishuSetupHint />
          </>
        )}
        <Button
          className="github-action"
          variant="outline"
          size="sm"
          type="button"
          isDisabled={busy || reading || !writable}
          onPress={() => void discover()}
        >
          {reading ? <Spinner /> : <Icon name="refresh" size={15} />}
          {reading
            ? "正在读取飞书…"
            : source
              ? "查询当前用户 OpenID"
              : "读取机器人所在群聊"}
        </Button>
        {discoveryError && (
          <p className="inline-error" role="alert">
            {discoveryError}
          </p>
        )}
        {discovery && (
          <p className="github-note">
            机器人{" "}
            {discovery.identity.bot_name || discovery.identity.bot_open_id} ·
            应用 {discovery.identity.app_id}
            <br />
            {userId
              ? `当前用户 ${userId}`
              : "当前 CLI 未提供已登录用户的 OpenID。"}
          </p>
        )}
        {!source && (
          <label className="field">
            飞书群聊
            <select
              aria-label="飞书群聊"
              value={chatId}
              disabled={busy || !discovery?.groups.length || !writable}
              onChange={(event) => setChatId(event.target.value)}
            >
              <option value="">
                {discovery && !discovery.groups.length
                  ? "机器人尚未加入群聊"
                  : "请选择机器人所在群聊"}
              </option>
              {discovery?.groups.map((group) => (
                <option value={group.chat_id} key={group.chat_id}>
                  {group.name || group.chat_id}
                </option>
              ))}
            </select>
            {selectedGroup && <small>{selectedGroup.chat_id}</small>}
          </label>
        )}
        <label className="field">
          {source ? "来源名称" : "来源名称（可选）"}
          <input
            value={form.name}
            disabled={busy || !writable}
            placeholder={selectedGroup?.name || "默认使用群名称"}
            onChange={(event) => setForm({ ...form, name: event.target.value })}
          />
        </label>
        <label className="field">
          允许的发送人 OpenID
          <textarea
            aria-label="允许的发送人 OpenID"
            aria-describedby="feishu-senders-help"
            rows={3}
            value={form.allowed_senders}
            disabled={busy || !writable}
            onChange={(event) =>
              setForm({ ...form, allowed_senders: event.target.value })
            }
          />
          <small id="feishu-senders-help">
            填写以 ou_ 开头的用户 OpenID，每行一位。启用来源前至少添加一位。
          </small>
        </label>
        {userId && (
          <Button
            type="button"
            className="github-action"
            variant="outline"
            size="sm"
            isDisabled={busy || hasUser || !writable}
            onPress={() =>
              setForm((current) => ({
                ...current,
                allowed_senders: [current.allowed_senders.trim(), userId]
                  .filter(Boolean)
                  .join("\n"),
              }))
            }
          >
            {hasUser ? "已添加当前用户" : "添加当前用户"}
          </Button>
        )}
        <label className="field">
          消息关键词（可选）
          <textarea
            aria-label="消息关键词（可选）"
            aria-describedby="feishu-keywords-help"
            rows={2}
            value={form.required_keywords}
            disabled={busy || !writable}
            onChange={(event) =>
              setForm({ ...form, required_keywords: event.target.value })
            }
          />
          <small id="feishu-keywords-help">
            每行一个关键词。设置后，消息须包含其中任一关键词。
          </small>
        </label>
        {(validationError || error) && (
          <p className="inline-error" role="alert">
            {validationError || error}
          </p>
        )}
        {!source && (
          <p className="github-note">
            来源保存后处于暂停状态。启用后可接收群聊消息。
          </p>
        )}
        <div className="modal-actions">
          <Button
            type="button"
            className="github-action"
            variant="outline"
            size="sm"
            isDisabled={busy}
            onPress={onClose}
          >
            取消
          </Button>
          <Button
            type="submit"
            className="github-action"
            variant="primary"
            size="sm"
            isDisabled={busy || !writable || (!source && !selectedGroup)}
          >
            {busy ? "正在保存…" : "保存来源"}
          </Button>
        </div>
      </form>
    </Modal>
  );
}
