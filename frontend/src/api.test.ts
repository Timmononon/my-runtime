import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, readableError, request } from "./api";

afterEach(() => vi.unstubAllGlobals());

describe("safe provider failures", () => {
  it.each([
    ["authentication", "密钥"],
    ["endpoint", "DNS"],
    ["transport", "网络"],
    ["catalogue", "模型目录"],
    ["protocol", "格式"],
    ["budget", "读取上限"],
  ])(
    "translates %s without reflecting provider text",
    async (category, label) => {
      vi.stubGlobal(
        "fetch",
        vi.fn().mockResolvedValue(
          new Response(
            JSON.stringify({
              error: "provider",
              category,
              detail: "synthetic-private-upstream-value",
            }),
            { status: 502 },
          ),
        ),
      );
      const error = await request<never>(
        "/providers/selected/models/fetch",
      ).catch((value) => value);
      expect(error).toBeInstanceOf(ApiError);
      expect(error.category).toBe(category);
      expect(readableError(error)).toContain(label);
      expect(readableError(error)).not.toContain(
        "synthetic-private-upstream-value",
      );
    },
  );
  it("keeps a bounded HTTP status while rejecting unknown metadata", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            error: "provider",
            category: "catalogue",
            status_code: 404,
          }),
          { status: 502 },
        ),
      ),
    );
    const error = await request<never>(
      "/providers/selected/models/fetch",
    ).catch((value) => value);
    expect(error.upstreamStatus).toBe(404);
    expect(readableError(error)).toContain("HTTP 404");
  });
  it.each([undefined, "synthetic-private-upstream-value", "__proto__"])(
    "handles legacy or unknown category safely",
    async (category) => {
      vi.stubGlobal(
        "fetch",
        vi.fn().mockResolvedValue(
          new Response(
            JSON.stringify({
              error: "provider",
              category,
              status_code: "synthetic-private-upstream-value",
            }),
            { status: 502 },
          ),
        ),
      );
      const error = await request<never>(
        "/providers/selected/models/fetch",
      ).catch((value) => value);
      expect(error.category).toBeNull();
      expect(error.upstreamStatus).toBeNull();
      expect(readableError(error)).toBe("提供商请求未完成，请检查连接后重试。");
    },
  );
});
