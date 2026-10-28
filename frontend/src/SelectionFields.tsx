import type { ModelProvider, RunSelection } from "./types";

export default function SelectionFields({
  value,
  onChange,
  providers,
  skills,
  disabled = false,
}: {
  value: RunSelection;
  onChange: (value: RunSelection) => void;
  providers: ModelProvider[];
  skills: { name: string }[];
  disabled?: boolean;
}) {
  const provider = providers.find((item) => item.id === value.provider_id),
    model = provider?.models.find((item) => item.id === value.model);
  return (
    <fieldset className="selection-fields" disabled={disabled}>
      <legend>模型与工作流</legend>
      <div className="form-row">
        <label className="field">
          提供商
          <select
            aria-label="提供商"
            value={value.provider_id}
            onChange={(e) => {
              const p = providers.find((item) => item.id === e.target.value);
              onChange({
                ...value,
                provider_id: e.target.value,
                model: p?.models[0]?.id || "",
                reasoning_effort: null,
              });
            }}
          >
            {!provider && (
              <option value={value.provider_id}>
                {value.provider_id || "选择提供商"}
              </option>
            )}
            {providers.map((item) => (
              <option key={item.id} value={item.id}>
                {item.name}
                {!item.configured ? " · 未配置密钥" : ""}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          模型
          <select
            aria-label="模型"
            value={value.model}
            onChange={(e) =>
              onChange({
                ...value,
                model: e.target.value,
                reasoning_effort: null,
              })
            }
          >
            {!model && (
              <option value={value.model}>{value.model || "选择模型"}</option>
            )}
            {provider?.models.map((item) => (
              <option key={item.id} value={item.id}>
                {item.name || item.id}
              </option>
            ))}
          </select>
        </label>
      </div>
      <div className="form-row">
        <label className="field">
          推理强度
          <select
            aria-label="推理强度"
            value={value.reasoning_effort || ""}
            onChange={(e) =>
              onChange({ ...value, reasoning_effort: e.target.value || null })
            }
          >
            <option value="">模型默认</option>
            {model?.effort_levels?.map((item) => (
              <option key={item} value={item}>
                {item}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          固定 Skill
          <select
            aria-label="固定 Skill"
            value={value.selected_skill || ""}
            onChange={(e) =>
              onChange({ ...value, selected_skill: e.target.value || null })
            }
          >
            <option value="">不选择</option>
            {value.selected_skill &&
              !skills.some((item) => item.name === value.selected_skill) && (
                <option value={value.selected_skill}>
                  {value.selected_skill} · 当前不可用
                </option>
              )}
            {skills.map((item) => (
              <option key={item.name} value={item.name}>
                {item.name}
              </option>
            ))}
          </select>
        </label>
      </div>
    </fieldset>
  );
}
