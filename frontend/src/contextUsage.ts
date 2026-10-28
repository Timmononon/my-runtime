import type { ContextUsage } from "./types";

export function contextOccupancy(value: ContextUsage | null) {
  const used = value?.used_tokens,
    window = value?.context_window;
  if (
    value?.status !== "estimated" ||
    typeof used !== "number" ||
    typeof window !== "number" ||
    !Number.isFinite(used) ||
    !Number.isFinite(window) ||
    used < 0 ||
    window <= 0
  )
    return null;
  return {
    used,
    window,
    percent: Math.min(100, Math.round((used / window) * 100)),
  };
}

export function compactTokens(value: number | null) {
  if (value == null || !Number.isFinite(value) || value < 0) return "未知";
  if (value < 1000) return String(Math.round(value));
  const scaled = value / (value >= 1_000_000 ? 1_000_000 : 1000);
  return `${scaled >= 100 ? Math.round(scaled) : Math.round(scaled * 10) / 10}${value >= 1_000_000 ? "M" : "K"}`;
}
