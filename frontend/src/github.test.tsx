import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { GithubDraftReview, GithubSourceFields } from "./GitHubPanel";
import type { GithubDraft, GithubSource } from "./githubTypes";
import {
  accountList,
  draftActions,
  githubDeliveryLabel,
  githubDraftLabel,
  newGithubSource,
  safeGithubUrl,
  sourcePayload,
} from "./githubTypes";

const source = (changes: Partial<GithubSource> = {}): GithubSource => ({
  ...sourcePayload({ ...newGithubSource(), repository: "fixture/repo" }),
  id: "source",
  revision: 2,
  source_session_id: "session",
  configured: false,
  verified_login: null,
  health: "unchecked",
  last_poll_at: null,
  next_poll_at: null,
  error: null,
  identity_generation: 3,
  ...changes,
});
const draft = (changes: Partial<GithubDraft> = {}): GithubDraft => ({
  id: "draft",
  revision: 1,
  source_id: "source",
  ticket_id: null,
  kind: "comment",
  number: 7,
  body: "Reviewed text",
  title: null,
  head: null,
  base: null,
  state: "draft",
  url: null,
  error: null,
  created_at: 1,
  identity_generation: 3,
  ...changes,
});
describe("GitHub source controls", () => {
  it("defaults to disabled/read-only/writes off and supports anonymous public reading", () => {
    const fields = newGithubSource();
    expect(fields).toMatchObject({
      enabled: false,
      mode: "read_only",
      allow_writes: false,
    });
    const html = renderToStaticMarkup(
      <GithubSourceFields
        value={fields}
        onChange={() => {}}
        disabled={false}
      />,
    );
    expect(html).toContain("默认关闭后台轮询、只读且禁止发送");
    expect(html).toContain("只读：记录事件，不创建任务或发送");
    expect(source().configured).toBe(false);
    expect(draftActions(draft(), source()).send).toBe(false);
  });
  it("serializes explicit metadata only and deduplicates source-native actors", () => {
    const fields = {
      ...newGithubSource(),
      repository: " fixture/repo ",
      allowed_actors: "@Alice\nalice,Bob",
      token: "never-in-metadata",
      configured: true,
    };
    const payload = sourcePayload(fields);
    expect(payload.repository).toBe("fixture/repo");
    expect(payload.allowed_actors).toEqual(["Alice", "Bob"]);
    expect(payload).not.toHaveProperty("token");
    expect(payload).not.toHaveProperty("configured");
    expect(accountList("github-actions[bot]\n@gitHub-actions[bot]")).toEqual([
      "github-actions[bot]",
    ]);
    expect(() =>
      sourcePayload({
        ...fields,
        repository: "https://github.com/fixture/repo",
      }),
    ).toThrow("owner/repo");
    expect(() => accountList("alice is allowed")).toThrow("完整 GitHub 账号");
  });
  it("links only HTTPS pages of the exact repository without credentials or lookalike hosts", () => {
    expect(
      safeGithubUrl("https://github.com/FIXTURE/repo/pull/8", "fixture/repo"),
    ).toBeTruthy();
    for (const url of [
      "http://github.com/fixture/repo/issues/7",
      "https://github.com.evil.test/fixture/repo",
      "https://github.com/fixture/other/issues/7",
      "https://person:secret@github.com/fixture/repo",
      "javascript:alert(1)",
    ])
      expect(safeGithubUrl(url, "fixture/repo")).toBeNull();
  });
  it("publishes only explicitly enabled, verified, configured, same-generation drafts", () => {
    const enabled = source({
      enabled: true,
      mode: "tasks",
      allow_writes: true,
      configured: true,
      verified_login: "fixture-user",
    });
    expect(draftActions(draft(), enabled)).toEqual({
      edit: true,
      send: true,
      reconcile: false,
    });
    for (const changes of [
      { enabled: false },
      { mode: "read_only" as const },
      { allow_writes: false },
      { configured: false },
      { verified_login: null },
      { identity_generation: 4 },
    ])
      expect(draftActions(draft(), { ...enabled, ...changes }).send).toBe(
        false,
      );
    expect(draftActions(draft({ state: "outcome_unknown" }), enabled)).toEqual({
      edit: false,
      send: false,
      reconcile: true,
    });
    expect(draftActions(draft({ state: "sending" }), enabled)).toEqual({
      edit: false,
      send: false,
      reconcile: false,
    });
    expect(draftActions(draft({ state: "sent" }), enabled).send).toBe(false);
  });
  it("shows the exact reviewed destination and content while keeping PR delivery separate from execution", () => {
    const html = renderToStaticMarkup(
      <GithubDraftReview
        draft={draft({ body: "明确审阅的正文\n\n<script>do_bad()</script>" })}
        repository="fixture/repo"
        account="fixture-user"
      />,
    );
    expect(html).toContain("fixture/repo");
    expect(html).toContain("fixture-user");
    expect(html).toContain("Issue／PR #7 的评论");
    expect(html).toContain("明确审阅的正文");
    expect(html).not.toContain("<script>");
    expect(githubDraftLabel("outcome_unknown")).toBe("发送结果待核验");
    expect(githubDeliveryLabel("waiting_review")).toBe("等待 PR 审核");
    expect(githubDeliveryLabel("merged")).toBe("PR 已合并");
  });
});
