import { useEffect, useState } from "react";
import { post, readableError, request } from "./api";
import { Icon } from "./icons";
import { Empty, Modal, Spinner } from "./ui";
import type { ModelProvider, ProviderModel, ProviderProtocol } from "./types";
import type { ReactNode } from "react";
import RuntimeSettings from "./RuntimeSettings";
import type { RuntimeManagement } from "./types";

const protocolEfforts = (protocol: string) =>
  protocol === "deepseek_responses"
    ? ["none", "low", "high", "max"]
    : ["none", "minimal", "low", "medium", "high", "xhigh"];

function ModelCapabilities({
  model,
  protocol,
  disabled,
  onChange,
}: {
  model: ProviderModel;
  protocol: string;
  disabled: boolean;
  onChange: (value: ProviderModel) => void;
}) {
  return (
    <details className="model-capabilities">
      <summary>
        模型能力 <Icon name="down" size={12} />
      </summary>
      <p className="fine-print">
        以下是你声明的能力，不代表在线验证。仅勾选该模型文档明确支持的选项；不勾选推理强度时，不发送该参数。
      </p>
      <fieldset disabled={disabled}>
        <legend>支持的推理强度</legend>
        <div className="capability-checks">
          {protocolEfforts(protocol).map((effort) => (
            <label key={effort}>
              <input
                type="checkbox"
                checked={model.effort_levels?.includes(effort) || false}
                onChange={(event) =>
                  onChange({
                    ...model,
                    effort_levels: event.target.checked
                      ? [...(model.effort_levels || []), effort]
                      : (model.effort_levels || []).filter(
                          (value) => value !== effort,
                        ),
                  })
                }
              />
              {effort}
            </label>
          ))}
        </div>
      </fieldset>
      <div className="form-row">
        <label className="field">
          上下文上限（tokens）
          <input
            type="number"
            min="1"
            max="10000000"
            step="1"
            value={model.context_window || ""}
            placeholder="未知"
            disabled={disabled}
            onChange={(event) =>
              onChange({
                ...model,
                context_window: event.target.value
                  ? Number(event.target.value)
                  : undefined,
              })
            }
          />
        </label>
        <label className="field">
          输出上限（tokens）
          <input
            type="number"
            min="1"
            max="10000000"
            step="1"
            value={model.max_output_tokens || ""}
            placeholder="未知"
            disabled={disabled}
            onChange={(event) =>
              onChange({
                ...model,
                max_output_tokens: event.target.value
                  ? Number(event.target.value)
                  : undefined,
              })
            }
          />
        </label>
      </div>
      <label className="capability-image">
        <input
          type="checkbox"
          disabled={disabled}
          checked={model.input_modalities?.includes("image") || false}
          onChange={(event) =>
            onChange({
              ...model,
              input_modalities: event.target.checked
                ? ["text", "image"]
                : ["text"],
            })
          }
        />
        支持图片输入
      </label>
    </details>
  );
}

export default function ProviderSettings({
  onClose,
  onChanged,
  report,
  archive,
  onManagementChanged,
  selected,
}: {
  onClose: () => void;
  onChanged: (
    providers: ModelProvider[],
    protocols: ProviderProtocol[],
  ) => void;
  report: (error: unknown) => void;
  archive: ReactNode;
  onManagementChanged: (value: RuntimeManagement) => void;
  selected: string | null;
}) {
  const [section, setSection] = useState("providers");
  const [providers, setProviders] = useState<ModelProvider[]>([]),
    [protocols, setProtocols] = useState<ProviderProtocol[]>([]),
    [chosen, setChosen] = useState<string | null>(null),
    [creating, setCreating] = useState(false),
    [loading, setLoading] = useState(true),
    [busy, setBusy] = useState(false),
    [remove, setRemove] = useState(false),
    [notice, setNotice] = useState(""),
    [catalogIssue, setCatalogIssue] = useState("");
  const [name, setName] = useState(""),
    [base, setBase] = useState(""),
    [protocol, setProtocol] = useState(""),
    [key, setKey] = useState(""),
    [models, setModels] = useState<ProviderModel[]>([]),
    [modelId, setModelId] = useState(""),
    [modelName, setModelName] = useState("");
  const current = providers.find((p) => p.id === chosen),
    endpointChanged =
      !!current && (current.base_url !== base || current.protocol !== protocol);
  const unsaved =
    !!current &&
    (current.name !== name ||
      endpointChanged ||
      JSON.stringify(current.models) !== JSON.stringify(models) ||
      !!key);
  const fill = (provider?: ModelProvider) => {
    setChosen(provider?.id || null);
    setCreating(!provider);
    setName(provider?.name || "");
    setBase(provider?.base_url || "");
    setProtocol(provider?.protocol || protocols[0]?.id || "");
    setKey("");
    setModels(provider?.models.map((m) => ({ ...m })) || []);
    setModelId("");
    setModelName("");
    setRemove(false);
    setNotice("");
    setCatalogIssue("");
  };
  const refresh = async (select?: string) => {
    const result = await request<{
      providers: ModelProvider[];
      protocols: ProviderProtocol[];
    }>("/providers");
    setProviders(result.providers);
    setProtocols(result.protocols);
    onChanged(result.providers, result.protocols);
    if (select) {
      const provider = result.providers.find((p) => p.id === select);
      if (provider) fill(provider);
    }
    return result;
  };
  useEffect(() => {
    let active = true;
    void request<{ providers: ModelProvider[]; protocols: ProviderProtocol[] }>(
      "/providers",
    )
      .then((result) => {
        if (!active) return;
        setProviders(result.providers);
        setProtocols(result.protocols);
        onChanged(result.providers, result.protocols);
        if (result.providers[0]) fill(result.providers[0]);
        else {
          setCreating(true);
          setProtocol(result.protocols[0]?.id || "");
        }
      })
      .catch(report)
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
      setKey("");
    };
  }, []);
  const save = async () => {
    setBusy(true);
    setNotice("");
    try {
      const body = {
        name: name.trim(),
        base_url: base.trim(),
        protocol,
        models,
        ...(key ? { api_key: key } : {}),
        ...(current ? { expected_revision: current.revision } : {}),
      };
      const result = current
        ? await request<ModelProvider>(
            `/providers/${encodeURIComponent(current.id)}`,
            { method: "PATCH", body: JSON.stringify(body) },
          )
        : await post<ModelProvider>("/providers", body);
      await refresh(result.id);
      setCreating(false);
      setNotice("已保存。后续运行将使用新的配置。");
    } catch (error) {
      report(error);
    } finally {
      setKey("");
      setBusy(false);
    }
  };
  const fetchModels = async () => {
    if (!current) return;
    setBusy(true);
    setNotice("");
    setCatalogIssue("");
    try {
      await post(`/providers/${encodeURIComponent(current.id)}/models/fetch`, {
        expected_revision: current.revision,
      });
      await refresh(current.id);
      setNotice("已读取并更新供应商返回的模型目录。");
    } catch (error) {
      setCatalogIssue(readableError(error));
      report(error);
    } finally {
      setBusy(false);
    }
  };
  const clearKey = async () => {
    if (!current) return;
    setBusy(true);
    try {
      await request(`/providers/${encodeURIComponent(current.id)}/key`, {
        method: "DELETE",
        body: JSON.stringify({ expected_revision: current.revision }),
      });
      await refresh(current.id);
      setNotice("已移除保存的密钥。");
    } catch (error) {
      report(error);
    } finally {
      setKey("");
      setBusy(false);
    }
  };
  const deleteProvider = async () => {
    if (!current) return;
    setBusy(true);
    try {
      await request(
        `/providers/${encodeURIComponent(current.id)}?expected_revision=${current.revision}`,
        { method: "DELETE" },
      );
      const result = await refresh();
      fill(result.providers[0]);
      setRemove(false);
    } catch (error) {
      report(error);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal title="设置" onClose={onClose} className="settings-modal">
      <div className="settings-shell">
        <nav
          className="settings-nav"
          role="tablist"
          aria-label="设置类别"
          aria-orientation="vertical"
        >
          <button
            role="tab"
            aria-selected={section === "providers"}
            aria-controls="settings-content"
            onClick={() => setSection("providers")}
          >
            <Icon name="globe" size={16} />
            <span>模型提供商</span>
          </button>
          <button
            role="tab"
            aria-selected={section === "archive"}
            aria-controls="settings-content"
            onClick={() => setSection("archive")}
          >
            <Icon name="folder" size={16} />
            <span>已归档</span>
          </button>
          {[
            ["agents", "Agents", "chat"],
            ["mcp", "MCP 服务", "grid"],
            ["hooks", "Hooks", "bolt"],
            ["permissions", "工具权限", "shield"],
            ["extensions", "搜索与记忆", "search"],
          ].map(([id, label, icon]) => (
            <button
              key={id}
              role="tab"
              aria-selected={section === id}
              aria-controls="settings-content"
              onClick={() => setSection(id)}
            >
              <Icon name={icon} size={16} />
              <span>{label}</span>
            </button>
          ))}
        </nav>
        <div
          id="settings-content"
          className="settings-content"
          role="tabpanel"
          aria-label={
            (
              {
                providers: "模型提供商",
                archive: "已归档",
                agents: "Agents",
                mcp: "MCP 服务",
                hooks: "Hooks",
                permissions: "工具权限",
                extensions: "搜索与记忆",
              } as Record<string, string>
            )[section]
          }
        >
          {section === "archive" ? (
            archive
          ) : section !== "providers" ? (
            <RuntimeSettings
              selected={selected}
              key={section}
              section={section}
              providers={providers}
              onChanged={onManagementChanged}
              report={report}
            />
          ) : (
            <div className="provider-settings">
              <div className="settings-section-heading">
                <h3>模型提供商</h3>
                <p>
                  管理 API 连接与模型。密钥保存到用户目录的
                  credentials.yaml，不会回显。
                </p>
              </div>
              {loading ? (
                <div className="loading-history">
                  <Spinner />
                  读取配置
                </div>
              ) : (
                <>
                  <div className="provider-picker-row">
                    <select
                      aria-label="选择提供商"
                      value={creating ? "" : chosen || ""}
                      disabled={busy}
                      onChange={(event) =>
                        fill(
                          providers.find(
                            (provider) => provider.id === event.target.value,
                          ),
                        )
                      }
                    >
                      <option value="" disabled>
                        添加提供商
                      </option>
                      {providers.map((provider) => (
                        <option key={provider.id} value={provider.id}>
                          {provider.name}
                        </option>
                      ))}
                    </select>
                    <button
                      className="button secondary"
                      onClick={() => fill()}
                      disabled={busy}
                    >
                      <Icon name="plus" size={15} />
                      添加提供商
                    </button>
                  </div>
                  {current || creating ? (
                    <>
                      <section
                        className="settings-card provider-connection-card"
                        aria-label="提供商连接"
                      >
                        <div className="provider-form-title">
                          <div className="provider-identity">
                            <h4>{creating ? "新提供商" : current?.name}</h4>
                            <span className="provider-credential-state">
                              {current?.configured
                                ? "密钥已配置"
                                : "尚未配置密钥"}
                            </span>
                          </div>
                          {current && !current.builtin && (
                            <button
                              className="icon-button"
                              aria-label="删除提供商"
                              title="删除提供商"
                              disabled={busy}
                              onClick={() => setRemove(true)}
                            >
                              <Icon name="trash" size={15} />
                            </button>
                          )}
                        </div>
                        <label className="field provider-key-field">
                          <span className="settings-field-label">
                            API Key
                            <span className="field-note">
                              {endpointChanged
                                ? "连接已改变 · 需要新密钥"
                                : current?.configured
                                  ? "已配置 · 留空保留原密钥"
                                  : "尚未配置"}
                            </span>
                          </span>
                          <input
                            type="password"
                            autoComplete="new-password"
                            name="provider-key-write-only"
                            value={key}
                            onChange={(event) => setKey(event.target.value)}
                            placeholder={
                              current?.configured
                                ? "输入新密钥以替换"
                                : "输入密钥，仅用于本机保存"
                            }
                            disabled={busy}
                            spellCheck={false}
                          />
                        </label>
                        {endpointChanged && (
                          <p className="soft-notice">
                            API
                            地址或协议已改变。原密钥不会转发到新地址；请重新输入密钥，否则保存时会清除原绑定。
                          </p>
                        )}
                        {current?.configured && (
                          <button
                            className="text-button key-remove"
                            disabled={busy}
                            onClick={() => void clearKey()}
                          >
                            移除保存的密钥
                          </button>
                        )}
                        {current?.credential_source === "environment" && (
                          <p className="fine-print">
                            当前来自启动环境。保存新密钥会写入
                            credentials.yaml；移除密钥仅断开此提供商，不修改系统环境变量。
                          </p>
                        )}
                        <details
                          className="provider-advanced"
                          key={current?.id || "new"}
                          open={creating ? true : undefined}
                        >
                          <summary>
                            自定义连接设置
                            <Icon name="down" size={13} />
                          </summary>
                          <div className="provider-advanced-fields">
                            <label className="field settings-field-row">
                              <span className="settings-field-label">
                                显示名称
                              </span>
                              <input
                                value={name}
                                onChange={(event) =>
                                  setName(event.target.value)
                                }
                                placeholder="例如 我的 DeepSeek"
                                disabled={busy}
                              />
                            </label>
                            <label className="field settings-field-row">
                              <span className="settings-field-label">
                                API 协议
                              </span>
                              <select
                                value={protocol}
                                onChange={(event) => {
                                  setProtocol(event.target.value);
                                  const allowed = protocolEfforts(
                                    event.target.value,
                                  );
                                  setModels((old) =>
                                    old.map((model) => ({
                                      ...model,
                                      effort_levels: (
                                        model.effort_levels || []
                                      ).filter((value) =>
                                        allowed.includes(value),
                                      ),
                                    })),
                                  );
                                }}
                                disabled={busy || current?.builtin}
                              >
                                <option value="" disabled>
                                  选择协议
                                </option>
                                {protocols.map((value) => (
                                  <option
                                    value={value.id}
                                    key={value.id}
                                    disabled={!value.supported}
                                  >
                                    {value.label}
                                  </option>
                                ))}
                              </select>
                            </label>
                            <label className="field settings-field-row">
                              <span className="settings-field-label">
                                API 地址
                              </span>
                              <input
                                type="url"
                                value={base}
                                onChange={(event) =>
                                  setBase(event.target.value)
                                }
                                placeholder="https://api.example.com/v1"
                                disabled={busy || current?.builtin}
                              />
                            </label>
                            <p className="fine-print">
                              {current?.builtin
                                ? "内置 DeepSeek 的地址和协议固定；可编辑名称、模型和密钥。"
                                : "仅支持公共 HTTPS 地址（443 端口）；暂不支持本地或内网服务。"}
                            </p>
                          </div>
                        </details>
                      </section>
                      <section
                        className="settings-card provider-models-card"
                        aria-label="模型目录"
                      >
                        <div className="model-catalog-heading">
                          <div>
                            <h4>模型目录</h4>
                            <span>{models.length} 个模型</span>
                          </div>
                          <button
                            className="button secondary"
                            disabled={
                              busy || !current || unsaved || !current.configured
                            }
                            onClick={() => void fetchModels()}
                            title={
                              !current
                                ? "请先保存提供商"
                                : unsaved
                                  ? "请先保存配置变更"
                                  : "显式请求供应商模型目录，不发送生成请求"
                            }
                          >
                            <Icon name="refresh" size={14} />
                            获取模型
                          </button>
                        </div>
                        {catalogIssue && (
                          <div className="catalog-issue">
                            <p className="inline-error" role="alert">
                              {catalogIssue}
                            </p>
                            <p className="fine-print">
                              已有模型仍然保留。可再次获取目录，或在下方手动添加供应商支持的模型
                              ID。
                            </p>
                          </div>
                        )}
                        {!models.length && (
                          <p className="fine-print">
                            可手动添加模型
                            ID；保存连接并配置密钥后，也可显式获取目录。
                          </p>
                        )}
                        <div className="model-catalog-list">
                          {models.map((model) => (
                            <div className="model-catalog-entry" key={model.id}>
                              <div className="model-catalog-row">
                                <span>
                                  <strong>{model.name || model.id}</strong>
                                  {model.name && <code>{model.id}</code>}
                                  <small>
                                    {model.effort_levels?.length
                                      ? `推理：${model.effort_levels.join(" / ")}`
                                      : "未声明可调推理能力"}
                                    {model.context_window
                                      ? ` · 上下文 ${model.context_window.toLocaleString()}`
                                      : ""}
                                  </small>
                                </span>
                                <button
                                  className="icon-button"
                                  aria-label={`移除模型 ${model.id}`}
                                  disabled={busy}
                                  onClick={() =>
                                    setModels((old) =>
                                      old.filter(
                                        (item) => item.id !== model.id,
                                      ),
                                    )
                                  }
                                >
                                  <Icon name="close" size={14} />
                                </button>
                              </div>
                              <ModelCapabilities
                                model={model}
                                protocol={protocol}
                                disabled={busy}
                                onChange={(value) =>
                                  setModels((old) =>
                                    old.map((item) =>
                                      item.id === value.id ? value : item,
                                    ),
                                  )
                                }
                              />
                            </div>
                          ))}
                        </div>
                        <div className="manual-model">
                          <input
                            aria-label="手动添加模型 ID"
                            placeholder="模型 ID"
                            value={modelId}
                            onChange={(event) => setModelId(event.target.value)}
                            disabled={busy}
                          />
                          <input
                            aria-label="模型显示名称"
                            placeholder="显示名称（可选）"
                            value={modelName}
                            onChange={(event) =>
                              setModelName(event.target.value)
                            }
                            disabled={busy}
                          />
                          <button
                            className="icon-button"
                            aria-label="添加模型到目录"
                            disabled={
                              busy ||
                              !modelId.trim() ||
                              models.some(
                                (model) => model.id === modelId.trim(),
                              )
                            }
                            onClick={() => {
                              setModels((old) => [
                                ...old,
                                {
                                  id: modelId.trim(),
                                  ...(modelName.trim()
                                    ? { name: modelName.trim() }
                                    : {}),
                                  effort_levels: [],
                                },
                              ]);
                              setModelId("");
                              setModelName("");
                            }}
                          >
                            <Icon name="plus" size={17} />
                          </button>
                        </div>
                        <p className="fine-print">
                          手动添加不会验证模型能力；可在每个模型的折叠区声明能力。
                        </p>
                      </section>
                      {notice && (
                        <p className="success-notice" role="status">
                          <Icon name="check" size={14} />
                          {notice}
                        </p>
                      )}
                      <div className="provider-save">
                        <button
                          className="button primary"
                          disabled={
                            busy || !name.trim() || !base.trim() || !protocol
                          }
                          onClick={() => void save()}
                        >
                          {busy ? <Spinner /> : <Icon name="check" size={15} />}
                          保存提供商
                        </button>
                      </div>
                    </>
                  ) : (
                    <Empty
                      icon="globe"
                      title="添加第一个提供商"
                      detail="保存连接后，在输入框旁选择模型。"
                    />
                  )}
                  {remove && (
                    <div className="provider-delete-confirm" role="alert">
                      <p>
                        移除 {current?.name} 的配置和凭证？已有运行记录会保留。
                      </p>
                      <button
                        className="button secondary"
                        onClick={() => setRemove(false)}
                      >
                        取消
                      </button>
                      <button
                        className="button danger"
                        disabled={busy}
                        onClick={() => void deleteProvider()}
                      >
                        确认移除
                      </button>
                    </div>
                  )}
                </>
              )}
            </div>
          )}
        </div>
      </div>
    </Modal>
  );
}
