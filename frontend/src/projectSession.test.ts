import { describe, expect, it, vi } from "vitest";
import { ApiError } from "./api";
import {
  findEmptyProjectSession,
  projectTaskPresence,
  projectGithubPresence,
  type ProjectSessionSnapshot,
} from "./projectSession";
import type { Project, Session, SessionDetail } from "./types";

const project: Project = {
  id: "p",
  name: "Project",
  cwd: "D:\\project",
  created_at: 1,
  updated_at: 1,
  session_count: 0,
};
const session = (id: string, fields: Partial<Session> = {}): Session => ({
  id,
  title: "",
  cwd: project.cwd,
  agent_id: "default",
  project_id: project.id,
  updated_at: 1,
  ...fields,
});
const detail = (item: Session): SessionDetail => ({
  ...item,
  messages: [],
  runs: [],
  plan: null,
});
const snapshot = (
  item: Session,
  fields: Partial<ProjectSessionSnapshot> = {},
): ProjectSessionSnapshot => ({
  detail: detail(item),
  hasAttachments: false,
  hasMailbox: false,
  hasLineage: false,
  hasAutomation: false,
  hasTasks: false,
  hasGithubSources: false,
  ...fields,
});
const defaults = {
  project,
  preferred: null,
  blocked: () => false,
  valid: () => true,
};

describe("empty project conversation reuse", () => {
  it("reuses only the selected Agent's empty history", async () => {
    const other = session("default"),
      selected = session("research", { agent_id: "research" });
    const readSession = vi.fn(async () => snapshot(selected));
    const found = await findEmptyProjectSession({
      ...defaults,
      agent: "research",
      preferred: other,
      listPage: async () => [other, selected],
      readSession,
    });
    expect(found?.id).toBe(selected.id);
    expect(readSession).toHaveBeenCalledExactlyOnceWith(selected.id);
  });
  it("rechecks and reuses the current empty session before scanning pages", async () => {
    const preferred = session("current");
    const value = snapshot(preferred);
    const listPage = vi.fn();
    const readSession = vi.fn(async () => value);
    expect(
      await findEmptyProjectSession({
        ...defaults,
        preferred,
        listPage,
        readSession,
      }),
    ).toBe(value.detail);
    expect(readSession).toHaveBeenCalledExactlyOnceWith("current");
    expect(listPage).not.toHaveBeenCalled();
  });

  it("scans beyond sidebar pages and filters unrelated summaries before reading details", async () => {
    const unrelated = Array.from({ length: 100 }, (_, i) =>
      session(`other-${i}`, { project_id: "other" }),
    );
    const excluded = [
      session("renamed", { title: "新对话" }),
      session("agent", { agent_id: "other" }),
      session("cwd", { cwd: "D:\\other" }),
      session("archived", { archived_at: 2 }),
      session("deleted", { deleted_at: 2 }),
      session("draft"),
      session("draft-files"),
    ];
    const available = session("available");
    const listPage = vi
      .fn()
      .mockResolvedValueOnce(unrelated)
      .mockResolvedValueOnce([...excluded, available]);
    const readSession = vi.fn(async () => snapshot(available));
    expect(
      await findEmptyProjectSession({
        ...defaults,
        blocked: (id) => id === "draft" || id === "draft-files",
        listPage,
        readSession,
      }),
    ).toEqual(detail(available));
    expect(listPage.mock.calls).toEqual([[0], [100]]);
    expect(readSession).toHaveBeenCalledExactlyOnceWith("available");
  });

  it("rejects persisted content, uploads, lineage, plans and automation even without a title", async () => {
    const candidates = Array.from({ length: 8 }, (_, i) =>
      session(`used-${i}`),
    );
    const values = candidates.map((item) => snapshot(item));
    values[0].detail.messages = [
      {
        id: "m",
        seq: 1,
        run_id: null,
        created_at: 1,
        message: { role: "user", content: "", tool_calls: [], metadata: {} },
      },
    ];
    values[1].detail.runs = [
      {
        id: "r",
        session_id: candidates[1].id,
        status: "failed",
        created_at: 1,
        updated_at: 1,
      },
    ];
    values[2].detail.plan = { revision: 1, snapshot: {} };
    values[3].hasAttachments = true;
    values[4].hasMailbox = true;
    values[5].hasLineage = true;
    values[6].hasAutomation = true;
    values[7].detail.title = "Renamed during lookup";
    expect(
      await findEmptyProjectSession({
        ...defaults,
        listPage: async () => candidates,
        readSession: async (id) => values.find((v) => v.detail.id === id)!,
      }),
    ).toBeNull();
  });

  it("checks the latest draft state after a delayed candidate read", async () => {
    const preferred = session("current");
    let hasDraft = false;
    expect(
      await findEmptyProjectSession({
        ...defaults,
        preferred,
        blocked: () => hasDraft,
        listPage: async () => [preferred],
        readSession: async () => {
          hasDraft = true;
          return snapshot(preferred);
        },
      }),
    ).toBeNull();
  });

  it("preserves message-empty sources with retained tasks and Worker bindings", async () => {
    const source = session("task-source"),
      worker = session("task-worker"),
      pristine = session("pristine");
    const readTasks = vi.fn(async () => ({
      tickets: [{ id: "completed-retained-task" }],
    }));
    const bound = {
      ...detail(worker),
      local_task: { ticket_id: "ticket", worker_id: "worker" },
    };
    expect(
      await projectTaskPresence(bound, ["local_tasks_v1"], readTasks),
    ).toBe(true);
    expect(readTasks).not.toHaveBeenCalled();
    const sourceUsed = await projectTaskPresence(
      detail(source),
      ["local_tasks_v1"],
      readTasks,
    );
    expect(sourceUsed).toBe(true);
    const found = await findEmptyProjectSession({
      ...defaults,
      listPage: async () => [source, worker, pristine],
      readSession: async (id) =>
        snapshot(session(id), { hasTasks: id !== pristine.id }),
    });
    expect(found?.id).toBe(pristine.id);
  });

  it("gates task reads on capability and fails closed on unverified task presence", async () => {
    const item = detail(session("candidate"));
    const unavailable = vi.fn(async () => {
      throw new ApiError(503, "Task lookup failed");
    });
    expect(await projectTaskPresence(item, ["runs_v1"], unavailable)).toBe(
      false,
    );
    expect(unavailable).not.toHaveBeenCalled();
    await expect(
      projectTaskPresence(item, ["local_tasks_v1"], unavailable),
    ).rejects.toThrow("Task lookup failed");
    await expect(
      projectTaskPresence(item, ["local_tasks_v1"], async () => ({
        tickets: null as unknown as unknown[],
      })),
    ).rejects.toThrow("无法确认");
    await expect(
      findEmptyProjectSession({
        ...defaults,
        listPage: async () => [item],
        readSession: async () => ({
          ...snapshot(item),
          hasTasks: await projectTaskPresence(
            item,
            ["local_tasks_v1"],
            async () => {
              throw new ApiError(404, "Missing task endpoint");
            },
          ),
        }),
      }),
    ).rejects.toThrow("任务关联查询不可用");
    await expect(
      findEmptyProjectSession({
        ...defaults,
        listPage: async () => [item],
        readSession: async () => {
          const hasTasks = await projectTaskPresence(
            item,
            ["local_tasks_v1"],
            unavailable,
          );
          return snapshot(item, { hasTasks });
        },
      }),
    ).rejects.toThrow("Task lookup failed");
  });

  it("preserves disabled GitHub source bindings and gates observational lookups on capability", async () => {
    const used = session("github-bound"),
      blank = session("blank");
    const readSources = vi.fn(async () => ({
      sources: [{ enabled: false, mode: "read_only" }],
    }));
    expect(
      await projectGithubPresence(detail(used), ["runs_v1"], readSources),
    ).toBe(false);
    expect(readSources).not.toHaveBeenCalled();
    expect(
      await projectGithubPresence(
        detail(used),
        ["github_sources_v1"],
        readSources,
      ),
    ).toBe(true);
    expect(
      await findEmptyProjectSession({
        ...defaults,
        listPage: async () => [used, blank],
        readSession: async (id) =>
          snapshot(session(id), { hasGithubSources: id === used.id }),
      }),
    ).toEqual(detail(blank));
    await expect(
      findEmptyProjectSession({
        ...defaults,
        listPage: async () => [used],
        readSession: async () => ({
          ...snapshot(used),
          hasGithubSources: await projectGithubPresence(
            detail(used),
            ["github_sources_v1"],
            async () => {
              throw new ApiError(404, "missing source endpoint");
            },
          ),
        }),
      }),
    ).rejects.toThrow("GitHub 来源查询不可用");
    await expect(
      projectGithubPresence(detail(used), ["github_sources_v1"], async () => ({
        sources: null as unknown as unknown[],
      })),
    ).rejects.toThrow("无法确认");
  });

  it("discards delayed lookup results after navigation without selecting or creating", async () => {
    const preferred = session("current");
    let valid = true;
    const listPage = vi.fn();
    expect(
      await findEmptyProjectSession({
        ...defaults,
        preferred,
        valid: () => valid,
        listPage,
        readSession: async () => {
          valid = false;
          return snapshot(preferred);
        },
      }),
    ).toBeUndefined();
    expect(listPage).not.toHaveBeenCalled();
    valid = true;
    const readSession = vi.fn();
    expect(
      await findEmptyProjectSession({
        ...defaults,
        valid: () => valid,
        listPage: async () => {
          valid = false;
          return [preferred];
        },
        readSession,
      }),
    ).toBeUndefined();
    expect(readSession).not.toHaveBeenCalled();
  });

  it("skips concurrently deleted candidates but does not treat read failures as an empty page", async () => {
    const missing = session("missing"),
      available = session("available");
    expect(
      await findEmptyProjectSession({
        ...defaults,
        listPage: async () => [missing, available],
        readSession: async (id) => {
          if (id === missing.id) throw new ApiError(404, "Not found");
          return snapshot(available);
        },
      }),
    ).toEqual(detail(available));
    await expect(
      findEmptyProjectSession({
        ...defaults,
        listPage: async () => [available],
        readSession: async () => {
          throw new ApiError(500, "Failed lookup");
        },
      }),
    ).rejects.toThrow("Failed lookup");
  });
});
