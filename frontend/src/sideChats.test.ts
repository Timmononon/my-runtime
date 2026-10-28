import { describe, expect, it } from "vitest";
import { readSideChats } from "./sideChats";
import type { Lineage, Session } from "./types";

const session = (id: string): Session => ({
  id,
  title: "侧边聊天 · 名称不代表类型",
  cwd: "D:/workspace",
  agent_id: "default",
  project_id: null,
  updated_at: 1,
});
const relation = (parent: string): Lineage => ({
  parent_id: parent,
  kind: "side_chat",
  boundary_seq: 2,
  closed_at: null,
});

describe("side chat discovery outside the ordinary sidebar", () => {
  it("finds an older side chat across pages, using lineage to isolate its parent", async () => {
    const ordinary = Array.from({ length: 105 }, (_, index) =>
      session(`ordinary-${index}`),
    );
    const all = [
      ...ordinary,
      session("other-project-side"),
      session("saved-side"),
    ];
    const readPaths: string[] = [];
    const read = async <T>(path: string): Promise<T> => {
      readPaths.push(path);
      const url = new URL(path, "http://localhost");
      if (url.pathname === "/sessions") {
        const items =
          url.searchParams.get("include_side_chats") === "true"
            ? all
            : ordinary;
        const offset = Number(url.searchParams.get("offset"));
        return { sessions: items.slice(offset, offset + 100) } as T;
      }
      return {
        lineage: relation(
          path.includes("other-project-side") ? "other-parent" : "parent",
        ),
      } as T;
    };
    expect(
      (await readSideChats("parent", read, () => true))?.map((item) => item.id),
    ).toEqual(["saved-side"]);
    expect(
      readPaths.filter((path) => path.includes("offset=100")),
    ).toHaveLength(2);
    expect(readPaths.filter((path) => path.endsWith("/lineage"))).toEqual([
      "/sessions/other-project-side/lineage",
      "/sessions/saved-side/lineage",
    ]);
  });
  it("propagates catalog failures instead of reporting no saved side chat", async () => {
    await expect(
      readSideChats(
        "parent",
        async () => {
          throw new Error("catalog unavailable");
        },
        () => true,
      ),
    ).rejects.toThrow("catalog unavailable");
  });
  it("discards a late response when the main conversation changes", async () => {
    let current = true;
    const read = async <T>(path: string): Promise<T> => {
      if (path.endsWith("/lineage")) {
        current = false;
        return { lineage: relation("parent") } as T;
      }
      return {
        sessions: path.includes("include_side_chats=true")
          ? [session("saved-side")]
          : [],
      } as T;
    };
    expect(await readSideChats("parent", read, () => current)).toBeNull();
  });
});
