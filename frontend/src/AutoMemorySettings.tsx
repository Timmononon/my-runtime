import type { ModelProvider, RuntimeManagement } from "./types";

export const defaultAutoMemory = {
  enabled: false,
  model: "jev-1.14.0",
  threshold: 0.7,
  timeout_seconds: 30,
  input_chars: 12000,
  max_facts: 5,
  credential_ref: "keyring:aster/jev",
  extractor_model: "deepseek-flash",
};

export default function AutoMemorySettings({
  value,
  onChange,
  status,
  providers,
  secret,
  onSecretChange,
  onSaveKey,
  busy,
}: {
  value: Record<string, unknown>;
  onChange: (value: Record<string, unknown>) => void;
  status: RuntimeManagement["extensions"]["auto_memory_status"];
  providers: ModelProvider[];
  secret: string;
  onSecretChange: (value: string) => void;
  onSaveKey: () => void;
  busy: boolean;
}) {
  const config = { ...defaultAutoMemory, ...value };
  const provider = providers.find(
    (item) => item.id === "deepseek" && item.protocol === "deepseek_responses",
  );
  const update = (changes: Record<string, unknown>) =>
    onChange({ ...config, ...changes });
  return (
    <article className="settings-card auto-memory-settings">
      <h4>自动记忆 · Jev</h4>
      <label className="check-field">
        <input
          type="checkbox"
          checked={!!config.enabled}
          onChange={(event) => update({ enabled: event.target.checked })}
        />
        每轮聊天后自动判断并保存记忆
      </label>
      <p className="fine-print">
        启用并保存后，每轮正常聊天结束会将本轮用户消息、少量前文和助手回答发送给
        TypeSafe 的 Jev 判断；通过后由 DeepSeek
        提取事实，保存有用户原文依据的记忆。 会产生 API
        用量，已有工具权限仍然有效。
      </p>
      <p className="fine-print">
        每轮只检查新增内容，不重新扫描全部历史。超时或失败会跳过本轮；自动化和临时侧聊不参与。
        保存设置不会发送模型请求。
      </p>
      <p className="fine-print" role="status">
        已保存状态：
        {status?.status === "ready"
          ? "已启用，配置就绪（尚未在线验证）"
          : status?.status === "unconfigured"
            ? "已启用，配置不完整"
            : "未启用"}
        。Jev 密钥{status?.gate_configured ? "已配置" : "未配置"}；DeepSeek
        提取模型
        {status?.extractor_configured ? "已配置" : "未配置"}。
      </p>
      <label className="field">
        Jev API Key
        <input
          type="password"
          autoComplete="new-password"
          value={secret}
          placeholder="输入或替换 TypeSafe API Key"
          onChange={(event) => onSecretChange(event.target.value)}
        />
      </label>
      <button
        type="button"
        className="button secondary"
        disabled={busy || !secret.trim()}
        onClick={onSaveKey}
      >
        保存 Jev 密钥
      </button>
      <p className="fine-print">
        密钥单独保存在本机，保存后不回显。先保存密钥，再点击页面下方的保存按钮。
      </p>
      <div className="form-row">
        <label className="field">
          Jev 判断模型
          <input
            value={String(config.model)}
            maxLength={200}
            onChange={(event) => update({ model: event.target.value })}
          />
        </label>
        <label className="field">
          记忆判断阈值
          <input
            type="number"
            min="0"
            max="1"
            step="0.05"
            value={Number(config.threshold)}
            onChange={(event) =>
              update({ threshold: Number(event.target.value) })
            }
          />
        </label>
      </div>
      <label className="field">
        DeepSeek 记忆提取模型
        <select
          aria-label="DeepSeek 记忆提取模型"
          value={String(config.extractor_model)}
          onChange={(event) => update({ extractor_model: event.target.value })}
        >
          {!provider?.models.some(
            (item) => item.id === config.extractor_model,
          ) && (
            <option value={String(config.extractor_model)}>
              {String(config.extractor_model)}（尚未加入模型目录）
            </option>
          )}
          {provider?.models.map((item) => (
            <option key={item.id} value={item.id}>
              {item.name || item.id}
            </option>
          ))}
        </select>
      </label>
      {!provider?.configured && (
        <p className="fine-print">请先在模型设置中配置 DeepSeek 密钥。</p>
      )}
      <label className="field">
        自动记忆超时（秒）
        <input
          type="number"
          min="1"
          max="120"
          value={Number(config.timeout_seconds)}
          onChange={(event) =>
            update({ timeout_seconds: Number(event.target.value) })
          }
        />
      </label>
    </article>
  );
}
