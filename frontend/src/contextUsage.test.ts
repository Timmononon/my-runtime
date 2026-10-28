import { describe, expect, it } from "vitest";
import { compactTokens, contextOccupancy } from "./contextUsage";
import type { ContextUsage } from "./types";

const usage = (used: number | null, window: number | null) =>
  ({
    status: "estimated",
    used_tokens: used,
    context_window: window,
  }) as ContextUsage;

describe("context occupancy", () => {
  it("uses current context and capacity, rounds and clamps the display", () => {
    expect(contextOccupancy(usage(305000, 1000000))?.percent).toBe(31);
    expect(contextOccupancy(usage(0, 1000000))?.percent).toBe(0);
    expect(contextOccupancy(usage(1100000, 1000000))).toEqual({
      used: 1100000,
      window: 1000000,
      percent: 100,
    });
  });
  it("keeps missing or invalid values unknown", () => {
    for (const value of [
      null,
      usage(null, 1000),
      usage(10, null),
      usage(10, 0),
      usage(-10, 1000),
      usage(NaN, 1000),
      usage(10, Infinity),
    ])
      expect(contextOccupancy(value)).toBeNull();
    expect(
      contextOccupancy({ ...usage(10, 1000), status: "unavailable" }),
    ).toBeNull();
  });
  it("uses compact token labels without converting unknown to zero", () => {
    expect([0, 999, 2000, 7400, 305000, 1000000].map(compactTokens)).toEqual([
      "0",
      "999",
      "2K",
      "7.4K",
      "305K",
      "1M",
    ]);
    expect(compactTokens(null)).toBe("未知");
    expect(compactTokens(NaN)).toBe("未知");
  });
});
