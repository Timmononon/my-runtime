import { ApiError } from "./api";
import type { Project, Session, SessionDetail } from "./types";

export type ProjectSessionSnapshot = {
  detail: SessionDetail;
  hasAttachments: boolean;
  hasMailbox: boolean;
  hasLineage: boolean;
  hasAutomation: boolean;
};

function matchesProject(session: Session, project: Project, agent: string) {
  return (
    session.project_id === project.id &&
    session.agent_id === agent &&
    session.cwd === project.cwd &&
    session.title === "" &&
    session.archived_at == null &&
    session.deleted_at == null &&
    !session.lineage
  );
}

// Sidebar pagination is independent of this lookup. Recheck server content and
// local drafts before reusing an empty session, including after delayed reads.
export async function findEmptyProjectSession({
  project,
  preferred,
  blocked,
  valid,
  listPage,
  readSession,
  agent = "default",
}: {
  project: Project;
  preferred: Session | null;
  blocked: (id: string) => boolean;
  valid: () => boolean;
  listPage: (offset: number) => Promise<Session[]>;
  readSession: (id: string) => Promise<ProjectSessionSnapshot>;
  agent?: string;
}): Promise<SessionDetail | null | undefined> {
  const seen = new Set<string>();
  const inspect = async (session: Session) => {
    if (seen.has(session.id)) return null;
    seen.add(session.id);
    if (!matchesProject(session, project, agent) || blocked(session.id))
      return null;
    let value: ProjectSessionSnapshot;
    try {
      value = await readSession(session.id);
    } catch (error) {
      if (error instanceof ApiError && error.status === 404) return null;
      throw error;
    }
    if (!valid()) return undefined;
    const detail = value.detail;
    return matchesProject(detail, project, agent) &&
      !blocked(detail.id) &&
      !detail.messages.length &&
      !detail.runs.length &&
      detail.plan === null &&
      !value.hasAttachments &&
      !value.hasMailbox &&
      !value.hasLineage &&
      !value.hasAutomation
      ? detail
      : null;
  };
  if (!valid()) return undefined;
  if (preferred) {
    const existing = await inspect(preferred);
    if (!valid() || existing === undefined) return undefined;
    if (existing) return existing;
  }
  for (let offset = 0; valid();) {
    const page = await listPage(offset);
    if (!valid()) return undefined;
    for (const session of page) {
      const existing = await inspect(session);
      if (!valid() || existing === undefined) return undefined;
      if (existing) return existing;
    }
    if (page.length < 100) return null;
    offset += page.length;
  }
  return undefined;
}
