import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  lazy,
  Suspense,
} from "react";
import type { FormEvent, KeyboardEvent } from "react";
import {
  ApiError,
  connect,
  download,
  post,
  readableError,
  request,
  streamRun,
} from "./api";
import { Icon, Star } from "./icons";
import {
  activeStatus,
  dateLabel,
  Empty,
  Markdown,
  Modal,
  Popup,
  sizeLabel,
  Spinner,
  Status,
} from "./ui";
import type {
  Artifact,
  Bootstrap,
  Lineage,
  Mail,
  MessageRecord,
  Performance,
  Run,
  Session,
  SessionDetail,
  ModelProvider,
  ProviderProtocol,
  RunSelection,
  Project,
  Skill,
} from "./types";
import Management from "./Management";
import Conversation from "./Conversation";
import PendingCard from "./PendingCard";
import SideChatPanel from "./SideChatPanel";
import { readSideChats } from "./sideChats";
const ProviderSettings = lazy(() => import("./ProviderSettings"));
import ExecutionTimeline from "./ExecutionTimeline";
import ContextMeter from "./ContextMeter";
import type { ManagedAgent, RuntimeManagement } from "./types";
import Projects from "./Projects";
import ArchivedSessions from "./ArchivedSessions";
import {
  SessionActions,
  SessionReadonlyNotice,
  WorkspaceSessionRows,
  sessionState,
} from "./SessionLifecycle";
import type { SessionAction } from "./SessionLifecycle";
import { readSessionSnapshot } from "./sessionSnapshot";
import { findEmptyProjectSession } from "./projectSession";
import { workspaceSessions } from "./workspaceTree";
import { ComposerMenu, ModelOptions, SkillPicker } from "./ComposerOptions";
export { branchBoundaries } from "./state";
import {
  LatestRequest,
  StreamCache,
  SubmissionLedger,
  branchBoundaries,
} from "./state";

const enc = encodeURIComponent;

export default function App() {
  const [boot, setBoot] = useState<Bootstrap | null>(null),
    [connecting, setConnecting] = useState(true),
    [connectError, setConnectError] = useState("");
  const [sessions, setSessions] = useState<Session[]>([]),
    [selected, setSelected] = useState<string | null>(null),
    [detail, setDetail] = useState<SessionDetail | null>(null);
  const [sessionsLoading, setSessionsLoading] = useState(false),
    [sessionsMore, setSessionsMore] = useState(false),
    [sessionsError, setSessionsError] = useState(""),
    [lifecycleTarget, setLifecycleTarget] = useState<string | null>(null),
    [lifecycleError, setLifecycleError] = useState(""),
    [deletingSession, setDeletingSession] = useState<Session | null>(null),
    [archiveRevision, setArchiveRevision] = useState(0),
    [creatingProjectSession, setCreatingProjectSession] = useState<
      string | null
    >(null);
  const lifecycleFlight = useRef<string | null>(null),
    projectSessionFlight = useRef(false),
    sessionOffset = useRef(0),
    sessionListBusy = useRef(false),
    sessionListGate = useRef(new LatestRequest()),
    writableRef = useRef(true);
  const [run, setRun] = useState<Run | null>(null),
    [stream, setStream] = useState(""),
    [streamState, setStreamState] = useState(""),
    [phase, setPhase] = useState("");
  const [lineage, setLineage] = useState<Lineage | null>(null),
    [mail, setMail] = useState<Mail[]>([]),
    [deliverables, setDeliverables] = useState<Artifact[]>([]),
    [attached, setAttached] = useState<Artifact[]>([]);
  const [drafts, setDrafts] = useState<Record<string, string>>({}),
    [draftFiles, setDraftFiles] = useState<Record<string, Artifact[]>>({});
  const [view, setView] = useState("chat"),
    [sidebar, setSidebar] = useState(false),
    [search, setSearch] = useState(""),
    [searchOpen, setSearchOpen] = useState(false);
  const [right, setRight] = useState<"files" | "activity" | "side-chat" | null>(
      null,
    ),
    [performance, setPerformance] = useState<Performance | null>(null),
    [performanceRevision, setPerformanceRevision] = useState(0);
  const performanceFor = useRef<string | null>(null);
  // Mounted side chats retain their own drafts, in-flight requests and streams
  // when the panel is tucked away or the main conversation changes.
  const [sideChats, setSideChats] = useState<Session[]>([]),
    [sideCatalog, setSideCatalog] = useState<Session[]>([]),
    [sideFor, setSideFor] = useState<Record<string, string>>({}),
    [sideCreating, setSideCreating] = useState(false),
    [identityLoading, setIdentityLoading] = useState(false);
  const sideCreation = useRef(false),
    workspaceNavigation = useRef(0);
  const [error, setError] = useState(""),
    [loading, setLoading] = useState(false),
    [sending, setSending] = useState(false),
    [uploading, setUploading] = useState(false),
    [dragging, setDragging] = useState(false);
  const [settings, setSettings] = useState(false),
    [profileMenu, setProfileMenu] = useState(false),
    [renameOpen, setRenameOpen] = useState(false),
    [sessionTitle, setSessionTitle] = useState(""),
    [newWorkspace, setNewWorkspace] = useState(""),
    [newAgent, setNewAgent] = useState("default"),
    [newProject, setNewProject] = useState("");
  const [projects, setProjects] = useState<Project[]>([]),
    [agents, setAgents] = useState<ManagedAgent[]>([]),
    [providers, setProviders] = useState<ModelProvider[]>([]),
    [protocols, setProtocols] = useState<ProviderProtocol[]>([]),
    [selection, setSelection] = useState<RunSelection>({
      provider_id: "",
      model: "",
      reasoning_effort: null,
      selected_skill: null,
    }),
    [skillPicker, setSkillPicker] = useState(false),
    [eligibleSkills, setEligibleSkills] = useState<Skill[]>([]),
    [skillsLoading, setSkillsLoading] = useState(false),
    [compactNotice, setCompactNotice] = useState(""),
    [compactionSession, setCompactionSession] = useState<string | null>(null),
    [compactionCancelling, setCompactionCancelling] = useState(false);
  const compactionFlight = useRef<{
    sessionId: string;
    previousRunId: string | null;
    runId: string | null;
  } | null>(null);
  const [steer, setSteer] = useState(false),
    [branch, setBranch] = useState<{ index: number } | null>(null),
    [branchTitle, setBranchTitle] = useState("");
  const [actionBusy, setActionBusy] = useState(false),
    [scrolledUp, setScrolledUp] = useState(false);
  const selectedRef = useRef<string | null>(null),
    runRef = useRef<Run | null>(null),
    creation = useRef<Promise<string> | null>(null),
    streamCache = useRef(new StreamCache()),
    refreshGate = useRef(new LatestRequest()),
    navigation = useRef(0),
    submissions = useRef(new SubmissionLedger()),
    submitting = useRef(false),
    refreshFlights = useRef(new Map<string, Promise<void>>()),
    refreshDirty = useRef(new Set<string>());
  const selectionCache = useRef<Record<string, RunSelection>>({}),
    newSelectionExplicit = useRef(false),
    selectionRef = useRef(selection),
    newProjectRef = useRef(newProject),
    draftsRef = useRef(drafts),
    draftFilesRef = useRef(draftFiles),
    skillsRequest = useRef(0);
  selectionRef.current = selection;
  newProjectRef.current = newProject;
  draftsRef.current = drafts;
  draftFilesRef.current = draftFiles;
  const conversation = useRef<HTMLDivElement>(null),
    conversationContent = useRef<HTMLDivElement>(null),
    scrolledUpRef = useRef(false),
    lastConversationTop = useRef(0),
    textarea = useRef<HTMLTextAreaElement>(null),
    fileInput = useRef<HTMLInputElement>(null);
  const markScrolledUp = useCallback((value: boolean) => {
    scrolledUpRef.current = value;
    setScrolledUp(value);
  }, []);
  const scrollConversationBottom = useCallback(() => {
    const element = conversation.current;
    if (!element) return;
    element.scrollTo({ top: element.scrollHeight, behavior: "instant" });
    lastConversationTop.current = element.scrollTop;
  }, []);
  const newDraftKey = newProject ? `new:${newProject}` : "new",
    key = selected || newDraftKey,
    draft = drafts[key] || "",
    chosenFiles = draftFiles[key] || [],
    busy = activeStatus(run?.status),
    compactionActive = !!selected && compactionSession === selected,
    compactionRunReady =
      compactionActive &&
      busy &&
      run?.session_id === selected &&
      run?.kind === "compaction" &&
      run?.id === compactionFlight.current?.runId,
    closed = !!lineage?.closed_at,
    readOnly = sessionState(detail) !== "active",
    writeLocked =
      readOnly ||
      identityLoading ||
      loading ||
      (!!selected && (!detail || lifecycleTarget === selected)),
    lifecycleBlocked =
      busy ||
      sending ||
      uploading ||
      actionBusy ||
      compactionActive ||
      loading ||
      !!lifecycleTarget;
  writableRef.current = !writeLocked;
  const chosenProvider = providers.find((p) => p.id === selection.provider_id),
    chosenModel = chosenProvider?.models.find((m) => m.id === selection.model),
    providerReady = !!chosenProvider?.configured && !!chosenModel,
    currentProject = projects.find(
      (project) => project.id === (detail ? detail.project_id : newProject),
    );
  const assistantName = (id: string) => {
    const name = agents.find((agent) => agent.id === id)?.name;
    return id === "default" && (!name || name === "default")
      ? "默认助手"
      : name || id;
  };
  const currentSession =
      detail?.id === selected
        ? detail
        : sessions.find((session) => session.id === selected),
    currentAssistant = currentSession?.agent_id
      ? assistantName(currentSession.agent_id)
      : "助手加载中…";
  const updateSelection = (value: RunSelection) => {
    if (!writableRef.current) return;
    const target = selectedRef.current || "new";
    if (target === "new") newSelectionExplicit.current = true;
    selectionCache.current[target] = { ...value };
    selectionRef.current = value;
    setSelection(value);
  };
  selectedRef.current = selected;
  runRef.current = run;
  useEffect(() => {
    if (renameOpen) setSessionTitle(detail?.title || "");
  }, [renameOpen]);
  const setDraft = (value: string) =>
    setDrafts((old) => ({ ...old, [key]: value }));
  const report = useCallback((e: unknown) => {
    if (e instanceof ApiError && e.status === 401) {
      setBoot(null);
      setConnectError(readableError(e));
    } else setError(readableError(e));
  }, []);
  const refreshSessions = useCallback(
    async (append = false) => {
      if (append && sessionListBusy.current) return;
      const generation = sessionListGate.current.begin();
      const offset = append ? sessionOffset.current : 0;
      const valid = () => sessionListGate.current.current(generation);
      sessionListBusy.current = true;
      setSessionsLoading(true);
      setSessionsError("");
      try {
        // The server filters side chats before pagination. Neither existing
        // side chats nor newly created ones belong in the project sidebar.
        const result = await request<{ sessions: Session[] }>(
          `/sessions?include_side_chats=false&state=active&limit=100&offset=${offset}`,
        );
        if (!valid()) return;
        setSessions((old) => {
          const unique = new Map<string, Session>();
          for (const item of [...(append ? old : []), ...result.sessions])
            unique.set(item.id, {
              ...item,
              lineage:
                item.lineage ||
                old.find((saved) => saved.id === item.id)?.lineage,
            });
          return [...unique.values()];
        });
        sessionOffset.current = offset + result.sessions.length;
        setSessionsMore(result.sessions.length === 100);
      } catch (error) {
        if (valid()) {
          setSessionsError(readableError(error));
          report(error);
        }
      } finally {
        if (valid()) {
          sessionListBusy.current = false;
          setSessionsLoading(false);
        }
      }
    },
    [report],
  );
  const startConnection = useCallback(async () => {
    setConnecting(true);
    setConnectError("");
    try {
      const value = await connect();
      setBoot(value);
      setNewWorkspace(value.cwd);
      const profiles = value.providers
        ? { providers: value.providers, protocols: value.protocols || [] }
        : await request<{
            providers: ModelProvider[];
            protocols: ProviderProtocol[];
          }>("/providers");
      setProviders(profiles.providers);
      setProtocols(profiles.protocols);
      const management = await request<RuntimeManagement>("/management");
      setAgents(management.agents);
      const initial = value.selection || {
        provider_id: profiles.providers[0]?.id || "",
        model: value.model,
        reasoning_effort: null,
        selected_skill: null,
      };
      selectionCache.current.new = initial;
      selectionRef.current = initial;
      setSelection(initial);
      const projectData = await request<{ projects: Project[] }>(
        "/workbench/projects",
      );
      setProjects(projectData.projects);
    } catch (e) {
      setConnectError(readableError(e));
    } finally {
      setConnecting(false);
    }
  }, []);
  useEffect(() => {
    void startConnection();
  }, [startConnection]);
  useEffect(() => {
    if (boot) void refreshSessions();
    else {
      sessionListGate.current.invalidate();
      sessionListBusy.current = false;
      setSessionsLoading(false);
    }
  }, [boot, refreshSessions]);
  const refreshSnapshot = useCallback(async (id: string) => {
    if (selectedRef.current !== id) return;
    const flightKey = `${navigation.current}:${id}`;
    const generation = refreshGate.current.begin();
    const valid = () =>
      selectedRef.current === id && refreshGate.current.current(generation);
    const snapshot = await readSessionSnapshot(
      () => request<SessionDetail>(`/sessions/${enc(id)}`),
      (runId) => request<Run>(`/runs/${enc(runId)}`),
      valid,
    );
    if (!snapshot || !valid()) return;
    if ("retry" in snapshot) {
      refreshDirty.current.add(flightKey);
      return;
    }
    const value = snapshot.detail;
    if (!selectionCache.current[id] && value.selection) {
      selectionCache.current[id] = value.selection;
      selectionRef.current = value.selection;
      setSelection(value.selection);
    }
    if (snapshot.run) {
      const live = snapshot.run;
      if (valid()) {
        const compaction = compactionFlight.current;
        if (
          compaction &&
          compaction.sessionId === id &&
          !compaction.runId &&
          live.kind === "compaction" &&
          live.id !== compaction.previousRunId
        ) {
          compaction.runId = live.id;
        }
        setDetail(value);
        runRef.current = live;
        setRun(live);
        streamCache.current.synchronize(
          live.id,
          value.messages,
          !activeStatus(live.status),
        );
        setStream(streamCache.current.get(live.id).text);
      }
    } else {
      setDetail(value);
      runRef.current = null;
      setRun(null);
    }
    const result = await Promise.allSettled([
      request<{ messages: Mail[] }>(`/sessions/${enc(id)}/mailbox`),
      request<{ deliverables: Artifact[] }>(
        `/sessions/${enc(id)}/deliverables`,
      ),
      request<{ attachments: Artifact[] }>(`/sessions/${enc(id)}/attachments`),
      request<{ lineage: Lineage | null }>(`/sessions/${enc(id)}/lineage`),
    ]);
    if (!valid()) return;
    if (result[0].status === "fulfilled") setMail(result[0].value.messages);
    if (result[1].status === "fulfilled")
      setDeliverables(result[1].value.deliverables);
    if (result[2].status === "fulfilled")
      setAttached(result[2].value.attachments);
    if (result[3].status === "fulfilled") setLineage(result[3].value.lineage);
  }, []);
  const refresh: (id: string) => Promise<void> = useCallback(
    (id: string) => {
      if (selectedRef.current !== id) return Promise.resolve();
      const flightKey = `${navigation.current}:${id}`;
      const existing = refreshFlights.current.get(flightKey);
      if (existing) {
        refreshDirty.current.add(flightKey);
        return existing;
      }
      const promise = refreshSnapshot(id).finally(() => {
        refreshFlights.current.delete(flightKey);
        if (
          refreshDirty.current.delete(flightKey) &&
          selectedRef.current === id &&
          flightKey === `${navigation.current}:${id}`
        )
          queueMicrotask(() => {
            void refresh(id).catch(report);
          });
      });
      refreshFlights.current.set(flightKey, promise);
      return promise;
    },
    [refreshSnapshot, report],
  );
  const openSession = useCallback(
    async (id: string) => {
      navigation.current++;
      refreshGate.current.invalidate();
      selectedRef.current = id;
      setSelected(id);
      setRenameOpen(false);
      setBranch(null);
      setDeletingSession(null);
      setSkillPicker(false);
      skillsRequest.current++;
      if (selectionCache.current[id]) {
        selectionRef.current = selectionCache.current[id];
        setSelection(selectionCache.current[id]);
      }
      setView("chat");
      markScrolledUp(false);
      setSidebar(false);
      setError("");
      setCompactNotice("");
      setStream("");
      setRun(null);
      setDetail(null);
      setMail([]);
      setAttached([]);
      setDeliverables([]);
      setLineage(null);
      setLoading(true);
      try {
        await refresh(id);
      } catch (e) {
        if (selectedRef.current === id) report(e);
      } finally {
        if (selectedRef.current === id) setLoading(false);
      }
    },
    [refresh, report, markScrolledUp],
  );
  const ensureSession = useCallback(async () => {
    if (selectedRef.current) {
      if (
        !writableRef.current ||
        lifecycleFlight.current === selectedRef.current
      )
        throw new Error(
          "当前对话仅供查看或正在更新，请先取消归档或开启新对话。",
        );
      const sid = selectedRef.current;
      if (!selectionCache.current[sid]) {
        const value = await request<RunSelection>(
          `/sessions/${enc(sid)}/selection`,
        );
        selectionCache.current[sid] = value;
        if (selectedRef.current === sid) {
          selectionRef.current = value;
          setSelection(value);
        }
      }
      return sid;
    }
    if (creation.current) return creation.current;
    const startedAt = navigation.current;
    const sourceKey = newProject ? `new:${newProject}` : "new";
    const explicit = newSelectionExplicit.current;
    const requestedSelection = {
      ...(selectionCache.current.new || selectionRef.current),
    };
    creation.current = (async () => {
      let session: Session | null | undefined = null;
      if (newProject) {
        const project = projects.find((item) => item.id === newProject);
        if (!project) throw new Error("项目已更新，请重新选择项目。");
        session = await findEmptyProjectSession({
          project,
          agent: newAgent,
          preferred: null,
          // Navigation does not cancel an explicit send/upload. Finish the
          // captured scope, but only select it if this view is still current.
          valid: () => true,
          blocked: (id) =>
            !!draftsRef.current[id] || !!draftFilesRef.current[id]?.length,
          listPage: async (offset) =>
            (
              await request<{ sessions: Session[] }>(
                `/sessions?state=active&limit=100&offset=${offset}`,
              )
            ).sessions,
          readSession: async (id) => {
            const [value, files, messages, ancestry, automation] =
              await Promise.all([
                request<SessionDetail>(`/sessions/${enc(id)}`),
                request<{ attachments: Artifact[] }>(
                  `/sessions/${enc(id)}/attachments`,
                ),
                request<{ messages: Mail[] }>(`/sessions/${enc(id)}/mailbox`),
                request<{ lineage: Lineage | null }>(
                  `/sessions/${enc(id)}/lineage`,
                ),
                request<{ jobs: unknown[] }>(
                  `/automations?session_id=${enc(id)}`,
                ),
              ]);
            return {
              detail: value,
              hasAttachments: !!files.attachments.length,
              hasMailbox: !!messages.messages.length,
              hasLineage: ancestry.lineage !== null,
              hasAutomation: !!automation.jobs.length,
            };
          },
        });
      }
      session ||= await post<Session>("/sessions", {
        ...(newProject ? {} : { cwd: newWorkspace || boot?.cwd }),
        agent_id: newAgent || "default",
        project_id: newProject || null,
      });
      if (explicit) selectionCache.current[session.id] = requestedSelection;
      if (navigation.current === startedAt && selectedRef.current === null) {
        selectedRef.current = session.id;
        setSelected(session.id);
        const id = session.id;
        setDrafts((old) => ({
          ...old,
          [id]: old[sourceKey] || "",
          [sourceKey]: "",
        }));
        setDraftFiles((old) => ({
          ...old,
          [id]: old[sourceKey] || [],
          [sourceKey]: [],
        }));
        setDetail({ ...session, messages: [], runs: [], plan: null });
      }
      const created = session;
      setSessions((old) => [
        created,
        ...old.filter((item) => item.id !== created.id),
      ]);
      if (!explicit) {
        const value = await request<RunSelection>(
          `/sessions/${enc(session.id)}/selection`,
        );
        selectionCache.current[session.id] = value;
        if (
          navigation.current === startedAt &&
          selectedRef.current === session.id
        ) {
          selectionRef.current = value;
          setSelection(value);
        }
      }
      return session.id;
    })();
    try {
      return await creation.current;
    } finally {
      creation.current = null;
    }
  }, [boot, newWorkspace, newAgent, newProject, projects]);
  useEffect(() => {
    if (!boot || !selected || (!busy && !compactionActive)) return;
    if (compactionActive) void refresh(selected).catch(report);
    const timer = setInterval(
      () => {
        void refresh(selected).catch(report);
      },
      compactionActive ? 1000 : 1800,
    );
    return () => clearInterval(timer);
  }, [boot, selected, busy, compactionActive, refresh, report]);
  useEffect(() => {
    if (!boot || !run?.id || !busy || !selected) return;
    const rid = run.id,
      sid = selected,
      controller = new AbortController();
    let mounted = true;
    const follow = async () => {
      let failures = 0;
      while (mounted && !controller.signal.aborted) {
        try {
          setStreamState("");
          await streamRun(
            rid,
            streamCache.current.get(rid).cursor,
            (event) => {
              if (
                !mounted ||
                controller.signal.aborted ||
                selectedRef.current !== sid ||
                runRef.current?.id !== rid
              )
                return;
              if (!streamCache.current.accept(rid, event)) return;
              setStream(streamCache.current.get(rid).text);
              if (event.type === "run.phase")
                setPhase(String(event.payload.phase || ""));
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
                void refresh(sid).catch(report);
            },
            controller.signal,
          );
          if (!mounted || selectedRef.current !== sid) return;
          await refresh(sid);
          void refreshSessions().catch(report);
          if (
            runRef.current?.id !== rid ||
            !activeStatus(runRef.current?.status)
          )
            break;
        } catch (e) {
          if (controller.signal.aborted) return;
          if (e instanceof ApiError && e.status === 401) {
            report(e);
            return;
          }
          failures++;
          setStreamState("连接暂时中断，正在从已保存的位置恢复…");
          if (failures > 5) {
            setStreamState("实时连接暂不可用，正在定时同步执行状态。");
            return;
          }
        }
        await new Promise((resolve) =>
          setTimeout(resolve, Math.min(4000, 800 * (failures + 1))),
        );
      }
    };
    void follow();
    return () => {
      mounted = false;
      controller.abort();
    };
  }, [boot, run?.id, busy, selected, refresh, refreshSessions, report]);
  useEffect(() => {
    let active = true;
    if (right === "activity" && run) {
      if (performanceFor.current !== run.id) setPerformance(null);
      void request<Performance>(`/runs/${enc(run.id)}/performance`)
        .then((value) => {
          if (
            active &&
            selectedRef.current === run.session_id &&
            runRef.current?.id === run.id
          ) {
            performanceFor.current = run.id;
            setPerformance(value);
          }
        })
        .catch((error) => {
          if (active) report(error);
        });
    }
    return () => {
      active = false;
    };
  }, [
    right,
    run?.id,
    run?.status,
    run?.budget?.model_requests,
    run?.tool_executions?.length,
    performanceRevision,
    report,
  ]);
  useLayoutEffect(() => {
    if (!boot || view !== "chat" || loading) return;
    const element = conversation.current,
      content = conversationContent.current;
    if (!element || !content) return;
    // History is rendered only after loading ends. Reentering this reading
    // surface starts at the latest message, including a same-length session.
    markScrolledUp(false);
    scrollConversationBottom();
    if (typeof ResizeObserver === "undefined") return;
    let active = true;
    const observer = new ResizeObserver(() => {
      if (active && selectedRef.current === selected && !scrolledUpRef.current)
        scrollConversationBottom();
    });
    observer.observe(content);
    observer.observe(element);
    return () => {
      active = false;
      observer.disconnect();
    };
  }, [
    boot?.instance_id,
    selected,
    view,
    loading,
    markScrolledUp,
    scrollConversationBottom,
  ]);
  useLayoutEffect(() => {
    if (view === "chat" && !loading && !scrolledUpRef.current)
      scrollConversationBottom();
  }, [
    detail?.id,
    detail?.messages.length,
    stream,
    run?.pending_requests?.length,
    selected,
    view,
    loading,
    scrolledUp,
    scrollConversationBottom,
  ]);
  useEffect(() => {
    if (textarea.current) {
      textarea.current.style.height = "auto";
      textarea.current.style.height =
        Math.min(160, textarea.current.scrollHeight) + "px";
    }
  }, [draft]);
  const newChat = () => {
    workspaceNavigation.current++;
    setIdentityLoading(false);
    const projectId = detail ? detail.project_id : newProjectRef.current;
    const project = projects.find((item) => item.id === projectId);
    setNewProject(project?.id || "");
    setNewWorkspace(project?.cwd || boot?.cwd || "");
    navigation.current++;
    refreshGate.current.invalidate();
    selectedRef.current = null;
    setSelected(null);
    setRenameOpen(false);
    setBranch(null);
    setDeletingSession(null);
    setSkillPicker(false);
    skillsRequest.current++;
    setLoading(false);
    const next = selectionCache.current.new || selectionRef.current;
    selectionRef.current = next;
    setSelection(next);
    setDetail(null);
    setRun(null);
    setStream("");
    setMail([]);
    setLineage(null);
    setAttached([]);
    setDeliverables([]);
    setView("chat");
    setSidebar(false);
    setError("");
    setCompactNotice("");
    setRight(null);
    setScrol(false);
  };
  const setScrol = (value: boolean) => markScrolledUp(value);
  async function submit(e?: FormEvent) {
    e?.preventDefault();
    if (
      (!draft.trim() && !chosenFiles.length) ||
      sending ||
      uploading ||
      loading ||
      submitting.current ||
      compactionActive ||
      writeLocked ||
      closed ||
      (!busy && !providerReady) ||
      run?.status === "needs_reconciliation"
    )
      return;
    const text = draft,
      files = [...chosenFiles],
      startedAt = navigation.current,
      startedKey = key,
      wasNew = !selectedRef.current,
      explicitChoice = newSelectionExplicit.current,
      runSelection = { ...selectionRef.current };
    // An explicit send resumes following the conversation. Background tokens
    // still respect deliberate history browsing; smooth scrolling would emit
    // intermediate positions that look like such a user scroll.
    markScrolledUp(false);
    scrollConversationBottom();
    submitting.current = true;
    setSending(true);
    setError("");
    try {
      const sid = await ensureSession();
      const attempt = submissions.current.prepare(
        sid,
        text,
        files.map((f) => f.attachment_id!),
        steer,
        busy,
        wasNew && !explicitChoice ? selectionCache.current[sid] : runSelection,
      );
      const submitted = await post<Run>(attempt.endpoint, attempt.body);
      submissions.current.accepted(attempt);
      // A pre-submit snapshot may still be in flight. It must not replace the
      // accepted run with an older run from the same session.
      if (selectedRef.current === sid) refreshGate.current.invalidate();
      if (attempt.endpoint.endsWith("/runs") && selectedRef.current === sid) {
        runRef.current = submitted;
        setRun(submitted);
        setStream("");
        setPhase("setup");
      }
      setDrafts((old) => ({
        ...old,
        [sid]: old[sid] === text ? "" : old[sid],
      }));
      setDraftFiles((old) => ({ ...old, [sid]: [] }));
      if (wasNew && navigation.current === startedAt)
        setDrafts((old) => ({
          ...old,
          [startedKey]: old[startedKey] === text ? "" : old[startedKey],
        }));
      if (selectedRef.current === sid) {
        setSteer(false);
        setScrolledUp(false);
      }
      // A failed title update must not turn an accepted task into a retry.
      try {
        const session = await request<Session>(`/sessions/${enc(sid)}`);
        if (!session.title) {
          await request(`/sessions/${enc(sid)}`, {
            method: "PATCH",
            body: JSON.stringify({
              title: (text.trim() || files[0]?.filename || "新对话").slice(
                0,
                60,
              ),
            }),
          });
        }
      } catch {
        /* Task acceptance is authoritative; title can be edited later. */
      }
      await refresh(sid);
      void refreshSessions().catch(report);
    } catch (e) {
      report(e);
    } finally {
      submitting.current = false;
      setSending(false);
    }
  }
  async function upload(files: FileList | File[]) {
    if (
      !files.length ||
      closed ||
      writeLocked ||
      uploading ||
      (lifecycleFlight.current === selectedRef.current && !!selectedRef.current)
    )
      return;
    setUploading(true);
    setError("");
    try {
      const sid = await ensureSession();
      for (const file of Array.from(files)) {
        if (file.size > 25 * 1024 * 1024) {
          setError(`${file.name} 超过 25 MiB，请选择更小的文件。`);
          continue;
        }
        const artifact = await request<Artifact>(
          `/sessions/${enc(sid)}/attachments?filename=${enc(file.name)}`,
          {
            method: "POST",
            headers: {
              "Content-Type": "application/octet-stream",
            },
            body: file,
          },
        );
        setDraftFiles((old) => ({
          ...old,
          [sid]: [...(old[sid] || []), artifact].slice(0, 20),
        }));
      }
      await refresh(sid);
    } catch (e) {
      report(e);
    } finally {
      setUploading(false);
      if (fileInput.current) fileInput.current.value = "";
    }
  }
  const chooseProject = (project: Project | null) => {
    newChat();
    setNewProject(project?.id || "");
    setNewWorkspace(project?.cwd || boot?.cwd || "");
  };
  const createProjectConversation = async (project: Project) => {
    if (projectSessionFlight.current) return;
    projectSessionFlight.current = true;
    setCreatingProjectSession(project.id);
    const startedAt = navigation.current;
    try {
      if (
        !selectedRef.current &&
        !submitting.current &&
        !uploading &&
        (draftsRef.current[newDraftKey] ||
          draftFilesRef.current[newDraftKey]?.length)
      ) {
        // Starting another conversation parks an unsent draft under its own
        // verified session so it stays reachable in the sidebar. No run is sent.
        await ensureSession();
      }
      if (navigation.current !== startedAt) return;
      chooseProject(project);
    } catch (error) {
      if (navigation.current === startedAt) report(error);
    } finally {
      projectSessionFlight.current = false;
      setCreatingProjectSession(null);
    }
  };
  const projectsChanged = (items: Project[]) => {
    setProjects(items);
    if (
      newProjectRef.current &&
      !items.some((p) => p.id === newProjectRef.current)
    ) {
      setNewProject("");
      setNewWorkspace(boot?.cwd || "");
    }
  };
  const changeLifecycle = async (session: Session, action: SessionAction) => {
    if (lifecycleFlight.current) return;
    const startedAt = navigation.current;
    lifecycleFlight.current = session.id;
    setLifecycleTarget(session.id);
    setLifecycleError("");
    setError("");
    // Older list/detail responses must not overwrite a lifecycle transition.
    sessionListGate.current.invalidate();
    if (selectedRef.current === session.id) refreshGate.current.invalidate();
    try {
      let cleanupWarning = false;
      const updated =
        action === "delete"
          ? null
          : await post<Session>(`/sessions/${enc(session.id)}/${action}`);
      if (action === "delete") {
        const result = await request<{
          deleted: boolean;
          id: string;
          cleanup_warnings?: unknown[];
        }>(`/sessions/${enc(session.id)}?permanent=true`, { method: "DELETE" });
        if (result.deleted !== true || result.id !== session.id)
          throw new Error("服务未确认删除，请重新载入对话后核对。");
        cleanupWarning = !!result.cleanup_warnings?.length;
        setSideChats((old) => old.filter((item) => item.id !== session.id));
        setSideCatalog((old) => old.filter((item) => item.id !== session.id));
        if (sideFor[selectedRef.current || "new"] === session.id) {
          setRight(null);
          requestAnimationFrame(() =>
            textarea.current?.focus({ preventScroll: true }),
          );
        }
        setSideFor((old) =>
          Object.fromEntries(
            Object.entries(old).filter(([, id]) => id !== session.id),
          ),
        );
        delete selectionCache.current[session.id];
        setDrafts((old) => {
          const next = { ...old };
          delete next[session.id];
          return next;
        });
        setDraftFiles((old) => {
          const next = { ...old };
          delete next[session.id];
          return next;
        });
      }
      sessionListGate.current.invalidate();
      if (updated)
        setSideChats((old) =>
          old.map((item) =>
            item.id === updated.id ? { ...item, ...updated } : item,
          ),
        );
      // Refresh from the server's ordinary-only list after a lifecycle change;
      // unarchiving a side chat must not briefly insert it into the sidebar.
      setSessions((old) => old.filter((item) => item.id !== session.id));
      setArchiveRevision((old) => old + 1);
      setDeletingSession(null);
      if (selectedRef.current === session.id) {
        refreshGate.current.invalidate();
        if (!updated) newChat();
        else
          setDetail((old) =>
            old?.id === updated.id ? { ...old, ...updated } : old,
          );
        setRenameOpen(false);
        setBranch(null);
        setSkillPicker(false);
        skillsRequest.current++;
        // If the user left and reopened this session during the request, a
        // pending detail load may contain its previous state. Fetch it again.
        if (updated && navigation.current !== startedAt)
          void refresh(session.id).catch(report);
      }
      if (cleanupWarning) {
        const message = "对话已永久删除，但部分附件文件未能清理。";
        setError(message);
        setLifecycleError(message);
      }
    } catch (e) {
      setLifecycleError(readableError(e));
      report(
        e instanceof ApiError && e.status === 401
          ? e
          : new Error(
              `对话「${session.title || "新对话"}」操作未完成：${readableError(e)}`,
            ),
      );
    } finally {
      lifecycleFlight.current = null;
      setLifecycleTarget(null);
      // Mutations change page offsets. Refresh the currently chosen state,
      // without navigating away from another conversation opened meanwhile.
      void refreshSessions();
    }
  };
  const sessionAction = (action: SessionAction) => {
    if (!detail) return;
    actionForSession(detail, action);
  };
  const actionForSession = (session: Session, action: SessionAction) => {
    if (lifecycleFlight.current) return;
    if (action === "delete") {
      setLifecycleError("");
      setDeletingSession(session);
    } else void changeLifecycle(session, action);
  };
  const openSkills = async () => {
    if (writeLocked) return;
    const requestId = ++skillsRequest.current;
    setSkillPicker(true);
    setEligibleSkills([]);
    setSkillsLoading(true);
    try {
      const agent = detail?.agent_id || newAgent,
        project = detail ? detail.project_id : newProject;
      const value = await request<{ skills: Skill[] }>(
        `/workbench/skills?agent_id=${enc(agent)}${project ? `&project_id=${enc(project)}` : ""}`,
      );
      if (requestId === skillsRequest.current) setEligibleSkills(value.skills);
    } catch (e) {
      if (requestId === skillsRequest.current) report(e);
    } finally {
      if (requestId === skillsRequest.current) setSkillsLoading(false);
    }
  };
  const compact = async (
    method: "deterministic" | "semantic" = "deterministic",
  ) => {
    const sid = selectedRef.current;
    if (
      !sid ||
      !writableRef.current ||
      actionBusy ||
      compactionFlight.current ||
      activeStatus(runRef.current?.status)
    )
      return;
    compactionFlight.current = {
      sessionId: sid,
      previousRunId: runRef.current?.id || null,
      runId: null,
    };
    if (method === "deterministic") setActionBusy(true);
    setCompactionCancelling(false);
    setError("");
    setCompactNotice("");
    if (method === "semantic") setCompactionSession(sid);
    try {
      const value = await post<{
        changed: boolean;
        before_tokens: number;
        after_tokens: number;
        preserved_archive: boolean;
        semantic_applied?: boolean;
        cancelled?: boolean;
      }>(`/sessions/${enc(sid)}/compact`, { method });
      if (selectedRef.current === sid) {
        setCompactNotice(
          value.cancelled
            ? "上下文压缩已取消，原始对话和之前的上下文保持不变。"
            : value.changed
              ? `已${value.semantic_applied ? "通过模型摘要" : "在本地"}整理上下文，估算 ${value.before_tokens.toLocaleString()} → ${value.after_tokens.toLocaleString()} token。原始对话保留。${method === "semantic" && !value.semantic_applied ? "本次已回退到本地压缩。" : ""}`
              : "当前上下文无需进一步压缩，原始对话已保留。",
        );
      }
    } catch (error) {
      if (selectedRef.current === sid) report(error);
    } finally {
      compactionFlight.current = null;
      setCompactionSession(null);
      setCompactionCancelling(false);
      if (method === "deterministic") setActionBusy(false);
      if (selectedRef.current === sid) void refresh(sid).catch(report);
      void refreshSessions().catch(report);
    }
  };
  const cancelCompaction = async () => {
    const flight = compactionFlight.current;
    if (!flight || !flight.runId || compactionCancelling) return;
    setCompactionCancelling(true);
    try {
      const result = await post<{ accepted: boolean }>(
        `/runs/${enc(flight.runId)}/cancel`,
      );
      if (!result.accepted) {
        setCompactionCancelling(false);
        if (selectedRef.current === flight.sessionId)
          await refresh(flight.sessionId);
      }
    } catch (error) {
      setCompactionCancelling(false);
      if (selectedRef.current === flight.sessionId) report(error);
    }
  };
  const act = async (fn: () => Promise<unknown>) => {
    if (
      !writableRef.current ||
      actionBusy ||
      compactionFlight.current?.sessionId === selectedRef.current ||
      (lifecycleFlight.current === selectedRef.current && !!selectedRef.current)
    )
      return;
    setActionBusy(true);
    setError("");
    try {
      await fn();
      refreshGate.current.invalidate();
      if (selectedRef.current) await refresh(selectedRef.current);
      void refreshSessions().catch(report);
    } catch (e) {
      report(e);
    } finally {
      setActionBusy(false);
    }
  };
  async function preview(artifact: Artifact) {
    if (!selected) return;
    const tab = window.open("about:blank", "_blank");
    if (tab) tab.opener = null;
    try {
      const value = await post<{ preview_url: string }>(
        `/sessions/${enc(selected)}/previews`,
        { artifact_id: artifact.artifact_id, interactive: false },
      );
      const url = new URL(value.preview_url);
      if (url.protocol !== "http:" || url.hostname !== "127.0.0.1")
        throw new Error("预览地址无效");
      if (tab) tab.location.href = url.href;
      else setError("浏览器拦截了新窗口，请允许此站点打开预览。");
    } catch (e) {
      tab?.close();
      report(e);
    }
  }
  async function makeBranch() {
    if (!branch || !selected || writeLocked) return;
    const sourceId = selected;
    const startedAt = navigation.current;
    await act(async () => {
      const child = await post<Session>(`/sessions/${enc(sourceId)}/fork`, {
        message_index: branch.index,
        title: branchTitle || null,
      });
      if (selectedRef.current !== sourceId || navigation.current !== startedAt)
        return;
      setBranch(null);
      setBranchTitle("");
      await openSession(child.id);
      if (child.selection_warning && selectedRef.current === child.id)
        setError(
          "原会话的模型或 Skill 在此分支不可用，已恢复默认选择。请在发送前重新确认。",
        );
    });
  }
  const showSideChat = (
    session: Session,
    owner = selectedRef.current || "new",
  ) => {
    setSideCatalog((old) => [
      session,
      ...old.filter((item) => item.id !== session.id),
    ]);
    setSideChats((old) =>
      old.some((item) => item.id === session.id) ? old : [...old, session],
    );
    setSideFor((old) => ({ ...old, [owner]: session.id }));
    setRight("side-chat");
  };
  const openSideChat = async () => {
    const source = detail;
    if (!source || writeLocked || sideCreation.current) return;
    const index = Math.max(0, ...branchBoundaries(source.messages));
    const startedAt = navigation.current;
    const valid = () =>
      selectedRef.current === source.id && navigation.current === startedAt;
    sideCreation.current = true;
    setSideCreating(true);
    setError("");
    try {
      const available = await readSideChats(source.id, request, valid);
      if (!available || !valid()) return;
      setSideCatalog((old) => [
        ...available,
        ...old.filter((item) => item.lineage?.parent_id !== source.id),
      ]);
      const existing =
        available.find((item) => item.id === sideFor[source.id]) ||
        available.find((item) => !item.lineage?.closed_at);
      if (existing) {
        showSideChat(existing, source.id);
        return;
      }
      const quote =
        [...source.messages.slice(0, index)]
          .reverse()
          .find((item) => item.message.role === "user")?.message.content ||
        source.title;
      const child = await post<Session>(
        `/sessions/${enc(source.id)}/side-chat`,
        {
          message_index: index,
          title: `侧边聊天 · ${(quote || "继续讨论").trim().slice(0, 40)}`,
        },
      );
      setSideCatalog((old) => [
        child,
        ...old.filter((item) => item.id !== child.id),
      ]);
      if (selectedRef.current === source.id && navigation.current === startedAt)
        showSideChat(child, source.id);
      void refreshSessions();
    } catch (error) {
      if (selectedRef.current === source.id && navigation.current === startedAt)
        report(error);
    } finally {
      sideCreation.current = false;
      setSideCreating(false);
    }
  };
  const openWorkspaceSession = async (id: string, saved?: Session) => {
    const ticket = ++workspaceNavigation.current,
      startedAt = ++navigation.current;
    refreshGate.current.invalidate();
    writableRef.current = false;
    setIdentityLoading(true);
    try {
      // The existing list API intentionally omits lineage. Read it explicitly
      // before deciding which surface owns this saved conversation.
      const relation = await request<{ lineage: Lineage | null }>(
        `/sessions/${enc(id)}/lineage`,
      );
      if (
        ticket !== workspaceNavigation.current ||
        startedAt !== navigation.current
      )
        return;
      if (relation.lineage?.kind !== "side_chat") {
        await openSession(id);
        return;
      }
      const session = {
        ...(saved ||
          sessions.find((item) => item.id === id) ||
          (await request<Session>(`/sessions/${enc(id)}`))),
        lineage: relation.lineage,
      };
      if (
        ticket !== workspaceNavigation.current ||
        startedAt !== navigation.current
      )
        return;
      const parent = relation.lineage.parent_id;
      if (parent && selectedRef.current !== parent) {
        await openSession(parent);
        if (
          selectedRef.current !== parent ||
          ticket !== workspaceNavigation.current
        )
          return;
      }
      setView("chat");
      setSidebar(false);
      showSideChat(session);
    } catch (error) {
      if (
        ticket === workspaceNavigation.current &&
        startedAt === navigation.current
      )
        report(error);
    } finally {
      if (ticket === workspaceNavigation.current) setIdentityLoading(false);
    }
  };
  const sideOptions = sideCatalog.filter(
    (item) =>
      item.lineage?.kind === "side_chat" &&
      item.lineage.parent_id === selected &&
      sessionState(item) === "active",
  );
  const activeSideId = sideFor[selected || "new"] || sideOptions[0]?.id;
  const currentSide =
    sideCatalog.find((item) => item.id === activeSideId) ||
    sideChats.find((item) => item.id === activeSideId);
  const revealSideSource = (boundary: number) => {
    const container = conversation.current;
    const message = container?.querySelector<HTMLElement>(
      `[data-message-boundary="${boundary}"]`,
    );
    if (!container || !message) return;
    markScrolledUp(true);
    container.scrollTo({
      top:
        container.scrollTop +
        message.getBoundingClientRect().top -
        container.getBoundingClientRect().top -
        20,
      behavior: "instant",
    });
  };
  const filtered = sessions
    .filter((s) => sessionState(s) === "active")
    .filter(
      (s) =>
        (s.title || "新对话")
          .toLocaleLowerCase()
          .includes(search.toLocaleLowerCase()) ||
        s.cwd.toLocaleLowerCase().includes(search.toLocaleLowerCase()),
    );
  const boundaries = branchBoundaries(detail?.messages || []),
    lastBoundary = Math.max(0, ...boundaries);
  const hasHistory = !!detail?.messages.length;

  if (!boot)
    return (
      <div className="connect-screen">
        <div className="connect-brand">
          <Star size={38} />
          <span>Aster</span>
        </div>
        <div className="connect-content">
          <span className="eyebrow">你的本地 AGENT 工作台</span>
          <h1>让想法，在这里开始。</h1>
          <p>连接本机 Runtime，继续你的对话和工作。</p>
          {connecting ? (
            <div className="connection-loading">
              <Spinner />
              正在建立安全连接
            </div>
          ) : (
            <>
              <div className="connect-command">
                <Icon name="shield" />
                <code>aster web</code>
                <button
                  className="icon-button"
                  aria-label="复制连接命令"
                  onClick={() =>
                    void navigator.clipboard
                      .writeText("aster web")
                      .catch(() => setConnectError("请手动复制命令 aster web"))
                  }
                >
                  <Icon name="copy" size={16} />
                </button>
              </div>
              <p className="connect-hint">
                在终端运行命令，使用新打开的页面进入。
                <br />
                连接凭证只用于本机，不需要在网页输入模型密钥。
              </p>
              {connectError && (
                <p className="inline-error" role="alert">
                  {connectError}
                </p>
              )}
              <button
                className="button secondary"
                onClick={() => void startConnection()}
              >
                <Icon name="refresh" size={15} />
                重新检查连接
              </button>
            </>
          )}
        </div>
        <div className="connect-footer">本地运行 · 由你掌控</div>
      </div>
    );

  return (
    <div className="app-shell">
      {sidebar && (
        <button
          className="sidebar-scrim"
          aria-label="关闭导航"
          onClick={() => setSidebar(false)}
        />
      )}
      <aside className={`sidebar ${sidebar ? "open" : ""}`}>
        <button className="brand" onClick={newChat} aria-label="Aster 首页">
          <Star size={34} />
          <span>Aster</span>
          <span className="brand-label">WORKSPACE</span>
        </button>
        <button className="new-chat" onClick={newChat}>
          <Icon name="plus" size={19} />
          开启新对话<span>↗</span>
        </button>
        <nav aria-label="主导航">
          <button
            className={view === "chat" ? "nav-item active" : "nav-item"}
            onClick={() => {
              setView("chat");
              setSidebar(false);
            }}
          >
            <Icon name="chat" />
            对话
          </button>
          <button
            className={view === "skills" ? "nav-item active" : "nav-item"}
            onClick={() => {
              setView("skills");
              setSidebar(false);
            }}
          >
            <Icon name="skills" />
            Skills
          </button>
          <button
            className={view === "memory" ? "nav-item active" : "nav-item"}
            onClick={() => {
              setView("memory");
              setSidebar(false);
            }}
          >
            <Icon name="memory" />
            记忆
          </button>
          <button
            className={view === "automations" ? "nav-item active" : "nav-item"}
            onClick={() => {
              setView("automations");
              setSidebar(false);
            }}
          >
            <Icon name="clock" />
            自动化
          </button>
        </nav>
        <div className="workspace-browser">
          <Projects
            projects={projects}
            selected={currentProject?.id || null}
            onSelect={chooseProject}
            onChanged={projectsChanged}
            report={report}
            onNewChat={(project) => void createProjectConversation(project)}
            creatingSession={creatingProjectSession}
            renderSessions={(project) => (
              <WorkspaceSessionRows
                sessions={workspaceSessions(filtered, projects, project)}
                selected={selected}
                pending={lifecycleTarget}
                selectedBlocked={lifecycleBlocked}
                onOpen={(id) => void openWorkspaceSession(id)}
                onAction={actionForSession}
              />
            )}
            searchControl={
              <button
                className="icon-button"
                onClick={() => setSearchOpen(!searchOpen)}
                aria-label="搜索对话"
              >
                <Icon name="search" size={15} />
              </button>
            }
            searchField={
              searchOpen && (
                <div className="sidebar-search">
                  <Icon name="search" size={14} />
                  <input
                    autoFocus
                    placeholder="搜索已加载的标题或目录"
                    aria-label="搜索最近对话"
                    value={search}
                    onChange={(event) => setSearch(event.target.value)}
                  />
                </div>
              )
            }
          />
          {!filtered.length && !sessionsLoading && !sessionsError && (
            <div className="recent-empty">
              {search ? "没有找到匹配的对话" : "点击项目旁的 + 开启对话。"}
            </div>
          )}
          {sessionsLoading && (
            <div className="recent-empty" role="status">
              <Spinner /> 正在载入对话
            </div>
          )}
          {sessionsError && (
            <div className="recent-empty">
              <span>{sessionsError}</span>
              <button
                className="text-button"
                onClick={() => void refreshSessions()}
              >
                重试载入对话
              </button>
            </div>
          )}
          {sessionsMore && (
            <button
              className="load-more-sessions"
              disabled={sessionsLoading}
              onClick={() => void refreshSessions(true)}
            >
              加载更多对话
            </button>
          )}
          {search && sessionsMore && (
            <p className="lifecycle-help">
              搜索已加载的对话；可继续加载更早的记录。
            </p>
          )}
        </div>
        <div className="sidebar-footer">
          <div className="local-status">
            <span className="connection-dot" />
            <span>本机已连接</span>
            <span className="local-tag">LOCAL</span>
          </div>
          <div className="profile-anchor">
            {profileMenu && (
              <Popup
                label="个人菜单"
                className="profile-popup"
                onClose={() => setProfileMenu(false)}
              >
                <button
                  onClick={() => {
                    setProfileMenu(false);
                    setLifecycleError("");
                    setSettings(true);
                  }}
                >
                  <Icon name="settings" size={18} />
                  设置
                </button>
              </Popup>
            )}
            <button
              className="profile"
              aria-label="打开个人菜单"
              title="我的工作空间"
              aria-haspopup="dialog"
              aria-expanded={profileMenu}
              onClick={() => setProfileMenu(!profileMenu)}
            >
              <span className="profile-avatar">A</span>
              <span>
                <strong>我的工作空间</strong>
              </span>
              <Icon name="more" size={18} />
            </button>
          </div>
        </div>
      </aside>
      <main className="main-shell">
        <header className="topbar">
          <div className="topbar-start">
            <button
              className="icon-button mobile-menu"
              onClick={() => setSidebar(true)}
              aria-label="打开导航"
            >
              <Icon name="menu" />
            </button>
            <span className="breadcrumb">
              {currentProject?.name || "工作台"}
            </span>
            <span className="breadcrumb-divider">/</span>
            <button
              className="page-name"
              onClick={() => {
                if (selected && view === "chat") setRenameOpen(true);
              }}
              disabled={!selected || view !== "chat" || writeLocked}
              title="重命名当前对话"
            >
              {view === "chat"
                ? detail?.title || "新对话"
                : view === "skills"
                  ? "Skills"
                  : view === "memory"
                    ? "长期记忆"
                    : "自动化"}
            </button>
            {lineage?.kind === "side_chat" && view === "chat" && (
              <span className="small-tag">侧边聊天</span>
            )}
          </div>
          <div className="topbar-actions">
            {view === "chat" && (
              <>
                <button
                  aria-label="文件与成果"
                  className={`topbar-button ${right === "files" ? "selected" : ""}`}
                  onClick={() => setRight(right === "files" ? null : "files")}
                >
                  <Icon name="folder" size={16} />
                  <span>文件与成果</span>
                  {deliverables.length > 0 && <b>{deliverables.length}</b>}
                </button>
                <button
                  className="icon-button"
                  onClick={() =>
                    setRight(right === "activity" ? null : "activity")
                  }
                  aria-label="执行状态与性能"
                >
                  <Icon name="panel" size={17} />
                </button>
                {detail && (
                  <SessionActions
                    key={detail.id}
                    session={detail}
                    onSideChat={() => void openSideChat()}
                    sideChatDisabled={writeLocked || !!lifecycleTarget}
                    sideChatBusy={sideCreating}
                    disabled={lifecycleBlocked}
                    pending={lifecycleTarget === detail.id}
                    reason={
                      lifecycleTarget
                        ? "正在更新对话，请稍候。"
                        : "任务正在运行或请求处理中，请稍候再操作。"
                    }
                    onAction={sessionAction}
                  />
                )}
              </>
            )}
          </div>
        </header>
        {error && (
          <div className="error-banner" role="alert">
            <Icon name="info" size={17} />
            <span>{error}</span>
            <button
              className="icon-button"
              aria-label="关闭错误提示"
              onClick={() => setError("")}
            >
              <Icon name="close" size={15} />
            </button>
          </div>
        )}
        <div
          className={`workspace ${view === "chat" && right && right !== "side-chat" ? "with-inspector" : ""} ${view === "chat" && right === "side-chat" && currentSide ? "with-side-chat" : ""}`}
        >
          {view !== "chat" &&
          (readOnly || (lifecycleTarget === selected && !!selected)) ? (
            <div className="management-scroll">
              <div className="management-page">
                <Empty
                  icon="info"
                  title="当前对话仅供查看"
                  detail="请先在对话页取消归档，也可以开启新对话后管理 Skills、记忆与自动化。"
                />
                <button
                  className="button secondary"
                  onClick={() => setView("chat")}
                >
                  返回对话
                </button>
                <button className="button secondary" onClick={newChat}>
                  开启新对话
                </button>
              </div>
            </div>
          ) : view !== "chat" ? (
            <Management
              providers={providers}
              selection={selection}
              view={view}
              selected={selected}
              ensureSession={ensureSession}
              report={report}
            />
          ) : (
            <>
              <section
                className={`chat-area ${identityLoading ? "identity-loading" : ""}`}
                aria-label="主对话"
                onDragOver={(e) => {
                  e.preventDefault();
                  if (
                    !writeLocked &&
                    !closed &&
                    e.dataTransfer.types.includes("Files")
                  )
                    setDragging(true);
                }}
                onDragLeave={(e) => {
                  if (!e.currentTarget.contains(e.relatedTarget as Node))
                    setDragging(false);
                }}
                onDrop={(e) => {
                  e.preventDefault();
                  setDragging(false);
                  if (!closed && !writeLocked)
                    void upload(e.dataTransfer.files);
                }}
              >
                {dragging && (
                  <div className="drop-zone">
                    <Icon name="attach" size={32} />
                    <h3>把文件放在这里</h3>
                    <p>文件会先上传，发送后才进入对话。</p>
                  </div>
                )}
                <div
                  className="conversation-scroll"
                  ref={conversation}
                  onScroll={(e) => {
                    if (loading) return;
                    const el = e.currentTarget;
                    const awayFromBottom =
                      el.scrollHeight - el.scrollTop - el.clientHeight > 160;
                    if (!awayFromBottom) markScrolledUp(false);
                    else if (el.scrollTop < lastConversationTop.current)
                      markScrolledUp(true);
                    lastConversationTop.current = el.scrollTop;
                  }}
                >
                  <div
                    className={`conversation ${!hasHistory ? "welcome-mode" : ""}`}
                    ref={conversationContent}
                  >
                    {detail && (
                      <SessionReadonlyNotice
                        session={detail}
                        disabled={lifecycleBlocked}
                        onAction={sessionAction}
                      />
                    )}
                    {loading ? (
                      <div className="loading-history">
                        <Spinner />
                        正在载入对话
                      </div>
                    ) : !hasHistory ? (
                      <div className="welcome">
                        <div className="welcome-mark">
                          <Star size={38} />
                        </div>
                        <h1>有什么可以帮你？</h1>
                        <p className="welcome-description">
                          {currentProject
                            ? `在「${currentProject.name}」中开始一个任务。`
                            : "开启对话，或选择左侧项目继续工作。"}
                        </p>
                      </div>
                    ) : (
                      <>
                        {lineage && (
                          <div className="lineage-note">
                            <Icon name="branch" size={14} />
                            {lineage.kind === "side_chat"
                              ? "这是一段侧边聊天"
                              : "从已有对话继续"}{" "}
                            · 独立保留此对话的记录
                            {lineage.parent_id && (
                              <button
                                onClick={() =>
                                  void openSession(lineage.parent_id!)
                                }
                              >
                                查看来源
                              </button>
                            )}
                          </div>
                        )}
                        <Conversation
                          records={detail?.messages || []}
                          stream={stream}
                          runId={run?.id || null}
                          busy={busy}
                          readOnly={writeLocked || sideCreating}
                          onBranch={(index) => {
                            if (writeLocked) return;
                            setBranch({ index });
                            setBranchTitle("");
                          }}
                          report={report}
                        />
                        {busy && !stream && (
                          <div className="working-line">
                            <span className="thinking-dots">
                              <i />
                              <i />
                              <i />
                            </span>
                            <span>
                              {compactionActive
                                ? "正在生成模型摘要"
                                : run?.status === "waiting_approval"
                                  ? "等待你的批准"
                                  : run?.status === "waiting_input"
                                    ? "有一个问题需要你确认"
                                    : phase === "executing_tools"
                                      ? "正在执行工具"
                                      : phase === "compacting"
                                        ? "正在整理上下文"
                                        : "正在处理你的任务"}
                            </span>
                          </div>
                        )}
                        {run?.pending_requests?.map((p) => (
                          <PendingCard
                            key={p.id}
                            pending={p}
                            busy={actionBusy || writeLocked}
                            submit={(answer) =>
                              act(async () => {
                                const result = await post<{
                                  accepted: boolean;
                                }>(
                                  p.kind === "approval"
                                    ? `/approvals/${enc(p.id)}/decision`
                                    : `/questions/${enc(p.id)}/answer`,
                                  p.kind === "approval"
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
                            <Icon name="info" size={18} />
                            <div>
                              <Status status={run.status} />
                              <p>
                                {run.status === "needs_reconciliation"
                                  ? "部分操作的结果尚不确定。请先在 Runtime 中核对执行结果，避免重复产生副作用。"
                                  : run.status === "cancelled"
                                    ? "任务已停止，已完成的操作和对话仍会保留。"
                                    : run.error ||
                                      "这次任务未完整完成。你可以补充信息后开始新的请求。"}
                              </p>
                            </div>
                          </div>
                        )}
                      </>
                    )}
                  </div>
                </div>
                <div
                  className={`composer-region ${!hasHistory ? "welcome-composer" : ""}`}
                >
                  {scrolledUp && (
                    <button
                      className="jump-bottom"
                      onClick={() => {
                        markScrolledUp(false);
                        scrollConversationBottom();
                      }}
                    >
                      <Icon name="down" size={14} />
                      回到最新消息
                    </button>
                  )}
                  {compactionActive && (
                    <div className="compact-notice" role="status">
                      <span style={{ flex: "0 0 auto" }}>
                        <Spinner />
                      </span>
                      <span>
                        {compactionCancelling
                          ? "正在取消上下文压缩…"
                          : "正在生成模型摘要，原始对话保留。摘要任务启动后可取消。"}
                      </span>
                    </div>
                  )}
                  {compactNotice && !compactionActive && (
                    <div className="compact-notice" role="status">
                      <Icon name="check" size={13} />
                      <span>{compactNotice}</span>
                      <button
                        aria-label="关闭压缩提示"
                        onClick={() => setCompactNotice("")}
                      >
                        <Icon name="close" size={12} />
                      </button>
                    </div>
                  )}
                  {busy && (
                    <div className="next-run-note">
                      模型、推理和 Skill
                      选择仅用于下一次新请求；本次补充沿用正在执行的配置。
                    </div>
                  )}
                  {streamState && (
                    <div className="stream-warning">{streamState}</div>
                  )}
                  {mail.filter((m) => m.state === "pending").length > 0 && (
                    <div className="mailbox">
                      {mail
                        .filter((m) => m.state === "pending")
                        .map((item) => (
                          <div key={item.id}>
                            <Icon name="clock" size={14} />
                            <span>
                              {item.steering ? "待接收的调整" : "待发送"} ·{" "}
                              {item.text || "附件消息"}
                            </span>
                            <button
                              disabled={writeLocked || actionBusy}
                              onClick={() =>
                                void act(async () => {
                                  const result = await request<{
                                    retracted: boolean;
                                  }>(
                                    `/sessions/${enc(selected!)}/mailbox/${enc(item.id)}`,
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
                    </div>
                  )}
                  {detail?.plan?.snapshot.steps?.length ? (
                    <details className="work-plan">
                      <summary>
                        <Icon name="check" size={14} />
                        工作计划 · {detail.plan.snapshot.steps.length} 步
                        <Icon name="down" size={12} />
                      </summary>
                      {detail.plan.snapshot.steps.map((s, i) => (
                        <div key={i}>
                          <span>{s.status === "completed" ? "✓" : i + 1}</span>
                          {s.text || s.title || s.description || "步骤"}
                          <small>{s.status}</small>
                        </div>
                      ))}
                    </details>
                  ) : null}
                  <form
                    className={`composer ${closed || writeLocked ? "disabled" : ""}`}
                    onSubmit={submit}
                  >
                    {chosenFiles.length > 0 && (
                      <div className="draft-attachments">
                        {chosenFiles.map((file) => (
                          <div key={file.attachment_id}>
                            <span className="file-mini">
                              <Icon name="file" size={17} />
                            </span>
                            <span>
                              <strong>{file.filename}</strong>
                              <small>{sizeLabel(file.size)}</small>
                            </span>
                            <button
                              type="button"
                              aria-label={`移除 ${file.filename}`}
                              disabled={writeLocked}
                              onClick={() =>
                                setDraftFiles((old) => ({
                                  ...old,
                                  [key]: (old[key] || []).filter(
                                    (f) =>
                                      f.attachment_id !== file.attachment_id,
                                  ),
                                }))
                              }
                            >
                              <Icon name="close" size={13} />
                            </button>
                          </div>
                        ))}
                      </div>
                    )}
                    <textarea
                      ref={textarea}
                      aria-label="输入消息"
                      placeholder={
                        readOnly
                          ? "恢复或取消归档后可继续对话"
                          : closed
                            ? "这段侧边聊天已结束，可从历史创建新分支"
                            : busy
                              ? "补充信息，或调整正在进行的任务…"
                              : "有什么想一起完成的？"
                      }
                      value={draft}
                      onChange={(e) => setDraft(e.target.value)}
                      disabled={closed || writeLocked}
                      rows={2}
                      onKeyDown={(e: KeyboardEvent<HTMLTextAreaElement>) => {
                        if (
                          e.key === "Enter" &&
                          !e.shiftKey &&
                          !e.nativeEvent.isComposing
                        ) {
                          e.preventDefault();
                          void submit();
                        }
                      }}
                    />
                    <div className="composer-toolbar">
                      <div className="composer-tools">
                        {selected ? (
                          <output
                            className="agent-label"
                            aria-label="当前助手"
                            title={`${currentAssistant} · 创建后助手只读`}
                          >
                            {currentAssistant}
                          </output>
                        ) : (
                          <select
                            className="agent-picker"
                            aria-label="新对话 Agent"
                            title="新对话使用的助手配置，包含指令、工具权限和默认模型"
                            value={newAgent}
                            disabled={
                              sending || uploading || !!creatingProjectSession
                            }
                            onChange={(e) => {
                              const agent = agents.find(
                                (item) => item.id === e.target.value,
                              );
                              if (!agent) return;
                              setNewAgent(agent.id);
                              newSelectionExplicit.current = false;
                              selectionCache.current.new = {
                                ...agent.selection,
                              };
                              selectionRef.current = agent.selection;
                              setSelection(agent.selection);
                            }}
                          >
                            {agents.map((agent) => (
                              <option value={agent.id} key={agent.id}>
                                {assistantName(agent.id)}
                              </option>
                            ))}
                          </select>
                        )}
                        <ComposerMenu
                          onUpload={() => fileInput.current?.click()}
                          onCompact={() => void compact()}
                          onSummarize={() => void compact("semantic")}
                          onSkills={() => void openSkills()}
                          canCompact={
                            !!selected &&
                            hasHistory &&
                            !busy &&
                            !actionBusy &&
                            !compactionSession &&
                            !closed &&
                            !writeLocked &&
                            run?.status !== "needs_reconciliation"
                          }
                          compactReason={
                            busy
                              ? "任务运行中，完成后可压缩"
                              : !hasHistory
                                ? "开始对话后可压缩上下文"
                                : "保留原始记录，仅整理模型上下文"
                          }
                          disabled={uploading || closed || writeLocked}
                        />
                        {uploading && <Spinner />}
                        {selection.selected_skill && (
                          <button
                            type="button"
                            className="selected-skill-chip"
                            title="移除下次请求的 Skill"
                            disabled={writeLocked}
                            onClick={() =>
                              updateSelection({
                                ...selection,
                                selected_skill: null,
                              })
                            }
                          >
                            <Icon name="grid" size={13} />
                            <span>{selection.selected_skill}</span>
                            <Icon name="close" size={11} />
                          </button>
                        )}
                      </div>
                      <div className="composer-send">
                        <ContextMeter
                          key={selected || "new"}
                          sessionId={selected}
                          revision={`${detail?.messages.length || 0}:${run?.updated_at || 0}:${run?.status || ""}:${compactNotice}`}
                          model={selection.model}
                          providerId={selection.provider_id}
                          active={busy || compactionActive}
                        />
                        <ModelOptions
                          providers={providers}
                          selection={selection}
                          onChange={updateSelection}
                          busy={busy}
                          disabled={writeLocked}
                        />
                        {(busy || compactionActive) && (
                          <button
                            className="cancel-button"
                            type="button"
                            onClick={() =>
                              compactionActive
                                ? void cancelCompaction()
                                : void act(async () => {
                                    const r = await post<{ accepted: boolean }>(
                                      `/runs/${enc(run!.id)}/cancel`,
                                    );
                                    if (!r.accepted)
                                      throw new Error(
                                        "当前任务已结束，已刷新状态。",
                                      );
                                  })
                            }
                            disabled={
                              compactionActive
                                ? !compactionRunReady ||
                                  compactionCancelling ||
                                  writeLocked
                                : actionBusy ||
                                  writeLocked ||
                                  run?.status === "cancelling"
                            }
                          >
                            <Icon name="stop" size={13} />
                            {compactionActive ? "取消压缩" : "停止"}
                          </button>
                        )}
                        <button
                          className="send-button"
                          aria-label={busy ? "发送补充消息" : "发送消息"}
                          type="submit"
                          disabled={
                            sending ||
                            compactionActive ||
                            uploading ||
                            loading ||
                            closed ||
                            writeLocked ||
                            (!draft.trim() && !chosenFiles.length) ||
                            (!busy && !providerReady) ||
                            run?.status === "needs_reconciliation"
                          }
                        >
                          {sending ? (
                            <Spinner />
                          ) : (
                            <Icon name="arrow" size={20} />
                          )}
                        </button>
                      </div>
                    </div>
                  </form>
                  <div className="composer-footer">
                    {readOnly ? (
                      <span>对话与附件保留，可随时恢复。</span>
                    ) : busy ? (
                      <label className="steer-toggle">
                        <input
                          type="checkbox"
                          checked={steer}
                          onChange={(e) => setSteer(e.target.checked)}
                        />
                        作为方向调整 · 在安全边界接收
                        <span className="next-selection-note">
                          模型与 Skill 选择只用于下一次新请求
                        </span>
                      </label>
                    ) : !providerReady ? (
                      <span className="provider-warning">
                        请在左下角设置中配置提供商，并选择可用模型。
                      </span>
                    ) : (
                      <span>Aster 可能会出错，请核对重要结果。</span>
                    )}
                    <span className="keyboard-hint">
                      Enter 发送 · Shift + Enter 换行
                    </span>
                  </div>
                  <input
                    type="file"
                    ref={fileInput}
                    multiple
                    hidden
                    onChange={(e) => {
                      if (e.target.files) void upload(e.target.files);
                    }}
                  />
                </div>
              </section>
              {right && right !== "side-chat" && (
                <aside className="inspector">
                  <div className="inspector-heading">
                    <h2>{right === "files" ? "文件与成果" : "执行详情"}</h2>
                    <button
                      className="icon-button"
                      aria-label="关闭详情面板"
                      onClick={() => setRight(null)}
                    >
                      <Icon name="close" size={16} />
                    </button>
                  </div>
                  {right === "files" ? (
                    <>
                      <div className="section-label">
                        交付成果 <span>{deliverables.length}</span>
                      </div>
                      {deliverables.length ? (
                        deliverables.map((a) => (
                          <div className="artifact-card" key={a.artifact_id}>
                            <span className="artifact-icon">
                              <Icon name="file" size={23} />
                            </span>
                            <strong>
                              {a.title ||
                                a.filename ||
                                a.path?.split(/[\\/]/).pop()}
                            </strong>
                            <small>{sizeLabel(a.size)}</small>
                            <div>
                              <button
                                onClick={() =>
                                  void download(
                                    a.artifact_id,
                                    a.filename ||
                                      a.path?.split(/[\\/]/).pop() ||
                                      "artifact",
                                  ).catch(report)
                                }
                              >
                                <Icon name="download" size={14} />
                                下载
                              </button>
                              <button onClick={() => void preview(a)}>
                                <Icon name="external" size={14} />
                                静态预览
                              </button>
                            </div>
                          </div>
                        ))
                      ) : (
                        <Empty
                          title="成果会出现在这里"
                          detail="任务生成的文件可在这里下载与预览。"
                        />
                      )}
                      <div className="section-label">
                        会话附件 <span>{attached.length}</span>
                      </div>
                      {attached.map((file) => (
                        <button
                          className="attachment-row"
                          key={file.attachment_id}
                          onClick={() => {
                            setDraftFiles((old) => ({
                              ...old,
                              [key]: old[key]?.some(
                                (f) => f.attachment_id === file.attachment_id,
                              )
                                ? old[key]
                                : [...(old[key] || []), file].slice(0, 20),
                            }));
                            textarea.current?.focus();
                          }}
                          disabled={closed || writeLocked}
                        >
                          <Icon name="file" size={18} />
                          <span>
                            {file.filename}
                            <small>
                              {sizeLabel(file.size)} ·{" "}
                              {readOnly ? "保留的附件" : "点击加入消息"}
                            </small>
                          </span>
                          <Icon name="plus" size={14} />
                        </button>
                      ))}
                    </>
                  ) : run ? (
                    <>
                      <Status status={run.status} />
                      <div className="section-label">此次执行</div>
                      <dl className="metric-list">
                        <dt>模型请求</dt>
                        <dd>{run.budget?.model_requests ?? "—"}</dd>
                        <dt>工具调用</dt>
                        <dd>{run.budget?.tool_calls ?? "—"}</dd>
                        <dt>开始时间</dt>
                        <dd>{dateLabel(run.created_at)}</dd>
                        <dt>输入 token</dt>
                        <dd>{sumUsage(run, "input_tokens")}</dd>
                        <dt>输出 token</dt>
                        <dd>{sumUsage(run, "output_tokens")}</dd>
                      </dl>
                      <p className="fine-print">
                        未报告的 usage 显示为未知，不按零计算。
                      </p>
                      {performance &&
                        Object.entries(performance.phase_seconds).length >
                          0 && (
                          <>
                            <div className="section-label">阶段耗时</div>
                            <dl className="metric-list">
                              {Object.entries(performance.phase_seconds).map(
                                ([key, value]) => (
                                  <div className="metric-pair" key={key}>
                                    <dt>{phaseName(key)}</dt>
                                    <dd>{value.toFixed(1)} s</dd>
                                  </div>
                                ),
                              )}
                            </dl>
                          </>
                        )}
                      <button
                        className="text-button"
                        onClick={() =>
                          setPerformanceRevision((value) => value + 1)
                        }
                      >
                        刷新执行轨迹
                      </button>
                      {performance && <ExecutionTimeline value={performance} />}
                      <div className="section-label">工具执行</div>
                      {run.tool_executions?.length ? (
                        run.tool_executions.map((t) => (
                          <div className="execution-row" key={t.id}>
                            <Icon
                              name={
                                t.state === "outcome_unknown" ? "info" : "bolt"
                              }
                              size={14}
                            />
                            <code>{t.tool_name}</code>
                            <small>
                              {t.state === "checkpointed" ||
                              t.state === "committed"
                                ? "已返回"
                                : t.state === "outcome_unknown"
                                  ? "结果未知"
                                  : t.state}
                            </small>
                          </div>
                        ))
                      ) : (
                        <p className="fine-print">此次运行还没有工具记录。</p>
                      )}
                      <div className="inspector-bottom">
                        {hasHistory && (
                          <button
                            className="button secondary"
                            onClick={() => {
                              setBranch({ index: lastBoundary });
                              setBranchTitle("");
                            }}
                            disabled={!lastBoundary || busy || writeLocked}
                          >
                            <Icon name="branch" size={15} />
                            从完整边界创建分支
                          </button>
                        )}
                        {lineage?.kind === "side_chat" && !closed && (
                          <button
                            className="text-button"
                            disabled={busy || actionBusy || writeLocked}
                            onClick={() =>
                              void act(async () => {
                                await post(`/sessions/${enc(selected!)}/close`);
                              })
                            }
                          >
                            结束侧边聊天，保留记录
                          </button>
                        )}
                      </div>
                    </>
                  ) : (
                    <Empty
                      icon="bolt"
                      title="还没有执行记录"
                      detail="开始一个任务后，这里会显示实时状态和实际用量。"
                    />
                  )}
                </aside>
              )}
            </>
          )}
          {sideChats.map((session) => (
            <SideChatPanel
              key={session.id}
              session={session}
              lifecycleBusy={lifecycleTarget === session.id}
              visible={
                view === "chat" &&
                right === "side-chat" &&
                activeSideId === session.id
              }
              providers={providers}
              assistantName={assistantName(session.agent_id)}
              options={sideOptions}
              onSelect={(item) => showSideChat(item)}
              onCollapse={() => {
                setRight(null);
                requestAnimationFrame(() =>
                  textarea.current?.focus({ preventScroll: true }),
                );
              }}
              onRequestDelete={() => actionForSession(session, "delete")}
              onSource={() => {
                if (window.matchMedia("(max-width: 1000px)").matches)
                  setRight(null);
                const index =
                  detail?.messages.findIndex(
                    (item) => item.seq === session.lineage?.boundary_seq,
                  ) ?? -1;
                requestAnimationFrame(() => revealSideSource(index + 1));
              }}
              onChanged={refreshSessions}
              onAuthError={report}
            />
          ))}
        </div>
      </main>
      {settings && (
        <Suspense
          fallback={
            <Modal
              title="设置"
              onClose={() => setSettings(false)}
              className="settings-modal"
            >
              <Spinner />
            </Modal>
          }
        >
          <ProviderSettings
            selected={selected}
            onManagementChanged={(value) => {
              setAgents(value.agents);
              if (!selectedRef.current && !newSelectionExplicit.current) {
                const agent = value.agents.find((item) => item.id === newAgent);
                if (agent) {
                  selectionCache.current.new = { ...agent.selection };
                  selectionRef.current = agent.selection;
                  setSelection(agent.selection);
                }
              }
            }}
            onClose={() => setSettings(false)}
            onChanged={(items, values) => {
              setProviders(items);
              setProtocols(values);
            }}
            report={report}
            archive={
              <ArchivedSessions
                revision={archiveRevision}
                pending={lifecycleTarget}
                operationError={lifecycleError}
                onOpen={(session) => {
                  setSettings(false);
                  void openWorkspaceSession(session.id, session);
                }}
                onAction={actionForSession}
                report={report}
              />
            }
          />
        </Suspense>
      )}
      {deletingSession && (
        <Modal
          title={
            deletingSession.lineage?.kind === "side_chat"
              ? "删除侧边聊天"
              : "永久删除对话"
          }
          onClose={() => {
            if (!lifecycleTarget) setDeletingSession(null);
          }}
        >
          {lifecycleError && (
            <p className="inline-error" role="alert">
              {lifecycleError}
            </p>
          )}
          <p className="modal-description">
            {deletingSession.lineage?.kind === "side_chat"
              ? `是否删除「${deletingSession.title || "侧边聊天"}」？侧边聊天的记录、未发送草稿及专属附件将删除，无法恢复。主对话和共享文件会保留。`
              : `删除「${deletingSession.title || "新对话"}」后，该对话的记录、附件和运行数据将永久删除，无法恢复。其他分支和项目工作目录中的文件会保留。`}
          </p>
          <div className="modal-actions">
            <button
              className="button secondary"
              disabled={!!lifecycleTarget}
              onClick={() => setDeletingSession(null)}
            >
              取消
            </button>
            <button
              className="button danger"
              disabled={
                !!lifecycleTarget ||
                (deletingSession.id === selected && lifecycleBlocked)
              }
              onClick={() => void changeLifecycle(deletingSession, "delete")}
            >
              {lifecycleTarget ? <Spinner /> : null}永久删除
            </button>
          </div>
        </Modal>
      )}
      {skillPicker && (
        <SkillPicker
          skills={eligibleSkills}
          current={selection.selected_skill}
          loading={skillsLoading}
          onClose={() => {
            skillsRequest.current++;
            setSkillPicker(false);
          }}
          onChoose={(name) => {
            skillsRequest.current++;
            updateSelection({ ...selection, selected_skill: name });
            setSkillPicker(false);
          }}
        />
      )}
      {renameOpen && (
        <Modal title="重命名对话" onClose={() => setRenameOpen(false)}>
          <label className="field">
            对话名称
            <input
              value={sessionTitle}
              onChange={(e) => setSessionTitle(e.target.value)}
              maxLength={200}
            />
          </label>
          <div className="modal-actions">
            <button
              className="button secondary"
              onClick={() => setRenameOpen(false)}
            >
              取消
            </button>
            <button
              className="button primary"
              disabled={actionBusy}
              onClick={() =>
                void act(async () => {
                  await request(`/sessions/${enc(selected!)}`, {
                    method: "PATCH",
                    body: JSON.stringify({ title: sessionTitle }),
                  });
                  setRenameOpen(false);
                })
              }
            >
              保存名称
            </button>
          </div>
        </Modal>
      )}
      {branch && (
        <Modal title="从这里创建分支" onClose={() => setBranch(null)}>
          <p className="modal-description">
            保留前 {branch.index}{" "}
            条原始消息的完整上下文，之后的内容不会带入。原始对话和执行审计保持不变，已有审批不会继承。
          </p>
          <label className="field">
            名称（可选）
            <input
              autoFocus
              value={branchTitle}
              onChange={(e) => setBranchTitle(e.target.value)}
              placeholder="继续这个思路"
            />
          </label>
          <div className="modal-actions">
            <button
              className="button secondary"
              onClick={() => setBranch(null)}
            >
              取消
            </button>
            <button
              className="button primary"
              disabled={actionBusy}
              onClick={() => void makeBranch()}
            >
              {actionBusy ? <Spinner /> : <Icon name="branch" size={15} />}
              创建分支
            </button>
          </div>
        </Modal>
      )}
    </div>
  );
}

function sumUsage(run: Run, key: "input_tokens" | "output_tokens") {
  if (!run.usage?.length || run.usage.some((u) => u[key] == null))
    return "未知";
  return run.usage.reduce((n, u) => n + (u[key] || 0), 0).toLocaleString();
}
function phaseName(key: string) {
  return (
    (
      {
        setup: "准备",
        awaiting_model: "等待模型",
        executing_tools: "执行工具",
        awaiting_approval: "等待批准",
        awaiting_input: "等待回答",
        compacting: "压缩上下文",
        done: "结束",
      } as Record<string, string>
    )[key] || key
  );
}
