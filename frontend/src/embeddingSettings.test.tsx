import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import EmbeddingSettings, {
  embeddingStatusText,
  localEmbeddingStatusText,
} from "./EmbeddingSettings";
import type { RuntimeManagement } from "./types";

type Status = NonNullable<RuntimeManagement["extensions"]["embedding_status"]>;
const ready: Status = {
  status: "ready",
  available: true,
  configured: true,
  backend: "local",
  local: {
    state: "unloaded",
    device: null,
    error_code: null,
    dependencies_available: true,
  },
};

describe("embedding settings display", () => {
  it("distinguishes saved configuration from a model actually loaded on a device", () => {
    expect(embeddingStatusText(ready, true)).toBe("配置就绪");
    expect(localEmbeddingStatusText(ready)).toContain("尚未加载");
    expect(
      localEmbeddingStatusText({
        ...ready,
        local: { ...ready.local!, state: "loading" },
      }),
    ).toBe("正在加载模型");
    expect(
      localEmbeddingStatusText({
        ...ready,
        local: { ...ready.local!, state: "ready", device: "cpu" },
      }),
    ).toBe("模型已加载 · cpu");
    expect(
      localEmbeddingStatusText({
        ...ready,
        local: { ...ready.local!, dependencies_available: false },
      }),
    ).toContain("缺少本地推理依赖");
  });

  it("keeps a local draft separate from the saved remote status without showing remote credential fields", () => {
    const html = renderToStaticMarkup(
      <EmbeddingSettings
        value={{
          backend: "local",
          model_path: "D:/Models/bge-m3",
          device: "auto",
        }}
        saved={{ backend: "remote" }}
        status={{ status: "disabled", available: false, configured: false }}
        providers={[]}
        busy={false}
        onChange={() => {}}
        onRefresh={() => {}}
      />,
    );
    expect(html).toContain("本地模型目录");
    expect(html).toContain("记忆文本不发送到云端");
    expect(html).toContain("已保存状态（远程 API）：未启用");
    expect(html).not.toContain("Embedding 提供商");
    expect(html).not.toContain("Embedding 模型 ID");
    expect(html).toContain("GPU 是否可用以实际加载结果为准");
  });

  it("defaults legacy settings to remote and retains the external authorization disclosure", () => {
    const html = renderToStaticMarkup(
      <EmbeddingSettings
        value={{}}
        saved={{}}
        status={undefined}
        providers={[]}
        busy={false}
        onChange={() => {}}
        onRefresh={() => {}}
      />,
    );
    expect(html).toContain("Embedding 提供商");
    expect(html).toContain("每次查询另行授权");
    expect(html).not.toContain("本地模型目录");
    expect(embeddingStatusText(ready, false)).toContain("尚未在线验证");
  });

  it("does not display arbitrary backend error strings as user-visible source text", () => {
    const status = {
      ...ready,
      local: {
        ...ready.local!,
        state: "error" as const,
        error_code: "private text",
      },
    };
    expect(localEmbeddingStatusText(status)).toContain("本地推理失败");
    expect(localEmbeddingStatusText(status)).not.toContain("private text");
  });
});
