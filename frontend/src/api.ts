import type { Bootstrap, RuntimeEvent } from "./types";

const providerErrors: Record<string, string> = {
  authentication: "提供商密钥未配置或未通过验证，请检查已保存的密钥。",
  endpoint: "提供商地址或 DNS 校验未通过，请检查地址、代理或 VPN 设置。",
  transport: "无法连接提供商，请检查网络后重试。",
  catalogue: "供应商未返回可读取的模型目录，可以重试或手动添加模型 ID。",
  protocol: "供应商返回的模型目录格式无法读取，可以手动添加模型 ID。",
  budget: "供应商模型目录超过读取上限，可以手动添加所需模型 ID。",
};

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
    public category: string | null = null,
    public upstreamStatus: number | null = null,
  ) {
    super(message);
  }
}
export function readableError(error: unknown) {
  if (error instanceof ApiError && error.status === 401)
    return "连接已过期，请在终端运行 aster web 重新连接。";
  if (error instanceof ApiError && error.status === 403)
    return "当前操作没有获得授权，或文件超出了会话范围。";
  return error instanceof Error ? error.message : "操作未完成，请稍后重试。";
}
export async function request<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const headers = new Headers(options.headers);
  headers.set("X-Aster-UI", "1");
  if (typeof options.body === "string")
    headers.set("Content-Type", "application/json");
  const response = await fetch(path, {
    ...options,
    headers,
    credentials: "same-origin",
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    if (body.error === "provider") {
      const category = Object.hasOwn(providerErrors, body.category)
        ? body.category
        : null;
      const upstreamStatus =
        Number.isInteger(body.status_code) &&
        body.status_code >= 100 &&
        body.status_code <= 599
          ? body.status_code
          : null;
      throw new ApiError(
        response.status,
        (category
          ? providerErrors[category]
          : "提供商请求未完成，请检查连接后重试。") +
          (upstreamStatus ? `（HTTP ${upstreamStatus}）` : ""),
        category,
        upstreamStatus,
      );
    }
    throw new ApiError(
      response.status,
      body.detail || body.error || `请求未完成 (${response.status})`,
    );
  }
  return response.status === 204 ? (undefined as T) : response.json();
}
export const post = <T>(path: string, body: unknown = {}) =>
  request<T>(path, { method: "POST", body: JSON.stringify(body) });
export async function connect(): Promise<Bootstrap> {
  const params = new URLSearchParams(window.location.hash.slice(1));
  const ticket = params.get("ticket");
  if (ticket) {
    window.history.replaceState(
      null,
      "",
      window.location.pathname + window.location.search,
    );
    await post("/ui/auth", { ticket });
  }
  return request<Bootstrap>("/ui/bootstrap");
}
export function parseSSE(frame: string): RuntimeEvent | null {
  const data = frame
    .split("\n")
    .filter((line) => line.startsWith("data:"))
    .map((line) => line.slice(5).trimStart())
    .join("\n");
  if (!data) return null;
  const value = JSON.parse(data);
  return typeof value.event_id === "number" && typeof value.type === "string"
    ? value
    : null;
}
export async function streamRun(
  id: string,
  after: number,
  onEvent: (event: RuntimeEvent) => void,
  signal: AbortSignal,
) {
  const response = await fetch(
    `/runs/${encodeURIComponent(id)}/events?after=${after}`,
    {
      credentials: "same-origin",
      headers: { "X-Aster-UI": "1", Accept: "text/event-stream" },
      signal,
    },
  );
  if (!response.ok)
    throw new ApiError(response.status, `执行流连接失败 (${response.status})`);
  if (!response.body) throw new Error("当前浏览器不支持流式读取。");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const { done, value } = await reader.read();
      buffer = (buffer + decoder.decode(value, { stream: !done })).replace(
        /\r\n/g,
        "\n",
      );
      let end: number;
      while ((end = buffer.indexOf("\n\n")) >= 0) {
        const frame = buffer.slice(0, end);
        buffer = buffer.slice(end + 2);
        const event = parseSSE(frame);
        if (event) onEvent(event);
      }
      if (done) break;
    }
  } finally {
    reader.releaseLock();
  }
}
export async function download(id: string, filename: string) {
  const response = await fetch(`/artifacts/${encodeURIComponent(id)}`, {
    credentials: "same-origin",
    headers: { "X-Aster-UI": "1" },
  });
  if (!response.ok) throw new ApiError(response.status, "文件下载失败");
  const url = URL.createObjectURL(await response.blob());
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
export function redact(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(redact);
  if (value && typeof value === "object")
    return Object.fromEntries(
      Object.entries(value).map(([key, item]) => [
        key,
        /api.?key|password|authorization|cookie|secret|token|credential/i.test(
          key,
        )
          ? "••••••"
          : redact(item),
      ]),
    );
  return typeof value === "string"
    ? value.replace(/\bsk-[A-Za-z0-9_-]{16,}/g, "[已隐藏密钥]")
    : value;
}
