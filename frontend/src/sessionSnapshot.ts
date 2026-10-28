import { currentRun } from "./state";
import { activeStatus } from "./ui";
import type { Run, SessionDetail } from "./types";

type Snapshot = { detail: SessionDetail; run: Run | null };

// A terminal run can be newer than the preceding transcript response. Read
// history after observing completion before allowing polling/streaming to stop.
export async function readSessionSnapshot(
  readDetail: () => Promise<SessionDetail>,
  readRun: (id: string) => Promise<Run>,
  valid: () => boolean,
): Promise<Snapshot | { retry: true } | null> {
  let detail = await readDetail();
  if (!valid()) return null;
  for (let attempt = 0; attempt < 3; attempt++) {
    const candidate = currentRun(detail.runs);
    if (!candidate) return { detail, run: null };
    const live = await readRun(candidate.id);
    if (!valid()) return null;
    if (activeStatus(live.status)) return { detail, run: live };
    const confirmed = await readDetail();
    if (!valid()) return null;
    const latest = currentRun(confirmed.runs);
    if (
      latest?.id === live.id &&
      latest.status === live.status &&
      latest.updated_at === live.updated_at
    )
      return { detail: confirmed, run: live };
    // A queued task may start as the previous one finishes. Resolve its live
    // state instead of overwriting it with the old terminal run.
    detail = confirmed;
  }
  return { retry: true };
}
