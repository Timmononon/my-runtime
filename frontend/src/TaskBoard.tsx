import {
  Alert,
  Button,
  Card,
  SearchField,
  ToggleButton,
  ToggleButtonGroup,
} from "@heroui/react";
import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, post, readableError, request } from "./api";
import { Icon } from "./icons";
import { dateLabel, Empty, Markdown, Modal, Spinner, StatusChip } from "./ui";
import { LatestRequest } from "./state";
import PendingCard from "./PendingCard";
import type { ManagedAgent, Pending } from "./types";
import type {
  TaskCandidate,
  TaskMode,
  TaskOverview,
  TaskRule,
  TaskSettings,
  TaskTicket,
  WorkerDraft,
} from "./taskTypes";
import {
  orderTickets,
  taskActions,
  taskLabel,
  taskNeedsAttention,
  validateRules,
  workerPayload,
} from "./taskTypes";
import "./taskBoard.css";
import { githubDeliveryLabel } from "./githubTypes";

const enc = encodeURIComponent;
const emptyOverview: TaskOverview = {
  counts: { total: 0, active: 0, attention: 0, completed: 0 },
  tickets: [],
};
const taskStats = [
  { key: "attention", icon: "info", label: "需要你处理" },
  { key: "active", icon: "bolt", label: "进行中" },
  { key: "completed", icon: "check", label: "已完成" },
  { key: "total", icon: "grid", label: "全部任务" },
] as const;
const taskFilters = [
  { id: "all", label: "全部" },
  { id: "attention", label: "需要处理" },
  { id: "active", label: "进行中" },
  { id: "completed", label: "已完成" },
] as const;
type TaskFilter = (typeof taskFilters)[number]["id"];
const workerDraft = (): WorkerDraft => ({
  title: "",
  goal: "",
  agent_id: "",
  role: "worker",
});
type Props = {
  selected: string | null;
  sourceWritable: boolean;
  ensureSession: () => Promise<string>;
  onOpenSession: (id: string) => void;
  agents: ManagedAgent[];
  initialTicketId?: string | null;
  onOpenGithub?: (sourceId: string) => void;
  onOpenFeishu?: (sourceId: string) => void;
};

export default function TaskBoard({
  selected,
  sourceWritable,
  ensureSession,
  onOpenSession,
  agents,
  initialTicketId = null,
  onOpenGithub,
  onOpenFeishu,
}: Props) {
  const [overview, setOverview] = useState<TaskOverview>(emptyOverview),
    [loading, setLoading] = useState(true);
  const [selectedId, setSelectedId] = useState<string | null>(initialTicketId),
    [ticket, setTicket] = useState<TaskTicket | null>(null);
  const [detailLoading, setDetailLoading] = useState(false),
    [error, setError] = useState("");
  const [filter, setFilter] = useState<TaskFilter>("all"),
    [search, setSearch] = useState(""),
    [busy, setBusy] = useState(false),
    [pendingBusy, setPendingBusy] = useState<string | null>(null);
  const [creating, setCreating] = useState(false),
    [title, setTitle] = useState(""),
    [goal, setGoal] = useState("");
  const [workers, setWorkers] = useState<WorkerDraft[]>([workerDraft()]),
    [formError, setFormError] = useState("");
  const [policyOpen, setPolicyOpen] = useState(false),
    [settings, setSettings] = useState<TaskSettings | null>(null);
  const [mode, setMode] = useState<TaskMode>("observe"),
    [rules, setRules] = useState<TaskRule[]>([]),
    [policyError, setPolicyError] = useState("");
  const [candidateText, setCandidateText] = useState(""),
    [candidate, setCandidate] = useState<TaskCandidate | null>(null);
  const [candidates, setCandidates] = useState<TaskCandidate[]>([]),
    [followup, setFollowup] = useState("");
  const [confirmCancel, setConfirmCancel] = useState(false);
  const overviewGate = useRef(new LatestRequest()),
    detailGate = useRef(new LatestRequest()),
    policyGate = useRef(new LatestRequest());
  const selectionRef = useRef(selectedId),
    mounted = useRef(true),
    mutation = useRef(false);
  const createRequest = useRef<string | null>(null),
    inputRequest = useRef<{ text: string; id: string } | null>(null);
  const candidateRequest = useRef<{
    text: string;
    source: string;
    id: string;
  } | null>(null);
  selectionRef.current = selectedId;

  const loadOverview = useCallback(async (signal?: AbortSignal) => {
    const epoch = overviewGate.current.begin();
    try {
      const result = await request<TaskOverview>("/tasks/overview", { signal });
      if (overviewGate.current.current(epoch) && mounted.current) {
        setOverview(result);
        setLoading(false);
      }
    } catch (failure) {
      if (
        overviewGate.current.current(epoch) &&
        !signal?.aborted &&
        mounted.current
      ) {
        setError(readableError(failure));
        setLoading(false);
      }
    }
  }, []);
  const loadDetail = useCallback(async (id: string, signal?: AbortSignal) => {
    const epoch = detailGate.current.begin();
    try {
      const result = await request<TaskTicket>(`/tasks/${enc(id)}`, { signal });
      if (
        detailGate.current.current(epoch) &&
        selectionRef.current === id &&
        mounted.current
      ) {
        setTicket(result);
        setDetailLoading(false);
      }
    } catch (failure) {
      if (
        detailGate.current.current(epoch) &&
        !signal?.aborted &&
        mounted.current
      ) {
        setError(readableError(failure));
        setDetailLoading(false);
      }
    }
  }, []);
  useEffect(() => {
    mounted.current = true;
    const control = new AbortController();
    void loadOverview(control.signal);
    let running = false;
    const timer = window.setInterval(async () => {
      if (running || mutation.current || document.hidden) return;
      running = true;
      try {
        await loadOverview(control.signal);
      } finally {
        running = false;
      }
    }, 2000);
    const policyEpoch = policyGate.current.begin();
    void request<TaskSettings>("/tasks/settings", { signal: control.signal })
      .then((value) => {
        if (
          mounted.current &&
          !control.signal.aborted &&
          policyGate.current.current(policyEpoch)
        )
          setSettings(value);
      })
      .catch(() => {});
    return () => {
      mounted.current = false;
      control.abort();
      window.clearInterval(timer);
      overviewGate.current.invalidate();
      detailGate.current.invalidate();
      policyGate.current.invalidate();
    };
  }, [loadOverview]);
  useEffect(() => {
    setFollowup("");
    inputRequest.current = null;
    setConfirmCancel(false);
    if (!selectedId) {
      setTicket(null);
      return;
    }
    setTicket(null);
    setDetailLoading(true);
    const control = new AbortController();
    void loadDetail(selectedId, control.signal);
    let running = false;
    const timer = window.setInterval(async () => {
      if (running || mutation.current || document.hidden) return;
      running = true;
      try {
        await loadDetail(selectedId, control.signal);
      } finally {
        running = false;
      }
    }, 2000);
    return () => {
      control.abort();
      window.clearInterval(timer);
      detailGate.current.invalidate();
    };
  }, [selectedId, loadDetail]);

  const selectTicket = (id: string) => {
    selectionRef.current = id;
    setSelectedId(id);
    setError("");
  };
  const refresh = async () => {
    await loadOverview();
    if (selectionRef.current) await loadDetail(selectionRef.current);
  };
  const runMutation = async (
    work: () => Promise<void>,
    failure: (text: string) => void = setError,
  ) => {
    if (mutation.current) return;
    mutation.current = true;
    setBusy(true);
    overviewGate.current.invalidate();
    detailGate.current.invalidate();
    try {
      await work();
      if (mounted.current) await refresh();
    } catch (problem) {
      if (mounted.current) {
        failure(readableError(problem));
        if (problem instanceof ApiError && problem.status === 409)
          await refresh();
      }
    } finally {
      mutation.current = false;
      if (mounted.current) setBusy(false);
    }
  };
  const openCreate = () => {
    setCreating(true);
    setFormError("");
    createRequest.current = null;
  };
  const create = () =>
    runMutation(async () => {
      setFormError("");
      if (!title.trim() || !goal.trim())
        throw new Error("请填写任务名称和总目标。");
      const payload = workerPayload(workers),
        sourceSession = await ensureSession();
      createRequest.current ||= crypto.randomUUID();
      const created = await post<TaskTicket>("/tasks", {
        source_session_id: sourceSession,
        title: title.trim(),
        goal: goal.trim(),
        workers: payload,
        request_id: createRequest.current,
        unattended: false,
      });
      if (!mounted.current) return;
      setCreating(false);
      setTitle("");
      setGoal("");
      setWorkers([workerDraft()]);
      createRequest.current = null;
      selectTicket(created.id);
    }, setFormError);
  const updateWorker = (index: number, changes: Partial<WorkerDraft>) => {
    createRequest.current = null;
    setWorkers((current) =>
      current.map((worker, at) =>
        at === index ? { ...worker, ...changes } : worker,
      ),
    );
  };
  const ticketAction = (action: "cancel" | "retry") => {
    if (!ticket) return;
    const current = ticket;
    void runMutation(async () => {
      setError("");
      // Runtime transitions may advance a revision between polling and the
      // person's confirmation. Re-read that same task, and retry only a known
      // rejected version conflict; transport/unknown outcomes are not replayed.
      let latest = await request<TaskTicket>(`/tasks/${enc(current.id)}`);
      let result: TaskTicket | null = null;
      for (let attempt = 0; attempt < 2; attempt++) {
        if (!taskActions(latest)[action]) {
          if (action === "cancel" && latest.state === "cancelled") {
            result = latest;
            break;
          }
          throw new Error("任务状态已更新，请检查最新结果后再操作。");
        }
        try {
          result = await post<TaskTicket>(
            `/tasks/${enc(current.id)}/${action}`,
            { expected_revision: latest.revision },
          );
          break;
        } catch (problem) {
          if (
            !(problem instanceof ApiError) ||
            problem.status !== 409 ||
            attempt > 0
          )
            throw problem;
          latest = await request<TaskTicket>(`/tasks/${enc(current.id)}`);
        }
      }
      if (!result) throw new Error("任务状态尚未稳定，请重新读取后操作。");
      if (selectionRef.current === result.id && mounted.current)
        setTicket(result);
      setConfirmCancel(false);
    });
  };
  const sendInput = () => {
    if (!ticket || !followup.trim()) return;
    const current = ticket,
      text = followup.trim();
    if (inputRequest.current?.text !== text)
      inputRequest.current = { text, id: crypto.randomUUID() };
    const requestId = inputRequest.current.id;
    void runMutation(async () => {
      setError("");
      const result = await post<TaskTicket>(
        `/tasks/${enc(current.id)}/inputs`,
        { text, request_id: requestId, expected_revision: current.revision },
      );
      if (selectionRef.current === result.id && mounted.current) {
        setTicket(result);
        setFollowup("");
        inputRequest.current = null;
      }
    });
  };
  const resolvePending = async (pending: Pending, answer: string | boolean) => {
    setPendingBusy(pending.id);
    await runMutation(async () => {
      const approval = pending.kind === "approval";
      const result = await post<{ accepted: boolean }>(
        `/${approval ? "approvals" : "questions"}/${enc(pending.id)}/${approval ? "decision" : "answer"}`,
        approval ? { approved: answer } : { answer },
      );
      if (!result.accepted)
        throw new Error("此请求已处理或已过期，已重新读取任务状态。");
    });
    setPendingBusy(null);
  };
  const loadPolicy = async () => {
    const epoch = policyGate.current.begin();
    setPolicyError("");
    try {
      const [configuration, history] = await Promise.allSettled([
        request<TaskSettings>("/tasks/settings"),
        request<{ candidates: TaskCandidate[] }>("/tasks/candidates"),
      ]);
      if (!policyGate.current.current(epoch) || !mounted.current) return;
      if (configuration.status === "rejected") throw configuration.reason;
      const value = configuration.value;
      setSettings(value);
      setMode(value.mode);
      setRules(
        value.rules.map((rule) => ({ ...rule, contains: [...rule.contains] })),
      );
      if (history.status === "fulfilled")
        setCandidates(history.value.candidates);
      else
        setPolicyError(
          "候选记录暂时无法读取。接单规则已读取，可稍后重新读取记录。",
        );
    } catch (failure) {
      if (policyGate.current.current(epoch) && mounted.current)
        setPolicyError(readableError(failure));
    }
  };
  const openPolicy = () => {
    setPolicyOpen(true);
    setSettings(null);
    setCandidate(null);
    void loadPolicy();
  };
  const savePolicy = () =>
    runMutation(async () => {
      if (!settings) return;
      const value = await request<TaskSettings>("/tasks/settings", {
        method: "PATCH",
        body: JSON.stringify({
          expected_revision: settings.revision,
          mode,
          rules: validateRules(rules),
        }),
      });
      setSettings(value);
      candidateRequest.current = null;
      setPolicyOpen(false);
    }, setPolicyError);
  const evaluate = () =>
    runMutation(async () => {
      if (!candidateText.trim()) return;
      const source = await ensureSession(),
        text = candidateText.trim();
      if (
        candidateRequest.current?.text !== text ||
        candidateRequest.current.source !== source
      )
        candidateRequest.current = { text, source, id: crypto.randomUUID() };
      const result = await post<TaskCandidate>("/tasks/candidates", {
        source_session_id: source,
        text,
        request_id: candidateRequest.current.id,
        source: "local",
      });
      setCandidate(result);
      const history = await request<{ candidates: TaskCandidate[] }>(
        "/tasks/candidates",
      );
      setCandidates(history.candidates);
    }, setPolicyError);
  const query = search.trim().toLocaleLowerCase();
  const ordered = orderTickets(overview.tickets)
    .filter((item) =>
      filter === "attention"
        ? taskNeedsAttention(item)
        : filter === "active"
          ? ["queued", "working", "waiting"].includes(item.state)
          : filter === "completed"
            ? item.state === "completed"
            : true,
    )
    .filter(
      (item) =>
        !query ||
        item.title.toLocaleLowerCase().includes(query) ||
        item.goal.toLocaleLowerCase().includes(query) ||
        item.workers.some((worker) =>
          worker.title.toLocaleLowerCase().includes(query),
        ),
    );
  const actions = ticket ? taskActions(ticket) : null;

  return (
    <div className="task-board-scroll">
      <div className="task-board">
        <header className="task-board-heading">
          <div className="task-heading-copy">
            <span className="task-heading-icon">
              <Icon name="grid" size={24} />
            </span>
            <div>
              <h1>让每项工作都有着落。</h1>
              <p>分派工作、跟进结果，需要你处理的事项会排在前面。</p>
            </div>
          </div>
          <div className="task-heading-actions">
            <Button variant="outline" onPress={openPolicy}>
              <Icon name="shield" size={16} />
              接单规则
            </Button>
            <Button onPress={openCreate} isDisabled={!sourceWritable}>
              <Icon name="plus" size={16} />
              创建任务
            </Button>
          </div>
        </header>
        {!sourceWritable && (
          <Alert className="task-notice" status="accent">
            <Alert.Indicator />
            <Alert.Content>
              <Alert.Description>
                当前对话仅供查看。切换到可用对话后可创建任务。
              </Alert.Description>
            </Alert.Content>
          </Alert>
        )}
        {error && (
          <Alert className="task-error" status="danger" role="alert">
            <Alert.Indicator />
            <Alert.Content>
              <Alert.Description>{error}</Alert.Description>
            </Alert.Content>
            <Button
              variant="danger-soft"
              size="sm"
              onPress={() => {
                setError("");
                void refresh();
              }}
            >
              重新读取
            </Button>
          </Alert>
        )}
        <div className="task-stats" aria-label="任务概览">
          {taskStats.map(({ key, icon, label }) => (
            <Card key={key} className="task-stat" data-count={key}>
              <Card.Content className="task-stat-content">
                <span className="task-stat-icon">
                  <Icon name={icon} size={17} />
                </span>
                <strong>{overview.counts[key]}</strong>
                <span className="task-stat-label">{label}</span>
              </Card.Content>
            </Card>
          ))}
        </div>
        <div className="task-toolbar">
          <SearchField
            className="task-search"
            aria-label="搜索任务"
            fullWidth
            value={search}
            onChange={setSearch}
          >
            <SearchField.Group>
              <SearchField.SearchIcon />
              <SearchField.Input placeholder="搜索任务名称、目标或 Worker" />
              <SearchField.ClearButton aria-label="清除任务搜索" />
            </SearchField.Group>
          </SearchField>
          <ToggleButtonGroup
            className="task-filters"
            aria-label="筛选任务"
            selectionMode="single"
            disallowEmptySelection
            isDetached
            size="sm"
            selectedKeys={[filter]}
            onSelectionChange={(keys) => {
              const next = taskFilters.find(({ id }) => keys.has(id));
              if (next) setFilter(next.id);
            }}
          >
            {taskFilters.map(({ id, label }) => (
              <ToggleButton key={id} id={id} variant="ghost">
                {label}
              </ToggleButton>
            ))}
          </ToggleButtonGroup>
          <Button
            className="task-refresh"
            isIconOnly
            size="sm"
            variant="outline"
            aria-label="刷新任务"
            onPress={() => void refresh()}
            isDisabled={busy}
          >
            <Icon name="refresh" size={16} />
          </Button>
        </div>
        <div className={`task-columns ${selectedId ? "has-detail" : ""}`}>
          <section
            className="task-list"
            aria-label="任务列表"
            aria-busy={loading}
          >
            {loading ? (
              <div className="task-loading" role="status">
                <Spinner inline={false} />
                正在读取任务
              </div>
            ) : !ordered.length ? (
              <Empty
                icon="grid"
                title={
                  overview.counts.total
                    ? "没有符合条件的任务"
                    : "开始一项有明确目标的工作"
                }
                detail={
                  overview.counts.total
                    ? "试试其他关键词或筛选条件。"
                    : "创建任务，并把可以独立进行的工作分给不同 Worker。"
                }
              />
            ) : (
              ordered.map((item) => (
                <Card<"button">
                  key={item.id}
                  render={(props) => <button {...props} />}
                  type="button"
                  className={`task-row ${selectedId === item.id ? "selected" : ""}`}
                  aria-pressed={selectedId === item.id}
                  onClick={() => selectTicket(item.id)}
                >
                  <Card.Header<"span">
                    className="task-row-head"
                    render={(props) => <span {...props} />}
                  >
                    <TaskStatus state={item.state} />
                    <span>{dateLabel(item.updated_at)}</span>
                  </Card.Header>
                  <Card.Content<"span">
                    className="task-row-content"
                    render={(props) => <span {...props} />}
                  >
                    <Card.Title<"span">
                      className="task-row-title"
                      render={(props) => <span {...props} />}
                    >
                      {item.title}
                    </Card.Title>
                    <Card.Description<"span">
                      className="task-row-summary"
                      render={(props) => <span {...props} />}
                    >
                      {item.attention || item.goal}
                    </Card.Description>
                  </Card.Content>
                  <Card.Footer<"span">
                    className="task-row-foot"
                    render={(props) => <span {...props} />}
                  >
                    <span>
                      {item.workers.length} 个 Worker ·{" "}
                      {
                        item.workers.filter(
                          (worker) => worker.state === "completed",
                        ).length
                      }{" "}
                      个完成
                    </span>
                    <Icon name="chevron" size={15} />
                  </Card.Footer>
                </Card>
              ))
            )}
          </section>
          {selectedId && (
            <Card<"section">
              render={(props) => <section {...props} />}
              className="task-detail"
              aria-label="任务详情"
              aria-busy={detailLoading}
            >
              <Card.Header className="task-detail-top">
                <span className="eyebrow">任务详情</span>
                <Button
                  isIconOnly
                  size="sm"
                  variant="ghost"
                  aria-label="关闭任务详情"
                  onPress={() => setSelectedId(null)}
                >
                  <Icon name="close" size={16} />
                </Button>
              </Card.Header>
              {detailLoading ? (
                <div className="task-loading" role="status">
                  <Spinner inline={false} />
                  正在读取执行情况
                </div>
              ) : (
                ticket && (
                  <>
                    <div className="task-detail-title">
                      <TaskStatus state={ticket.state} />
                      <Card.Title<"h2"> render={(props) => <h2 {...props} />}>
                        {ticket.title}
                      </Card.Title>
                      <Card.Description>{ticket.goal}</Card.Description>
                    </div>
                    {ticket.github && (
                      <div className="github-task-origin">
                        <strong>GitHub · {ticket.github.repository}</strong>
                        <div>
                          <span>
                            {ticket.github.numbers
                              .map((number) => `#${number}`)
                              .join("、")}
                          </span>
                          <span>
                            {githubDeliveryLabel(ticket.github.delivery_state)}
                          </span>
                        </div>
                        {onOpenGithub && (
                          <button
                            className="text-button"
                            onClick={() =>
                              onOpenGithub(ticket.github!.source_id)
                            }
                          >
                            打开仓库来源
                            <Icon name="chevron" size={14} />
                          </button>
                        )}
                      </div>
                    )}
                    {ticket.feishu && (
                      <div className="github-task-origin">
                        <strong>
                          飞书 · {ticket.feishu.name || ticket.feishu.chat_id}
                        </strong>
                        {onOpenFeishu && (
                          <button
                            className="text-button"
                            onClick={() =>
                              onOpenFeishu(ticket.feishu!.source_id)
                            }
                          >
                            打开飞书来源
                            <Icon name="chevron" size={14} />
                          </button>
                        )}
                      </div>
                    )}
                    {ticket.attention && (
                      <Alert className="task-notice" status="warning">
                        <Alert.Indicator />
                        <Alert.Content>
                          <Alert.Description>
                            {ticket.attention}
                          </Alert.Description>
                        </Alert.Content>
                      </Alert>
                    )}
                    <div className="task-detail-actions">
                      <Button
                        variant="outline"
                        size="sm"
                        onPress={() => onOpenSession(ticket.source_session_id)}
                      >
                        <Icon name="chat" size={15} />
                        来源对话
                      </Button>
                      {actions?.retry && (
                        <Button
                          variant="outline"
                          size="sm"
                          isDisabled={busy}
                          onPress={() => ticketAction("retry")}
                        >
                          <Icon name="refresh" size={15} />
                          重试未完成的工作
                        </Button>
                      )}
                      {actions?.cancel && (
                        <Button
                          variant="danger-soft"
                          size="sm"
                          isDisabled={busy}
                          onPress={() => setConfirmCancel(true)}
                        >
                          <Icon name="stop" size={15} />
                          取消任务
                        </Button>
                      )}
                    </div>
                    {ticket.state === "needs_reconciliation" && (
                      <Alert className="task-notice" status="warning">
                        <Alert.Indicator />
                        <Alert.Content>
                          <Alert.Description>
                            先在工作对话中核验结果并登记证据，再按 Runtime
                            的恢复流程继续。
                          </Alert.Description>
                        </Alert.Content>
                      </Alert>
                    )}
                    {!!ticket.summary && (
                      <section className="task-result">
                        <h3>
                          {ticket.state === "completed"
                            ? "结果汇总"
                            : "当前结果"}
                        </h3>
                        <Markdown>{ticket.summary}</Markdown>
                      </section>
                    )}
                    <div className="task-workers">
                      <h3>工作分配</h3>
                      {ticket.workers.map((worker) => (
                        <Card<"article">
                          render={(props) => <article {...props} />}
                          className="task-worker"
                          key={worker.id}
                        >
                          <Card.Header className="task-worker-heading">
                            <Card.Title<"h4">
                              render={(props) => <h4 {...props} />}
                            >
                              {worker.title}
                            </Card.Title>
                            <TaskStatus state={worker.state} />
                          </Card.Header>
                          <Card.Description>{worker.goal}</Card.Description>
                          <small>
                            {worker.role === "reader"
                              ? "只读审查"
                              : "执行 Worker"}{" "}
                            · {worker.agent_id}
                          </small>
                          {worker.cwd && (
                            <details className="task-worker-directory">
                              <summary>工作目录</summary>
                              <code>{worker.cwd}</code>
                            </details>
                          )}
                          {worker.error && (
                            <p className="task-worker-error">{worker.error}</p>
                          )}
                          {worker.answer && (
                            <details
                              className="task-worker-result"
                              open={worker.state === "failed" || undefined}
                            >
                              <summary>查看工作结果</summary>
                              <Markdown>{worker.answer}</Markdown>
                            </details>
                          )}
                          {worker.pending_requests?.map((pending) => (
                            <PendingCard
                              key={pending.id}
                              pending={pending}
                              busy={busy || pendingBusy === pending.id}
                              submit={(answer) =>
                                resolvePending(pending, answer)
                              }
                            />
                          ))}
                          {worker.session_id && (
                            <button
                              className="text-button"
                              onClick={() => onOpenSession(worker.session_id!)}
                            >
                              打开工作对话
                              <Icon name="external" size={13} />
                            </button>
                          )}
                        </Card>
                      ))}
                    </div>
                    {actions?.input && (
                      <form
                        className="task-followup"
                        onSubmit={(event) => {
                          event.preventDefault();
                          sendInput();
                        }}
                      >
                        <label className="field">
                          <span>补充任务要求</span>
                          <textarea
                            rows={3}
                            value={followup}
                            onChange={(event) =>
                              setFollowup(event.target.value)
                            }
                            placeholder="补充原任务范围内的要求或核验结果…"
                            disabled={busy}
                          />
                        </label>
                        <Button
                          variant="outline"
                          type="submit"
                          isDisabled={!followup.trim() || busy}
                        >
                          发送补充
                          <Icon name="arrow" size={14} />
                        </Button>
                      </form>
                    )}
                    <details className="task-audit">
                      <summary>任务记录</summary>
                      <dl>
                        <dt>接单策略</dt>
                        <dd>第 {ticket.policy_revision} 版</dd>
                        <dt>创建时间</dt>
                        <dd>{dateLabel(ticket.created_at)}</dd>
                        <dt>任务标识</dt>
                        <dd>{ticket.id}</dd>
                      </dl>
                    </details>
                  </>
                )
              )}
            </Card>
          )}
        </div>
        {creating && (
          <Modal
            title="创建任务"
            wide
            onClose={() => {
              if (!busy) setCreating(false);
            }}
          >
            <form
              onSubmit={(event) => {
                event.preventDefault();
                void create();
              }}
            >
              <p className="modal-description">
                填写总目标，再分配可以独立进行的工作。各 Worker
                使用自己的会话和工作目录，沿用来源对话的权限。
              </p>
              <label className="field">
                <span>任务名称</span>
                <input
                  value={title}
                  onChange={(event) => {
                    setTitle(event.target.value);
                    createRequest.current = null;
                  }}
                  maxLength={200}
                  required
                  disabled={busy}
                />
              </label>
              <label className="field">
                <span>总目标与交付要求</span>
                <textarea
                  rows={3}
                  value={goal}
                  onChange={(event) => {
                    setGoal(event.target.value);
                    createRequest.current = null;
                  }}
                  required
                  disabled={busy}
                />
              </label>
              <div className="task-form-workers">
                {workers.map((worker, index) => (
                  <fieldset key={index}>
                    <legend>Worker {index + 1}</legend>
                    <div className="task-worker-form-heading">
                      <label className="field">
                        <span>工作名称</span>
                        <input
                          value={worker.title}
                          onChange={(event) =>
                            updateWorker(index, { title: event.target.value })
                          }
                          required
                          disabled={busy}
                          maxLength={200}
                        />
                      </label>
                      {workers.length > 1 && (
                        <button
                          type="button"
                          className="icon-button"
                          aria-label={`移除 Worker ${index + 1}`}
                          disabled={busy}
                          onClick={() => {
                            setWorkers((current) =>
                              current.filter((_, at) => at !== index),
                            );
                            createRequest.current = null;
                          }}
                        >
                          <Icon name="trash" size={16} />
                        </button>
                      )}
                    </div>
                    <label className="field">
                      <span>独立工作目标</span>
                      <textarea
                        rows={2}
                        value={worker.goal}
                        onChange={(event) =>
                          updateWorker(index, { goal: event.target.value })
                        }
                        required
                        disabled={busy}
                      />
                    </label>
                    <div className="task-form-two">
                      <label className="field">
                        <span>Agent</span>
                        <select
                          value={worker.agent_id}
                          onChange={(event) =>
                            updateWorker(index, {
                              agent_id: event.target.value,
                            })
                          }
                          disabled={busy}
                        >
                          <option value="">沿用来源 Agent</option>
                          {agents.map((agent) => (
                            <option key={agent.id} value={agent.id}>
                              {agent.name || agent.id}
                            </option>
                          ))}
                        </select>
                      </label>
                      <label className="field">
                        <span>角色</span>
                        <select
                          value={worker.role}
                          onChange={(event) =>
                            updateWorker(index, {
                              role: event.target.value as WorkerDraft["role"],
                            })
                          }
                          disabled={busy}
                        >
                          <option value="worker">执行 Worker</option>
                          <option value="reader">只读审查</option>
                        </select>
                      </label>
                    </div>
                  </fieldset>
                ))}
              </div>
              <button
                type="button"
                className="text-button"
                disabled={
                  busy || workers.length >= (settings?.max_workers || 8)
                }
                onClick={() => {
                  setWorkers((current) => [...current, workerDraft()]);
                  createRequest.current = null;
                }}
              >
                <Icon name="plus" size={15} />
                增加 Worker
              </button>
              {formError && (
                <p className="inline-error" role="alert">
                  {formError}
                </p>
              )}
              <div className="modal-actions">
                <Button
                  variant="outline"
                  type="button"
                  isDisabled={busy}
                  onPress={() => setCreating(false)}
                >
                  取消
                </Button>
                <Button type="submit" isDisabled={busy}>
                  {busy ? "正在创建…" : "创建并开始"}
                </Button>
              </div>
            </form>
          </Modal>
        )}
        {policyOpen && (
          <Modal
            title="接单规则"
            wide
            onClose={() => {
              if (!busy) {
                setPolicyOpen(false);
                policyGate.current.invalidate();
              }
            }}
          >
            <p className="modal-description">
              规则决定本地主动候选是否成为任务。你明确创建的任务继续按原权限执行。
            </p>
            {!settings ? (
              <div className="task-loading">
                {policyError ? (
                  <Button variant="outline" onPress={() => void loadPolicy()}>
                    重试读取规则
                  </Button>
                ) : (
                  <>
                    <Spinner inline={false} />
                    正在读取规则
                  </>
                )}
              </div>
            ) : (
              <>
                <label className="field">
                  <span>主动接单方式</span>
                  <select
                    value={mode}
                    onChange={(event) =>
                      setMode(event.target.value as TaskMode)
                    }
                    disabled={busy}
                  >
                    <option value="off">关闭：不接受主动候选</option>
                    <option value="observe">
                      观察：记录判断，不启动主动任务
                    </option>
                    <option value="auto">自动：匹配已启用规则后创建任务</option>
                  </select>
                </label>
                <p className="task-policy-caption">
                  同一规则中的匹配词需要全部出现；任意一条已启用规则匹配即可。自动任务仍遵守无人值守权限。
                </p>
                <div className="task-policy-rules">
                  {rules.map((rule, index) => (
                    <fieldset key={index}>
                      <legend>规则 {index + 1}</legend>
                      <div className="task-policy-rule-head">
                        <label className="task-checkbox">
                          <input
                            type="checkbox"
                            checked={rule.enabled}
                            disabled={busy}
                            onChange={(event) =>
                              setRules((current) =>
                                current.map((item, at) =>
                                  at === index
                                    ? { ...item, enabled: event.target.checked }
                                    : item,
                                ),
                              )
                            }
                          />
                          启用
                        </label>
                        <button
                          className="icon-button"
                          aria-label={`移除规则 ${index + 1}`}
                          disabled={busy}
                          onClick={() =>
                            setRules((current) =>
                              current.filter((_, at) => at !== index),
                            )
                          }
                        >
                          <Icon name="trash" size={16} />
                        </button>
                      </div>
                      <label className="field">
                        <span>规则标识</span>
                        <input
                          value={rule.id}
                          disabled={busy}
                          onChange={(event) =>
                            setRules((current) =>
                              current.map((item, at) =>
                                at === index
                                  ? { ...item, id: event.target.value }
                                  : item,
                              ),
                            )
                          }
                        />
                      </label>
                      <label className="field">
                        <span>匹配词（每行一个）</span>
                        <textarea
                          rows={2}
                          value={rule.contains.join("\n")}
                          disabled={busy}
                          onChange={(event) =>
                            setRules((current) =>
                              current.map((item, at) =>
                                at === index
                                  ? {
                                      ...item,
                                      contains: event.target.value.split("\n"),
                                    }
                                  : item,
                              ),
                            )
                          }
                        />
                      </label>
                    </fieldset>
                  ))}
                </div>
                <button
                  className="text-button"
                  disabled={busy}
                  onClick={() =>
                    setRules((current) => [
                      ...current,
                      {
                        id: `rule-${crypto.randomUUID().slice(0, 8)}`,
                        enabled: true,
                        contains: [""],
                      },
                    ])
                  }
                >
                  <Icon name="plus" size={15} />
                  增加规则
                </button>
                <div className="modal-actions">
                  <Button
                    variant="outline"
                    isDisabled={busy}
                    onPress={() => void loadPolicy()}
                  >
                    重新读取已保存规则
                  </Button>
                  <Button isDisabled={busy} onPress={() => void savePolicy()}>
                    保存规则
                  </Button>
                </div>
                <details className="task-candidate-test">
                  <summary>评估本地候选</summary>
                  <p>
                    使用已保存规则评估文本。自动模式匹配后会创建任务并使用模型执行。
                  </p>
                  <label className="field">
                    <span>候选文本</span>
                    <textarea
                      rows={3}
                      value={candidateText}
                      onChange={(event) => {
                        setCandidateText(event.target.value);
                        setCandidate(null);
                      }}
                      disabled={busy}
                    />
                  </label>
                  <Button
                    variant="outline"
                    isDisabled={
                      busy || !candidateText.trim() || !sourceWritable
                    }
                    onPress={() => void evaluate()}
                  >
                    按已保存规则评估
                  </Button>
                  {candidate && (
                    <div className="task-candidate-result" role="status">
                      <strong>
                        {candidate.decision === "accepted"
                          ? "已接单"
                          : candidate.decision === "observed"
                            ? "已记录观察结果"
                            : "未接单"}
                      </strong>
                      <p>{candidate.reason}</p>
                      {candidate.ticket_id && (
                        <button
                          className="text-button"
                          onClick={() => {
                            selectTicket(candidate.ticket_id!);
                            setPolicyOpen(false);
                          }}
                        >
                          查看任务
                          <Icon name="chevron" size={14} />
                        </button>
                      )}
                    </div>
                  )}
                </details>
                <details className="task-candidate-history">
                  <summary>最近候选记录（{candidates.length}）</summary>
                  {!candidates.length ? (
                    <p>还没有本地主动候选。</p>
                  ) : (
                    candidates.map((item) => (
                      <article key={item.id}>
                        <strong>
                          {item.decision === "accepted"
                            ? "已接单"
                            : item.decision === "observed"
                              ? "观察"
                              : "忽略"}
                        </strong>
                        <p>{item.reason}</p>
                        {item.text && <blockquote>{item.text}</blockquote>}
                        {item.ticket_id && (
                          <button
                            className="text-button"
                            onClick={() => {
                              selectTicket(item.ticket_id!);
                              setPolicyOpen(false);
                            }}
                          >
                            查看任务
                          </button>
                        )}
                      </article>
                    ))
                  )}
                </details>
              </>
            )}
            {policyError && (
              <p className="inline-error" role="alert">
                {policyError}
              </p>
            )}
          </Modal>
        )}
        {confirmCancel && ticket && (
          <Modal
            title="取消任务"
            onClose={() => {
              if (!busy) setConfirmCancel(false);
            }}
          >
            <p>
              取消“{ticket.title}”及其尚未完成的
              Worker。已经执行的操作会保留结果和核验记录。
            </p>
            {error && (
              <p className="inline-error" role="alert">
                {error}
              </p>
            )}
            <div className="modal-actions">
              <Button
                variant="outline"
                isDisabled={busy}
                onPress={() => setConfirmCancel(false)}
              >
                继续执行
              </Button>
              <Button
                variant="danger-soft"
                isDisabled={busy}
                onPress={() => ticketAction("cancel")}
              >
                确认取消
              </Button>
            </div>
          </Modal>
        )}
      </div>
    </div>
  );
}

export function TaskStatus({ state }: { state: string }) {
  return (
    <StatusChip
      className={`task-status task-state-${state}`}
      status={state}
      label={taskLabel(state)}
    />
  );
}
