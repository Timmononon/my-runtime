import type { Lineage, Session } from "./types";

type Reader = <T>(path: string) => Promise<T>;

/** Discover saved side chats through the existing paginated API, without
 * placing them in the sidebar or guessing their kind from their title. */
export async function readSideChats(
  parentId: string,
  read: Reader,
  valid: () => boolean,
): Promise<Session[] | null> {
  const list = async (include: boolean): Promise<Session[] | null> => {
    const sessions = new Map<string, Session>();
    // 100 is the server's page size, not a cap on the number of conversations.
    for (let offset = 0; valid(); offset += 100) {
      const page = await read<{ sessions: Session[] }>(
        `/sessions?include_side_chats=${include}&state=active&limit=100&offset=${offset}`,
      );
      if (!valid()) return null;
      for (const item of page.sessions) sessions.set(item.id, item);
      if (page.sessions.length < 100) return [...sessions.values()];
    }
    return null;
  };
  const [all, ordinary] = await Promise.all([list(true), list(false)]);
  if (!valid() || !all || !ordinary) return null;
  const ordinaryIds = new Set(ordinary.map((item) => item.id));
  const result: Session[] = [];
  for (const session of all) {
    if (ordinaryIds.has(session.id)) continue;
    const { lineage } = await read<{ lineage: Lineage | null }>(
      `/sessions/${encodeURIComponent(session.id)}/lineage`,
    );
    if (!valid()) return null;
    if (lineage?.kind === "side_chat" && lineage.parent_id === parentId)
      result.push({ ...session, lineage });
  }
  return result;
}
