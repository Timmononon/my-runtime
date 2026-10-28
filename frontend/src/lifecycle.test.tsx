import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import Conversation from "./Conversation";
import { ModelOptions } from "./ComposerOptions";
import {
  SessionReadonlyNotice,
  WorkspaceSessionRows,
  sessionState,
} from "./SessionLifecycle";
import { workspaceSessions } from "./workspaceTree";
import Projects from "./Projects";
import type { MessageRecord, Project, Session } from "./types";

const session: Session = {
  id: "s",
  title: "Saved conversation",
  cwd: "C:/fixture",
  agent_id: "default",
  project_id: null,
  updated_at: 1,
  archived_at: 2,
};
const noop = () => {};

describe("conversation lifecycle and workspace tree", () => {
  it("keeps archived conversations readonly with an explicit settings location", () => {
    const html = renderToStaticMarkup(
      <SessionReadonlyNotice
        session={session}
        disabled={false}
        onAction={noop}
      />,
    );
    expect(html).toContain("已归档的对话仅供查看");
    expect(html).toContain("归档列表在设置中");
    expect(html).toContain("取消归档");
    expect(html).not.toContain("恢复对话");
    const archive = renderToStaticMarkup(
      <SessionReadonlyNotice
        session={session}
        disabled={true}
        onAction={noop}
      />,
    );
    expect(archive).toContain("取消归档");
    expect(archive).toMatch(/<button[^>]*disabled/);
    expect(sessionState({ ...session, archived_at: null })).toBe("active");
    expect(sessionState(session)).toBe("archived");
  });

  it("keeps transcript content and copy affordance while removing branch actions in readonly mode", () => {
    const records: MessageRecord[] = [
      {
        id: "m1",
        created_at: 1,
        seq: 1,
        run_id: "r",
        message: {
          role: "user",
          content: "original question",
          tool_calls: [],
          metadata: {},
        },
      },
      {
        id: "m2",
        created_at: 2,
        seq: 2,
        run_id: "r",
        message: {
          role: "assistant",
          content: "original answer",
          tool_calls: [],
          metadata: {},
        },
      },
    ];
    const render = (readOnly: boolean) =>
      renderToStaticMarkup(
        <Conversation
          records={records}
          stream=""
          runId={null}
          busy={false}
          readOnly={readOnly}
          onBranch={noop}
          report={noop}
        />,
      );
    const active = render(false),
      archived = render(true);
    expect(active).toContain("从这里分支");
    expect(archived).toContain("original question");
    expect(archived).toContain("original answer");
    expect(archived).toContain("复制回答");
    expect(archived).not.toContain("从这里分支");
    expect(archived).not.toContain("侧边聊天");
    expect(archived).not.toContain("选择更早的分支位置");
  });

  it("makes conversation management discoverable on each tree row", () => {
    const rows = renderToStaticMarkup(
      <WorkspaceSessionRows
        sessions={[session]}
        selected={session.id}
        pending={null}
        selectedBlocked={false}
        onOpen={noop}
        onAction={noop}
      />,
    );
    expect(rows).toContain('aria-label="管理对话 Saved conversation"');
    expect(rows).toContain('title="归档或删除对话"');
    expect(rows).not.toContain("已删除");
    const model = renderToStaticMarkup(
      <ModelOptions
        providers={[]}
        selection={{
          provider_id: "",
          model: "",
          reasoning_effort: null,
          selected_skill: null,
        }}
        busy={false}
        disabled={true}
        onChange={noop}
      />,
    );
    expect(model).toMatch(/aria-label="选择模型" disabled/);
    expect(model).toMatch(/aria-label="选择推理强度" disabled/);
  });

  it("groups multiple conversations per project and keeps orphaned history accessible", () => {
    const project: Project = {
      id: "p",
      name: "Research",
      cwd: "C:/fixture",
      created_at: 1,
      updated_at: 1,
      session_count: 2,
    };
    const first = { ...session, id: "first", project_id: project.id },
      second = { ...first, id: "second" },
      orphan = { ...session, id: "orphan", project_id: "removed" };
    expect(
      workspaceSessions(
        [session, first, second, orphan],
        [project],
        project,
      ).map((item) => item.id),
    ).toEqual(["first", "second"]);
    expect(
      workspaceSessions([session, first, second, orphan], [project], null).map(
        (item) => item.id,
      ),
    ).toEqual(["s", "orphan"]);
    expect(
      workspaceSessions([first, second], [], null).map((item) => item.id),
    ).toEqual(["first", "second"]);
    const html = renderToStaticMarkup(
      <Projects
        projects={[project]}
        selected={project.id}
        onSelect={noop}
        onChanged={noop}
        report={noop}
        onNewChat={noop}
        creatingSession={null}
        renderSessions={() => null}
        searchControl={null}
        searchField={null}
      />,
    );
    expect(html).toContain('aria-label="在项目 Research 中新建对话"');
    expect(html).toContain('aria-label="管理项目 Research"');
    expect(html).toContain('aria-label="删除项目 Research"');
    expect(html).not.toContain("已删除项目");
  });
});
