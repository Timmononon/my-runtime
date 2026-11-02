import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import AutoMemorySettings, { defaultAutoMemory } from "./AutoMemorySettings";

describe("automatic memory settings", () => {
  const props = {
    value: { ...defaultAutoMemory },
    onChange: () => {},
    providers: [],
    secret: "",
    onSecretChange: () => {},
    onSaveKey: () => {},
    busy: false,
  };
  it("discloses external processing and defaults off without claiming verification", () => {
    const html = renderToStaticMarkup(
      <AutoMemorySettings {...props} status={undefined} />,
    );
    expect(html).toContain("TypeSafe");
    expect(html).toContain("DeepSeek");
    expect(html).toContain("保存有用户原文依据的记忆");
    expect(html).toContain("会产生 API 用量");
    expect(html).toContain("不重新扫描全部历史");
    expect(html).toContain('type="password"');
    expect(html).not.toContain('checked=""');
  });
  it("distinguishes configured from online validation and keeps secrets out of defaults", () => {
    const html = renderToStaticMarkup(
      <AutoMemorySettings
        {...props}
        value={{ ...defaultAutoMemory, enabled: true }}
        status={{
          enabled: true,
          configured: true,
          gate_configured: true,
          extractor_configured: true,
          status: "ready",
          missing: [],
        }}
      />,
    );
    expect(html).toContain("配置就绪（尚未在线验证）");
    expect(html).toContain('checked=""');
    expect(defaultAutoMemory).not.toHaveProperty("api_key");
  });
});
