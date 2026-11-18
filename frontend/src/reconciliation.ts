import type { Run, SessionDetail } from "./types";

export const RECONCILIATION_NOTICE =
  "部分操作的结果尚不确定。请先在 Runtime 中核对执行结果，避免重复产生副作用。";

export function runRequiresReconciliation(
  run: Pick<Run, "status" | "requires_reconciliation"> | null | undefined,
  runtimeCapabilities?: readonly string[],
  session?: Pick<SessionDetail, "requires_reconciliation"> | null,
): boolean {
  if (
    Array.isArray(runtimeCapabilities) &&
    runtimeCapabilities.every((value) => typeof value === "string") &&
    runtimeCapabilities.includes("execution_reconciliation_status_v1")
  ) {
    const flags = [
      session?.requires_reconciliation,
      run?.requires_reconciliation,
    ];
    // A newer session or run snapshot can reveal an ancestor's unknown effect.
    // A true value wins over a stale false value during refresh.
    if (flags.some((value) => value === true)) return true;
    if (
      flags.every(
        (value) => value === undefined || typeof value === "boolean",
      ) &&
      flags.some((value) => value === false)
    )
      return false;
  }
  return run?.status === "needs_reconciliation";
}
