import type { ModelProvider, RuntimeManagement } from "./types";

type EmbeddingStatus = RuntimeManagement["extensions"]["embedding_status"];

export function embeddingStatusText(status: EmbeddingStatus, local: boolean) {
  if (!status || status.status === "disabled") return "未启用";
  if (status.status !== "ready") return "尚未配置就绪";
  return local ? "配置就绪" : "配置就绪（尚未在线验证）";
}

export function localEmbeddingStatusText(status: EmbeddingStatus) {
  if (status?.configured === false)
    return "请检查模型目录，需要完整的配置、分词器和 Safetensors 权重。";
  if (status?.local?.dependencies_available === false)
    return "缺少本地推理依赖，请安装 Aster 的 local-embedding 扩展。";
  const state = status?.local?.state;
  if (state === "loading") return "正在加载模型";
  if (state === "ready")
    return `模型已加载${status?.local?.device ? ` · ${status.local.device}` : ""}`;
  if (state === "error") {
    const explanations: Record<string, string> = {
      missing_dependencies: "本地推理依赖不可用",
      invalid_model: "模型目录不完整或格式不受支持",
      local_device_unavailable: "所选推理设备不可用",
      local_out_of_memory: "内存或显存不足，请调小批量或 token 上限",
      local_load_failed: "模型加载失败，请检查模型文件和依赖",
      local_inference_failed: "本地推理失败，请检查设备和模型配置",
      local_worker_failed: "模型进程已停止，请重新查询",
      local_budget: "推理数据超出限制，请调小批量",
      configuration_changed: "模型文件或配置已变化，请重新查询",
      dependency: "本地推理依赖不可用",
      dependencies: "本地推理依赖不可用",
      unavailable: "本地模型暂不可用",
      model: "模型加载失败，请检查本地模型目录",
      configuration: "请检查本地模型配置",
      device: "所选推理设备不可用",
      timeout: "模型加载或推理超时",
      budget: "本次推理超出资源限制",
    };
    return (
      explanations[status?.local?.error_code || ""] ||
      "本地推理失败，请检查模型、依赖和设备"
    );
  }
  if (state === "closed") return "模型已释放，下次查询时重新加载";
  return "尚未加载，首次语义查询时加载模型";
}

export default function EmbeddingSettings({
  value,
  saved,
  status,
  providers,
  busy,
  onChange,
  onRefresh,
}: {
  value: Record<string, unknown>;
  saved: Record<string, unknown>;
  status: EmbeddingStatus;
  providers: ModelProvider[];
  busy: boolean;
  onChange: (value: Record<string, unknown>) => void;
  onRefresh: () => void;
}) {
  const local = value.backend === "local";
  const savedLocal = saved.backend === "local";
  return (
    <article className="settings-card embedding-settings">
      <h4>语义记忆</h4>
      <label className="check-field">
        <input
          type="checkbox"
          checked={!!value.enabled}
          onChange={(e) => onChange({ ...value, enabled: e.target.checked })}
        />
        启用语义检索
      </label>
      <label className="field">
        Embedding 运行方式
        <select
          aria-label="Embedding 运行方式"
          value={String(value.backend || "remote")}
          onChange={(e) =>
            onChange({
              ...value,
              backend: e.target.value,
              ...(e.target.value === "local" && !value.model_path
                ? { batch_size: 4, timeout_seconds: 180 }
                : {}),
            })
          }
        >
          <option value="remote">远程 API</option>
          <option value="local">本地模型</option>
        </select>
      </label>
      <p className="fine-print">
        {local
          ? "使用本机模型生成向量，记忆文本不发送到云端。保存设置不会加载或下载模型，首次语义查询时才加载。"
          : "语义查询会将当前作用域的记忆文本发送到所选提供商；保存设置不发请求，每次查询另行授权。"}
      </p>
      <div className="embedding-saved-status" role="status">
        <p className="fine-print">
          已保存状态（{savedLocal ? "本地模型" : "远程 API"}）：
          {embeddingStatusText(status, savedLocal)}
        </p>
        {savedLocal && (
          <p className="fine-print">{localEmbeddingStatusText(status)}</p>
        )}
      </div>
      <button
        type="button"
        className="text-button"
        disabled={busy}
        onClick={onRefresh}
      >
        刷新模型状态
      </button>
      {local ? (
        <>
          <label className="field">
            本地模型目录
            <input
              aria-label="本地模型目录"
              value={String(value.model_path || "")}
              placeholder="已下载的模型目录完整路径"
              onChange={(e) =>
                onChange({ ...value, model_path: e.target.value || null })
              }
            />
            <small>
              填写 SentenceTransformers 模型目录，需有配置、分词器和 Safetensors
              权重文件。
            </small>
          </label>
          <div className="form-row">
            <label className="field">
              推理设备
              <select
                aria-label="推理设备"
                value={String(value.device || "auto")}
                onChange={(e) => onChange({ ...value, device: e.target.value })}
              >
                <option value="auto">自动选择</option>
                <option value="cpu">CPU</option>
                <option value="cuda">NVIDIA GPU（CUDA）</option>
              </select>
            </label>
            <label className="field">
              每条文本 token 上限
              <input
                type="number"
                min="32"
                max="8192"
                value={Number(value.local_max_tokens ?? 1024)}
                onChange={(e) =>
                  onChange({
                    ...value,
                    local_max_tokens: Number(e.target.value),
                  })
                }
              />
            </label>
          </div>
          <p className="fine-print">
            GPU 是否可用以实际加载结果为准。超过 token
            上限的文本会截断，查询结果会说明；较小的批量可降低内存和显存占用。
          </p>
        </>
      ) : (
        <>
          <label className="field">
            Embedding 提供商
            <select
              aria-label="Embedding 提供商"
              value={String(value.provider_id || "")}
              onChange={(e) =>
                onChange({ ...value, provider_id: e.target.value || null })
              }
            >
              <option value="">未配置</option>
              {providers
                .filter((item) =>
                  ["openai_chat", "openai_responses"].includes(item.protocol),
                )
                .map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.name}
                  </option>
                ))}
            </select>
          </label>
          <label className="field">
            Embedding 模型 ID
            <input
              value={String(value.model || "")}
              onChange={(e) =>
                onChange({ ...value, model: e.target.value || null })
              }
            />
          </label>
        </>
      )}
      <div className="form-row">
        <label className="field">
          每批文本数
          <input
            type="number"
            min="1"
            max="128"
            value={Number(value.batch_size ?? 32)}
            onChange={(e) =>
              onChange({ ...value, batch_size: Number(e.target.value) })
            }
          />
        </label>
        <label className="field">
          请求超时（秒）
          <input
            type="number"
            min="1"
            max="300"
            value={Number(value.timeout_seconds ?? 30)}
            onChange={(e) =>
              onChange({ ...value, timeout_seconds: Number(e.target.value) })
            }
          />
        </label>
      </div>
      <div className="form-row">
        <label className="field">
          候选扫描上限
          <input
            type="number"
            min="1"
            max="10000"
            value={Number(value.scan_limit ?? 2000)}
            onChange={(e) =>
              onChange({ ...value, scan_limit: Number(e.target.value) })
            }
          />
        </label>
        <label className="field">
          排序上限
          <input
            type="number"
            min="1"
            max="200"
            value={Number(value.rank_limit ?? 200)}
            onChange={(e) =>
              onChange({ ...value, rank_limit: Number(e.target.value) })
            }
          />
        </label>
      </div>
    </article>
  );
}
