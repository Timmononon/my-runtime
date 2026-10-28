import { useEffect, useState } from "react";
import { post, request, readableError } from "./api";
import BrowserControl from "./BrowserControl";
import { Spinner } from "./ui";
import type {
  ManagedAgent,
  ManagedHook,
  ManagedMCP,
  ModelProvider,
  Permissions,
  RuntimeManagement,
} from "./types";
import SelectionFields from "./SelectionFields";
import EmbeddingSettings from "./EmbeddingSettings";

const lines = (value: string) =>
  value
    .split(/\r?\n/)
    .map((item) => item.trim())
    .filter(Boolean);
const emptyPermissions = (): Permissions => ({
  allowed_tools: null,
  denied_tools: [],
  ask_tools: [],
  auto_approve_tools: [],
});
export function PermissionFields({
  value,
  onChange,
}: {
  value: Permissions;
  onChange: (value: Permissions) => void;
}) {
  return (
    <fieldset className="permission-fields">
      <legend>工具权限</legend>
      <label className="check-field">
        <input
          type="checkbox"
          checked={value.allowed_tools !== null}
          onChange={(e) =>
            onChange({ ...value, allowed_tools: e.target.checked ? [] : null })
          }
        />
        使用允许名单（空名单禁止全部工具）
      </label>
      <div className="runtime-grid">
        {(
          [
            "allowed_tools",
            "denied_tools",
            "ask_tools",
            "auto_approve_tools",
          ] as const
        ).map((key) => (
          <label className="field" key={key}>
            {
              {
                allowed_tools: "允许工具",
                denied_tools: "禁止工具",
                ask_tools: "每次询问",
                auto_approve_tools: "自动批准",
              }[key]
            }
            <textarea
              rows={3}
              placeholder="每行一个工具名称"
              disabled={key === "allowed_tools" && value.allowed_tools === null}
              value={value[key]?.join("\n") || ""}
              onChange={(e) =>
                onChange({ ...value, [key]: lines(e.target.value) })
              }
            />
          </label>
        ))}
      </div>
      <p className="fine-print">
        Agent 与项目只能收紧全局权限；模型和 Skill 选择不能授权工具。
      </p>
    </fieldset>
  );
}
function RefFields({
  label,
  value,
  onChange,
}: {
  label: string;
  value: Record<string, string>;
  onChange: (value: Record<string, string>) => void;
}) {
  const [entries, setEntries] = useState(Object.entries(value));
  useEffect(() => setEntries(Object.entries(value)), [value]);
  const update = (next: [string, string][]) => {
    setEntries(next);
    onChange(Object.fromEntries(next.filter(([name]) => name)));
  };
  return (
    <fieldset className="ref-fields">
      <legend>{label}</legend>
      {entries.map(([name, ref], index) => (
        <div className="ref-row" key={index}>
          <input
            aria-label={`${label}名称 ${index + 1}`}
            value={name}
            onChange={(e) =>
              update(
                entries.map((item, i) =>
                  i === index ? [e.target.value, item[1]] : item,
                ),
              )
            }
          />
          <input
            aria-label={`${label}引用 ${index + 1}`}
            value={ref}
            placeholder="env:NAME 或 keyring:aster/name"
            onChange={(e) =>
              update(
                entries.map((item, i) =>
                  i === index ? [item[0], e.target.value] : item,
                ),
              )
            }
          />
          <button
            className="text-button"
            onClick={() => update(entries.filter((_, i) => i !== index))}
          >
            移除
          </button>
        </div>
      ))}
      <button
        className="text-button"
        onClick={() => setEntries([...entries, ["", ""]])}
      >
        添加{label}
      </button>
    </fieldset>
  );
}
export default function RuntimeSettings({
  section,
  providers,
  onChanged,
  report: parentReport,
  selected,
}: {
  section: string;
  providers: ModelProvider[];
  onChanged: (value: RuntimeManagement) => void;
  report: (error: unknown) => void;
  selected: string | null;
}) {
  const [data, setData] = useState<RuntimeManagement | null>(null),
    [busy, setBusy] = useState(false),
    [notice, setNotice] = useState(""),
    [issue, setIssue] = useState("");
  const report = (error: unknown) => {
    setIssue(readableError(error));
    parentReport(error);
  };
  const [servers, setServers] = useState<ManagedMCP[]>([]),
    [hooks, setHooks] = useState<ManagedHook[]>([]),
    [permissions, setPermissions] = useState<Permissions>(emptyPermissions()),
    [embedding, setEmbedding] = useState<Record<string, unknown>>({}),
    [search, setSearch] = useState<Record<string, unknown>>({}),
    [browser, setBrowser] = useState<Record<string, unknown>>({});
  const [account, setAccount] = useState(""),
    [secret, setSecret] = useState("");
  const accept = (value: RuntimeManagement) => {
    setData(value);
    setServers(value.mcp_servers);
    setHooks(value.hooks);
    setPermissions(value.permissions);
    setEmbedding(value.extensions.embedding);
    setSearch(value.extensions.web_search);
    setBrowser(value.extensions.browser);
    onChanged(value);
  };
  useEffect(() => {
    let live = true;
    request<RuntimeManagement>("/management")
      .then((value) => {
        if (live) accept(value);
      })
      .catch(report);
    return () => {
      live = false;
    };
  }, []);
  const action = async (fn: () => Promise<void>) => {
    setBusy(true);
    setNotice("");
    setIssue("");
    try {
      await fn();
    } catch (error) {
      report(error);
    } finally {
      setBusy(false);
    }
  };
  const pendingHealth =
    section === "mcp" &&
    servers.some((server) =>
      ["connecting", "unavailable", "retrying"].includes(
        server.health?.status || "",
      ),
    );
  const deepseekProviders = providers.filter(
    (item) => item.id === "deepseek" && item.protocol === "deepseek_responses",
  );
  const searchProvider = deepseekProviders.find(
    (item) => item.id === search.provider_id,
  );
  const savedSearch = data?.extensions.web_search || {};
  const savedSearchProvider = deepseekProviders.find(
    (item) => item.id === savedSearch.provider_id,
  );
  const searchSelectionInvalid =
    search.backend === "deepseek" &&
    (!searchProvider?.configured ||
      !searchProvider.models.some((item) => item.id === search.model));
  useEffect(() => {
    if (!pendingHealth) return;
    let live = true;
    const timer = setInterval(() => {
      void request<RuntimeManagement>("/management")
        .then((value) => {
          if (live)
            setServers((old) =>
              old.map((server) => ({
                ...server,
                health:
                  value.mcp_servers.find((item) => item.name === server.name)
                    ?.health || server.health,
              })),
            );
        })
        .catch(report);
    }, 1000);
    return () => {
      live = false;
      clearInterval(timer);
    };
  }, [pendingHealth, section]);
  const save = () =>
    action(async () => {
      if (!data) return;
      const body =
        section === "mcp"
          ? {
              mcp_servers: servers.map(
                ({ health, transport, ...item }) => item,
              ),
            }
          : section === "hooks"
            ? { hooks }
            : section === "permissions"
              ? { permissions }
              : { embedding, web_search: search, browser };
      accept(
        await request<RuntimeManagement>("/management", {
          method: "PATCH",
          body: JSON.stringify({ expected_revision: data.revision, ...body }),
        }),
      );
      setNotice(
        "配置已保存。新任务使用新配置；已入队或运行的任务保留其快照。MCP 与凭证撤销会阻止后续工具派发。",
      );
    });
  if (!data)
    return (
      <div className="loading-history">
        {issue ? (
          <>
            <p className="inline-error" role="alert">
              {issue}
            </p>
            <button
              className="button secondary"
              onClick={() =>
                void action(async () =>
                  accept(await request<RuntimeManagement>("/management")),
                )
              }
            >
              重新读取配置
            </button>
          </>
        ) : (
          <>
            <Spinner />
            正在读取运行配置
          </>
        )}
      </div>
    );
  const title =
    {
      agents: "Agents",
      mcp: "MCP 服务",
      hooks: "Hooks",
      permissions: "工具权限",
      extensions: "搜索、记忆与浏览器",
    }[section] || section;
  const updateServer = (index: number, changes: Partial<ManagedMCP>) =>
    setServers((old) =>
      old.map((item, i) => (i === index ? { ...item, ...changes } : item)),
    );
  const updateHook = (index: number, changes: Partial<ManagedHook>) =>
    setHooks((old) =>
      old.map((item, i) => (i === index ? { ...item, ...changes } : item)),
    );
  return (
    <div className="runtime-settings">
      {issue && (
        <p className="inline-error" role="alert">
          {issue}
        </p>
      )}
      <div className="settings-section-heading">
        <h3>{title}</h3>
        <p>
          配置保存在用户目录。新提交的任务使用新配置，已入队和运行的任务保留快照；后台任务遵守同样的权限。
        </p>
        <button
          className="text-button"
          disabled={busy}
          onClick={() =>
            void action(async () =>
              accept(await request<RuntimeManagement>("/management")),
            )
          }
        >
          重新读取配置
        </button>
      </div>
      {section === "agents" ? (
        <AgentsSettings
          data={data}
          providers={providers}
          onChanged={accept}
          report={report}
        />
      ) : (
        <>
          {section === "mcp" && (
            <>
              <button
                className="button secondary"
                onClick={() =>
                  setServers([
                    ...servers,
                    {
                      name: "",
                      command: "",
                      args: [],
                      env: {},
                      headers: {},
                      timeout_seconds: 60,
                      required: false,
                    },
                  ])
                }
              >
                添加 MCP 服务
              </button>
              {servers.map((server, index) => (
                <article className="settings-card" key={index}>
                  <div className="runtime-card-heading">
                    <h4>{server.name || "新服务"}</h4>
                    <button
                      className="text-button"
                      onClick={() =>
                        setServers(servers.filter((_, i) => i !== index))
                      }
                    >
                      移除服务
                    </button>
                  </div>
                  <label className="field">
                    服务名称
                    <input
                      value={server.name}
                      onChange={(e) =>
                        updateServer(index, { name: e.target.value })
                      }
                    />
                  </label>
                  <label className="field">
                    连接方式
                    <select
                      aria-label="连接方式"
                      value={server.url ? "http" : "stdio"}
                      onChange={(e) =>
                        updateServer(
                          index,
                          e.target.value === "http"
                            ? {
                                command: null,
                                url: "https://",
                                args: [],
                              }
                            : { command: "", url: null },
                        )
                      }
                    >
                      <option value="stdio">本地进程</option>
                      <option value="http">HTTP</option>
                    </select>
                  </label>
                  {server.url ? (
                    <label className="field">
                      MCP 地址
                      <input
                        value={server.url}
                        onChange={(e) =>
                          updateServer(index, { url: e.target.value })
                        }
                      />
                    </label>
                  ) : (
                    <>
                      <label className="field">
                        可执行文件
                        <input
                          value={server.command || ""}
                          onChange={(e) =>
                            updateServer(index, { command: e.target.value })
                          }
                        />
                      </label>
                      <label className="field">
                        启动参数（每行一个）
                        <textarea
                          rows={2}
                          value={server.args?.join("\n") || ""}
                          onChange={(e) =>
                            updateServer(index, { args: lines(e.target.value) })
                          }
                        />
                      </label>
                      <label className="field">
                        工作目录
                        <input
                          value={server.cwd || ""}
                          onChange={(e) =>
                            updateServer(index, { cwd: e.target.value || null })
                          }
                        />
                      </label>
                    </>
                  )}
                  <RefFields
                    label="环境变量"
                    value={server.env || {}}
                    onChange={(env) => updateServer(index, { env })}
                  />
                  <RefFields
                    label="请求头"
                    value={server.headers || {}}
                    onChange={(headers) => updateServer(index, { headers })}
                  />
                  <div className="form-row">
                    <label className="field">
                      请求超时（秒）
                      <input
                        type="number"
                        min="1"
                        value={Number(server.timeout_seconds || 60)}
                        onChange={(e) =>
                          updateServer(index, {
                            timeout_seconds: Number(e.target.value),
                          })
                        }
                      />
                    </label>
                    <label className="field">
                      发现预算（秒）
                      <input
                        type="number"
                        min="0.1"
                        step="0.1"
                        value={Number(server.discovery_timeout_seconds || 2)}
                        onChange={(e) =>
                          updateServer(index, {
                            discovery_timeout_seconds: Number(e.target.value),
                          })
                        }
                      />
                    </label>
                  </div>
                  <label className="check-field">
                    <input
                      type="checkbox"
                      checked={!!server.required}
                      onChange={(e) =>
                        updateServer(index, { required: e.target.checked })
                      }
                    />
                    启动任务必须连接成功
                  </label>
                  <p className="mcp-health" role="status">
                    状态：{server.health?.status || "尚未连接"} · 工具{" "}
                    {server.health?.tool_count ?? "未知"} · 重连{" "}
                    {server.health?.reconnect_attempts ?? 0}
                  </p>
                  <div className="modal-actions">
                    {["diagnose", "reconnect"].map((method) => (
                      <button
                        key={method}
                        className="button secondary"
                        disabled={
                          busy ||
                          !data.mcp_servers.some(
                            (item) => item.name === server.name,
                          )
                        }
                        onClick={() =>
                          void action(async () => {
                            const value = await post<{
                              health: ManagedMCP["health"][];
                            }>(
                              `/management/mcp/${encodeURIComponent(server.name)}/${method}`,
                            );
                            setServers((old) =>
                              old.map((item) => ({
                                ...item,
                                health:
                                  value.health.find(
                                    (health) => health?.name === item.name,
                                  ) || item.health,
                              })),
                            );
                            setNotice("服务状态已更新。");
                          })
                        }
                      >
                        {method === "diagnose" ? "连接诊断" : "重新连接"}
                      </button>
                    ))}
                  </div>
                </article>
              ))}
            </>
          )}
          {section === "hooks" && (
            <>
              <p className="fine-print">
                Hooks
                执行你配置的本地程序，请使用明确的文件路径。保存不会立即运行它。
              </p>
              <button
                className="button secondary"
                onClick={() =>
                  setHooks([
                    ...hooks,
                    {
                      event: "PreToolUse",
                      executable: "",
                      args: [],
                      timeout_seconds: 15,
                      output_limit: 16384,
                      tool_names: null,
                    },
                  ])
                }
              >
                添加 Hook
              </button>
              {hooks.map((hook, index) => (
                <article className="settings-card" key={index}>
                  <div className="runtime-card-heading">
                    <h4>Hook {index + 1}</h4>
                    <button
                      className="text-button"
                      onClick={() =>
                        setHooks(hooks.filter((_, i) => i !== index))
                      }
                    >
                      移除 Hook
                    </button>
                  </div>
                  <label className="field">
                    事件
                    <select
                      aria-label="事件"
                      value={hook.event}
                      onChange={(e) =>
                        updateHook(index, { event: e.target.value })
                      }
                    >
                      {[
                        "PreToolUse",
                        "PostToolUse",
                        "SessionStart",
                        "Stop",
                      ].map((event) => (
                        <option key={event}>{event}</option>
                      ))}
                    </select>
                  </label>
                  <label className="field">
                    程序路径
                    <input
                      value={hook.executable}
                      onChange={(e) =>
                        updateHook(index, { executable: e.target.value })
                      }
                    />
                  </label>
                  <label className="field">
                    参数（每行一个）
                    <textarea
                      rows={2}
                      value={hook.args.join("\n")}
                      onChange={(e) =>
                        updateHook(index, { args: lines(e.target.value) })
                      }
                    />
                  </label>
                  <div className="form-row">
                    <label className="field">
                      超时（秒）
                      <input
                        type="number"
                        min="1"
                        value={hook.timeout_seconds}
                        onChange={(e) =>
                          updateHook(index, {
                            timeout_seconds: Number(e.target.value),
                          })
                        }
                      />
                    </label>
                    <label className="field">
                      输出预算（字节）
                      <input
                        type="number"
                        min="1"
                        value={hook.output_limit}
                        onChange={(e) =>
                          updateHook(index, {
                            output_limit: Number(e.target.value),
                          })
                        }
                      />
                    </label>
                  </div>
                  <label className="field">
                    指定工具（每行一个，留空匹配所有）
                    <textarea
                      rows={2}
                      value={hook.tool_names?.join("\n") || ""}
                      onChange={(e) =>
                        updateHook(index, {
                          tool_names: e.target.value.trim()
                            ? lines(e.target.value)
                            : null,
                        })
                      }
                    />
                  </label>
                </article>
              ))}
            </>
          )}
          {section === "permissions" && (
            <PermissionFields value={permissions} onChange={setPermissions} />
          )}
          {section === "extensions" && (
            <>
              <EmbeddingSettings
                value={embedding}
                saved={data.extensions.embedding}
                status={data.extensions.embedding_status}
                providers={providers}
                busy={busy}
                onChange={setEmbedding}
                onRefresh={() =>
                  void action(async () => {
                    const latest =
                      await request<RuntimeManagement>("/management");
                    setData((current) =>
                      current
                        ? {
                            ...current,
                            extensions: {
                              ...current.extensions,
                              embedding: latest.extensions.embedding,
                              embedding_status:
                                latest.extensions.embedding_status,
                            },
                          }
                        : current,
                    );
                  })
                }
              />
              <article className="settings-card">
                <h4>网络搜索</h4>
                <p className="fine-print" role="status">
                  已保存状态：
                  {!savedSearch.backend
                    ? "未配置，网页搜索不可用。"
                    : savedSearch.backend === "deepseek" &&
                        !savedSearchProvider?.configured
                      ? "已配置，DeepSeek 连接尚未配置密钥。"
                      : savedSearch.backend === "brave" &&
                          !savedSearch.credential_ref
                        ? "已配置，Brave 搜索凭证尚未配置。"
                        : "已配置（尚未在线验证）。"}
                </p>
                <label className="field">
                  搜索后端
                  <select
                    aria-label="搜索后端"
                    value={String(search.backend || "")}
                    onChange={(e) => {
                      const backend = e.target.value;
                      if (backend === "deepseek") {
                        const provider = deepseekProviders[0];
                        setSearch({
                          backend,
                          provider_id: provider?.id || "deepseek",
                          model:
                            provider?.models.find(
                              (item) => item.id === "deepseek-flash",
                            )?.id ||
                            provider?.models[0]?.id ||
                            "deepseek-flash",
                          timeout_seconds: 30,
                          result_limit: 10,
                          max_tokens: 4096,
                          max_uses: 5,
                        });
                      } else {
                        setSearch(
                          backend
                            ? {
                                backend,
                                base_url:
                                  backend === "brave"
                                    ? "https://api.search.brave.com"
                                    : "",
                                credential_ref: null,
                                timeout_seconds: 15,
                                result_limit: 10,
                              }
                            : {},
                        );
                      }
                    }}
                  >
                    <option value="">未配置</option>
                    <option value="deepseek">DeepSeek（使用已有连接）</option>
                    <option value="brave">Brave Search</option>
                    <option value="searxng">SearXNG</option>
                  </select>
                </label>
                {!search.backend && (
                  <p className="fine-print">
                    网页搜索需选择后端并保存；添加聊天模型不会自动开启搜索。
                  </p>
                )}
                {search.backend === "deepseek" && (
                  <>
                    <p className="fine-print">
                      使用已保存的 DeepSeek 官方连接和密钥。每次搜索计入
                      DeepSeek API 用量；保存配置不会发起搜索或验证连接。
                    </p>
                    <label className="field">
                      DeepSeek 搜索连接
                      <select
                        aria-label="DeepSeek 搜索连接"
                        value={String(search.provider_id || "deepseek")}
                        onChange={(e) =>
                          setSearch({ ...search, provider_id: e.target.value })
                        }
                      >
                        {deepseekProviders.map((item) => (
                          <option key={item.id} value={item.id}>
                            {item.name}
                            {item.configured ? "" : "（密钥未配置）"}
                          </option>
                        ))}
                      </select>
                    </label>
                    <label className="field">
                      搜索模型
                      <select
                        aria-label="搜索模型"
                        value={String(search.model || "")}
                        onChange={(e) =>
                          setSearch({ ...search, model: e.target.value })
                        }
                      >
                        {searchProvider?.models.map((item) => (
                          <option key={item.id} value={item.id}>
                            {item.name || item.id}
                          </option>
                        ))}
                      </select>
                    </label>
                    {!searchProvider?.configured && (
                      <p className="fine-print">
                        请先在模型提供商中配置 DeepSeek 密钥。
                      </p>
                    )}
                    {searchProvider?.configured && searchSelectionInvalid && (
                      <p className="fine-print">
                        请在模型提供商中添加所需模型，再明确选择搜索模型。
                      </p>
                    )}
                    <div className="form-row">
                      <label className="field">
                        搜索输出上限（tokens）
                        <input
                          type="number"
                          min="1"
                          max="65536"
                          value={Number(search.max_tokens || 4096)}
                          onChange={(e) =>
                            setSearch({
                              ...search,
                              max_tokens: Number(e.target.value),
                            })
                          }
                        />
                      </label>
                      <label className="field">
                        每次搜索的联网次数上限
                        <input
                          type="number"
                          min="1"
                          max="1000"
                          value={Number(search.max_uses || 5)}
                          onChange={(e) =>
                            setSearch({
                              ...search,
                              max_uses: Number(e.target.value),
                            })
                          }
                        />
                      </label>
                    </div>
                    <label className="field">
                      搜索超时（秒）
                      <input
                        type="number"
                        min="1"
                        max="300"
                        value={Number(search.timeout_seconds || 30)}
                        onChange={(e) =>
                          setSearch({
                            ...search,
                            timeout_seconds: Number(e.target.value),
                          })
                        }
                      />
                    </label>
                  </>
                )}
                {!!search.backend && (
                  <>
                    {search.backend !== "deepseek" && (
                      <>
                        <label className="field">
                          服务地址
                          <input
                            value={String(search.base_url || "")}
                            disabled={search.backend === "brave"}
                            onChange={(e) =>
                              setSearch({
                                ...search,
                                base_url: e.target.value,
                                credential_ref: null,
                              })
                            }
                          />
                        </label>
                        <label className="field">
                          凭证引用
                          <input
                            value={String(search.credential_ref || "")}
                            placeholder="keyring:aster/search"
                            onChange={(e) =>
                              setSearch({
                                ...search,
                                credential_ref: e.target.value || null,
                              })
                            }
                          />
                        </label>
                        <p className="fine-print">
                          Brave 使用独立搜索凭证；SearXNG
                          可按服务要求使用凭证。这里不会复用聊天模型的密钥。
                        </p>
                      </>
                    )}
                    <label className="field">
                      结果数量
                      <input
                        type="number"
                        min="1"
                        max="50"
                        value={Number(search.result_limit || 10)}
                        onChange={(e) =>
                          setSearch({
                            ...search,
                            result_limit: Number(e.target.value),
                          })
                        }
                      />
                    </label>
                  </>
                )}
              </article>
              <article className="settings-card">
                <h4>浏览器连接</h4>
                <label className="field">
                  浏览器模式
                  <select
                    aria-label="浏览器模式"
                    value={String(browser.mode || "isolated")}
                    onChange={(e) =>
                      setBrowser({ ...browser, mode: e.target.value })
                    }
                  >
                    <option value="isolated">独立浏览器</option>
                    <option value="bridge">连接现有调试浏览器</option>
                  </select>
                </label>
                {browser.mode === "bridge" ? (
                  <>
                    <label className="field">
                      本机 CDP 地址
                      <input
                        value={String(browser.cdp_endpoint || "")}
                        placeholder="http://127.0.0.1:9222"
                        onChange={(e) =>
                          setBrowser({
                            ...browser,
                            cdp_endpoint: e.target.value || null,
                          })
                        }
                      />
                    </label>
                    <label className="field">
                      允许的页面 URL（每行一个，精确匹配）
                      <textarea
                        rows={3}
                        value={((browser.allowed_pages as string[]) || []).join(
                          "\n",
                        )}
                        onChange={(e) =>
                          setBrowser({
                            ...browser,
                            allowed_pages: lines(e.target.value),
                          })
                        }
                      />
                    </label>
                    <p className="fine-print">
                      需自行启动开启调试端口的浏览器。仅允许列出的页面，保存不连接浏览器。
                    </p>
                  </>
                ) : (
                  <label className="check-field">
                    <input
                      type="checkbox"
                      checked={browser.headless !== false}
                      onChange={(e) =>
                        setBrowser({ ...browser, headless: e.target.checked })
                      }
                    />
                    隐藏自动化浏览器窗口
                  </label>
                )}
              </article>
              <article className="settings-card">
                <h4>添加服务凭证</h4>
                <label className="field">
                  凭证名称
                  <input
                    value={account}
                    onChange={(e) => setAccount(e.target.value)}
                    placeholder="例如 search"
                  />
                </label>
                <label className="field">
                  密钥（仅写入）
                  <input
                    type="password"
                    autoComplete="new-password"
                    value={secret}
                    onChange={(e) => setSecret(e.target.value)}
                  />
                </label>
                <button
                  className="button secondary"
                  disabled={busy || !account || !secret}
                  onClick={() => {
                    const value = secret;
                    setSecret("");
                    void action(async () => {
                      const saved = await post<{
                        credential_ref: string;
                        revision: string;
                      }>("/management/credentials", {
                        expected_revision: data.revision,
                        account,
                        value,
                      });
                      setNotice(`已保存凭证，引用：${saved.credential_ref}`);
                    });
                  }}
                >
                  保存服务凭证
                </button>
              </article>
              <BrowserControl
                selected={selected}
                revision={data.revision}
                allowedPages={(browser.allowed_pages as string[]) || []}
              />
            </>
          )}
          <div className="modal-actions">
            <button
              className="button primary"
              disabled={
                busy || (section === "extensions" && searchSelectionInvalid)
              }
              onClick={() => void save()}
            >
              {busy ? <Spinner /> : null}保存{title}
            </button>
          </div>
        </>
      )}
      {notice && (
        <p className="soft-notice" role="status">
          {notice}
        </p>
      )}
    </div>
  );
}

function AgentsSettings({
  data,
  providers,
  onChanged,
  report,
}: {
  data: RuntimeManagement;
  providers: ModelProvider[];
  onChanged: (value: RuntimeManagement) => void;
  report: (error: unknown) => void;
}) {
  const [chosen, setChosen] = useState<ManagedAgent | null>(
      data.agents[0] || null,
    ),
    [id, setId] = useState(data.agents[0]?.id || ""),
    [name, setName] = useState(data.agents[0]?.name || ""),
    [instructions, setInstructions] = useState(
      data.agents[0]?.instructions || "",
    ),
    [selection, setSelection] = useState(
      data.agents[0]?.selection || {
        provider_id: "deepseek",
        model: "deepseek-flash",
        reasoning_effort: "high",
        selected_skill: null,
      },
    ),
    [permissions, setPermissions] = useState(
      data.agents[0]?.permissions || emptyPermissions(),
    ),
    [skills, setSkills] = useState<{ name: string }[]>([]),
    [busy, setBusy] = useState(false);
  const fill = (agent: ManagedAgent | null) => {
    setChosen(agent);
    setId(agent?.id || "");
    setName(agent?.name || "");
    setInstructions(agent?.instructions || "");
    setSelection(agent?.selection || data.agents[0]?.selection || selection);
    setPermissions(agent?.permissions || emptyPermissions());
  };
  useEffect(() => {
    request<{ installed: { name: string }[] }>("/customization/skills")
      .then((value) => setSkills(value.installed))
      .catch(report);
  }, []);
  const save = async () => {
    setBusy(true);
    try {
      const value = await request<RuntimeManagement>(
        chosen
          ? `/management/agents/${encodeURIComponent(chosen.id)}`
          : "/management/agents",
        {
          method: chosen ? "PUT" : "POST",
          body: JSON.stringify({
            expected_revision: chosen ? chosen.revision : data.revision,
            ...(!chosen ? { id } : {}),
            name,
            instructions,
            selection,
            permissions,
          }),
        },
      );
      onChanged(value);
      fill(value.agents.find((agent) => agent.id === id) || null);
    } catch (error) {
      report(error);
    } finally {
      setBusy(false);
    }
  };
  return (
    <>
      <div className="agent-list">
        {data.agents.map((agent) => (
          <button
            className={`button ${chosen?.id === agent.id ? "primary" : "secondary"}`}
            key={agent.id}
            onClick={() => fill(agent)}
          >
            {agent.name || agent.id}
          </button>
        ))}
        <button className="button secondary" onClick={() => fill(null)}>
          创建 Agent
        </button>
      </div>
      <article className="settings-card">
        <h4>{chosen ? `编辑 ${chosen.name || chosen.id}` : "创建 Agent"}</h4>
        <div className="form-row">
          <label className="field">
            Agent ID
            <input
              disabled={!!chosen}
              value={id}
              onChange={(e) => setId(e.target.value)}
              placeholder="research"
            />
          </label>
          <label className="field">
            显示名称
            <input value={name} onChange={(e) => setName(e.target.value)} />
          </label>
        </div>
        <label className="field">
          Agent 指令
          <textarea
            rows={6}
            value={instructions}
            onChange={(e) => setInstructions(e.target.value)}
          />
        </label>
        <SelectionFields
          value={selection}
          onChange={setSelection}
          providers={providers}
          skills={skills}
        />
        <PermissionFields value={permissions} onChange={setPermissions} />
        {chosen && (
          <details>
            <summary>当前有效权限</summary>
            <p className="fine-print">
              允许：
              {chosen.effective_permissions.allowed_tools === null
                ? "由全局配置决定"
                : chosen.effective_permissions.allowed_tools.join("、") || "无"}
              <br />
              禁止：
              {chosen.effective_permissions.denied_tools.join("、") || "无"}
              <br />
              每次询问：
              {chosen.effective_permissions.ask_tools.join("、") || "无"}
            </p>
          </details>
        )}
        <div className="modal-actions">
          <button
            className="button primary"
            disabled={
              busy || !id || !name || !selection.provider_id || !selection.model
            }
            onClick={() => void save()}
          >
            {busy ? <Spinner /> : null}保存 Agent
          </button>
        </div>
      </article>
    </>
  );
}
