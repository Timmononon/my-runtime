import type { Project, Session } from "./types";

// Historical conversations keep their scope after their project registration
// is removed. Keep them reachable in the independent group.
export function workspaceSessions(
  sessions: Session[],
  projects: Project[],
  project: Project | null,
) {
  const registered = new Set(projects.map((item) => item.id));
  return sessions.filter((session) =>
    project
      ? session.project_id === project.id
      : !session.project_id || !registered.has(session.project_id),
  );
}
