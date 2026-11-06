import { useEffect, useState } from "react";
import { ApiError, readableError, request } from "./api";
import type { ModelProvider } from "./types";
import { Modal, Spinner } from "./ui";
import type { TaskTriageDecision, TaskTriageSettings } from "./taskTriageTypes";
import { triageDecisionLabel, triageSettingsPayload } from "./taskTriageTypes";

export default function TaskTriageSettingsModal({
  onClose,
  onSaved,
}: {
  onClose: () => void;
  onSaved: () => void;
}) {
  const [value, setValue] = useState<TaskTriageSettings | null>(null);
  const [providers, setProviders] = useState<ModelProvider[]>([]);
  const [busy, setBusy] = useState(false),
    [loading, setLoading] = useState(true);
  const [error, setError] = useState(""),
    [conflict, setConflict] = useState(false);
  const [reload, setReload] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError("");
    setValue(null);
    void Promise.all([
      request<TaskTriageSettings>("/tasks/triage/settings", {
        signal: controller.signal,
      }),
      request<{ providers: ModelProvider[] }>("/providers", {
        signal: controller.signal,
      }),
    ])
      .then(([settings, catalog]) => {
        if (controller.signal.aborted) return;
        setValue(settings);
        setProviders(catalog.providers);
        setConflict(false);
      })
      .catch((failure) => {
        if (!controller.signal.aborted) setError(readableError(failure));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [reload]);

  const save = async () => {
    if (!value || busy) return;
    setError("");
    setBusy(true);
    try {
      await request<TaskTriageSettings>("/tasks/triage/settings", {
        method: "PATCH",
        body: JSON.stringify(triageSettingsPayload(value)),
      });
      onSaved();
      onClose();
    } catch (failure) {
      setError(readableError(failure));
      if (failure instanceof ApiError && failure.status === 409)
        setConflict(true);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      title="任务判断模型"
      wide
      onClose={() => {
        if (!busy) onClose();
      }}
    >
      <p className="github-note">
        适用于所有 GitHub
        来源的新任务。来源和发送人通过规则检查后，由独立模型二选一判断“处理或忽略”；Worker
        使用各自原有的模型配置。
      </p>
      {loading ? (
        <p className="github-loading" role="status">
          <Spinner />
          正在读取本地配置
        </p>
      ) : (
        value && (
          <form
            onSubmit={(event) => {
              event.preventDefault();
              void save();
            }}
          >
            <TaskTriageFields
              value={value}
              providers={providers}
              disabled={busy || conflict}
              onChange={(changes) =>
                setValue((current) => current && { ...current, ...changes })
              }
            />
            {error && (
              <p className="inline-error" role="alert">
                {error}
              </p>
            )}
            {conflict && (
              <button
                className="button secondary"
                type="button"
                onClick={() => setReload((current) => current + 1)}
              >
                重新读取已保存配置
              </button>
            )}
            <div className="modal-actions">
              <button
                className="button secondary"
                type="button"
                disabled={busy}
                onClick={onClose}
              >
                取消
              </button>
              <button
                className="button primary"
                type="submit"
                disabled={busy || conflict}
              >
                {busy ? "正在保存…" : "保存判断模型"}
              </button>
            </div>
          </form>
        )
      )}
      {!loading && !value && (
        <>
          <p className="inline-error" role="alert">
            {error || "无法读取判断模型设置。"}
          </p>
          <button
            className="button secondary"
            onClick={() => setReload((current) => current + 1)}
          >
            重新读取本地配置
          </button>
        </>
      )}
    </Modal>
  );
}

export function TaskTriageFields({
  value,
  providers,
  onChange,
  disabled,
}: {
  value: TaskTriageSettings;
  providers: ModelProvider[];
  onChange: (changes: Partial<TaskTriageSettings>) => void;
  disabled: boolean;
}) {
  const provider = providers.find((item) => item.id === value.provider_id);
  const model = provider?.models.find((item) => item.id === value.model);
  const efforts = model?.effort_levels?.length ? model.effort_levels : ["none"];
  return (
    <div className="task-triage-settings">
      <label className="github-checkbox">
        <input
          type="checkbox"
          checked={value.enabled}
          disabled={disabled}
          onChange={(event) => onChange({ enabled: event.target.checked })}
        />
        启用新任务判断
      </label>
      <p className="github-note">
        关闭后暂停从 GitHub
        创建新任务。只读来源不调用判断模型；已有任务的后续消息和停止指令继续按原规则处理。
      </p>
      <div className="form-row">
        <label className="field">
          判断模型提供商
          <select
            aria-label="判断模型提供商"
            value={value.provider_id}
            disabled={disabled}
            onChange={(event) => {
              const selected = providers.find(
                (item) => item.id === event.target.value,
              );
              const first = selected?.models[0];
              const levels = first?.effort_levels || [];
              onChange({
                provider_id: event.target.value,
                model: first?.id || "",
                reasoning_effort: levels.includes("none")
                  ? "none"
                  : levels[0] || "none",
              });
            }}
          >
            {!provider && (
              <option value={value.provider_id}>
                {value.provider_id}（当前目录未列出）
              </option>
            )}
            {providers.map((item) => (
              <option key={item.id} value={item.id}>
                {item.name}
                {item.configured ? "" : "（未配置密钥）"}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          判断模型
          <select
            aria-label="判断模型"
            value={value.model}
            disabled={disabled || !provider?.models.length}
            onChange={(event) => {
              const selected = provider?.models.find(
                (item) => item.id === event.target.value,
              );
              const levels = selected?.effort_levels || [];
              onChange({
                model: event.target.value,
                reasoning_effort: levels.includes(value.reasoning_effort)
                  ? value.reasoning_effort
                  : levels.includes("none")
                    ? "none"
                    : levels[0] || "none",
              });
            }}
          >
            {!model && (
              <option value={value.model}>
                {value.model || "请先在设置中配置模型"}
                {value.model ? "（当前目录未列出）" : ""}
              </option>
            )}
            {provider?.models.map((item) => (
              <option key={item.id} value={item.id}>
                {item.name || item.id}
              </option>
            ))}
          </select>
        </label>
      </div>
      <label className="field">
        判断推理强度
        <select
          aria-label="判断推理强度"
          value={value.reasoning_effort}
          disabled={disabled}
          onChange={(event) =>
            onChange({ reasoning_effort: event.target.value })
          }
        >
          {!efforts.includes(value.reasoning_effort) && (
            <option value={value.reasoning_effort}>
              {value.reasoning_effort}（当前目录未列出）
            </option>
          )}
          {efforts.map((effort) => (
            <option key={effort} value={effort}>
              {effort === "none" ? "关闭推理（none）" : effort}
            </option>
          ))}
        </select>
      </label>
      <p className="github-note">
        提供商和密钥沿用“设置 →
        模型提供商”。此处只选择判断专用模型，保存和查看设置都不发送模型请求。
      </p>
      {(!provider?.configured || !model) && (
        <p className="github-error" role="status">
          判断模型尚未就绪。请在模型提供商设置中配置密钥和模型目录；就绪前的新事件会保留为待确认，不创建
          Worker。
        </p>
      )}
      <label className="field">
        自动处理范围
        <textarea
          aria-label="自动处理范围"
          value={value.processing_scope}
          maxLength={8000}
          rows={4}
          disabled={disabled}
          placeholder="例如：处理本仓库的缺陷检查、代码审查和功能修改请求。"
          onChange={(event) =>
            onChange({ processing_scope: event.target.value })
          }
        />
      </label>
      <details className="task-triage-limits">
        <summary>调用预算和输入限制</summary>
        <div className="form-row">
          <label className="field">
            判断超时（秒）
            <input
              aria-label="判断超时（秒）"
              type="number"
              min={1}
              max={120}
              step={1}
              value={value.timeout_seconds}
              disabled={disabled}
              onChange={(event) =>
                onChange({ timeout_seconds: Number(event.target.value) })
              }
            />
          </label>
          <label className="field">
            每小时判断次数
            <input
              aria-label="每小时判断次数"
              type="number"
              min={1}
              max={10000}
              step={1}
              value={value.max_requests_per_hour}
              disabled={disabled}
              onChange={(event) =>
                onChange({ max_requests_per_hour: Number(event.target.value) })
              }
            />
          </label>
        </div>
        <div className="form-row">
          <label className="field">
            输入字符上限
            <input
              aria-label="输入字符上限"
              type="number"
              min={1000}
              max={200000}
              step={1}
              value={value.max_input_chars}
              disabled={disabled}
              onChange={(event) =>
                onChange({ max_input_chars: Number(event.target.value) })
              }
            />
          </label>
          <label className="field">
            输出 Token 上限
            <input
              aria-label="输出 Token 上限"
              type="number"
              min={64}
              max={4096}
              step={1}
              value={value.max_output_tokens}
              disabled={disabled}
              onChange={(event) =>
                onChange({ max_output_tokens: Number(event.target.value) })
              }
            />
          </label>
        </div>
        <label className="field">
          每轮询判断次数
          <input
            aria-label="每轮询判断次数"
            type="number"
            min={1}
            max={100}
            step={1}
            value={value.max_calls_per_poll}
            disabled={disabled}
            onChange={(event) =>
              onChange({ max_calls_per_poll: Number(event.target.value) })
            }
          />
        </label>
        <p className="github-note">
          输入过长、额度耗尽或请求失败会显示判断错误，不作为模型的处理或忽略结果。
        </p>
      </details>
    </div>
  );
}

export function TaskTriageDecisionCard({
  value,
}: {
  value: TaskTriageDecision;
}) {
  return (
    <div
      className={`task-triage-decision ${value.status === "completed" && (value.decision === "process" || value.decision === "ignore") ? value.decision : "unavailable"}`}
      aria-label="模型判断结果"
    >
      <div>
        <strong>模型判断：{triageDecisionLabel(value)}</strong>
        <small>
          {value.provider_id} · {value.model}
        </small>
      </div>
      <p>{value.reason}</p>
      {!!value.questions?.length && (
        <div>
          <span>历史记录中的补充问题</span>
          <ul>
            {value.questions.map((question, index) => (
              <li key={index}>{question}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
