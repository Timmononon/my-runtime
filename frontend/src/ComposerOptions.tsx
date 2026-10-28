import { useState } from "react";
import { Icon } from "./icons";
import { Modal, Popup } from "./ui";
import type { ModelProvider, RunSelection, Skill } from "./types";

export function ModelOptions({
  providers,
  selection,
  onChange,
  busy,
  disabled = false,
}: {
  providers: ModelProvider[];
  selection: RunSelection;
  onChange: (next: RunSelection) => void;
  busy: boolean;
  disabled?: boolean;
}) {
  const [open, setOpen] = useState<"model" | "effort" | null>(null);
  const provider = providers.find((p) => p.id === selection.provider_id),
    model = provider?.models.find((m) => m.id === selection.model),
    efforts = model?.effort_levels || [];
  const labels: Record<string, string> = {
    none: "关闭",
    minimal: "最低",
    low: "低",
    medium: "中",
    high: "高",
    xhigh: "极高",
    max: "最高",
    ultra: "极致",
  };
  return (
    <div className="model-options">
      <div className="option-anchor">
        <button
          type="button"
          className="model-picker"
          aria-label="选择模型"
          disabled={disabled}
          aria-expanded={open === "model"}
          onClick={() => setOpen(open === "model" ? null : "model")}
          title={
            busy
              ? "用于下一次新请求，不改变运行中的任务"
              : `${provider?.name || "尚未选择提供商"} · ${selection.model || "未选择模型"}`
          }
        >
          <Icon name="globe" size={13} />
          <span>{model?.name || selection.model || "选择模型"}</span>
          <Icon name="down" size={11} />
        </button>
        {open === "model" && !disabled && (
          <Popup
            label="模型选择"
            onClose={() => setOpen(null)}
            className="model-popup"
          >
            <div className="popup-heading">
              选择模型<span>下一次新请求生效</span>
            </div>
            {providers.length ? (
              providers.map((p) => (
                <div className="model-option-group" key={p.id}>
                  <p>
                    {p.name}
                    {!p.configured && <small>密钥未配置</small>}
                  </p>
                  {p.models.length ? (
                    p.models.map((m) => (
                      <button
                        key={m.id}
                        type="button"
                        onClick={() => {
                          onChange({
                            ...selection,
                            provider_id: p.id,
                            model: m.id,
                            reasoning_effort: m.effort_levels?.includes(
                              selection.reasoning_effort || "",
                            )
                              ? selection.reasoning_effort
                              : null,
                          });
                          setOpen(null);
                        }}
                      >
                        <span>
                          {m.name || m.id}
                          {m.name && <small>{m.id}</small>}
                        </span>
                        {selection.provider_id === p.id &&
                          selection.model === m.id && (
                            <Icon name="check" size={14} />
                          )}
                      </button>
                    ))
                  ) : (
                    <div className="popup-empty">先在设置中添加或获取模型</div>
                  )}
                </div>
              ))
            ) : (
              <div className="popup-empty">请先在设置中添加模型提供商。</div>
            )}
          </Popup>
        )}
      </div>
      <div className="option-anchor">
        <button
          type="button"
          className="effort-picker"
          aria-label="选择推理强度"
          disabled={disabled || !efforts.length}
          aria-expanded={open === "effort"}
          title={
            efforts.length
              ? "仅展示模型明确支持的推理强度"
              : "当前模型未声明可调推理能力"
          }
          onClick={() => setOpen(open === "effort" ? null : "effort")}
        >
          <Icon name="bolt" size={13} />
          <span>
            {efforts.length
              ? selection.reasoning_effort
                ? labels[selection.reasoning_effort] ||
                  selection.reasoning_effort
                : "默认"
              : "不可调"}
          </span>
          <Icon name="down" size={10} />
        </button>
        {open === "effort" && !disabled && (
          <Popup
            label="推理强度选择"
            onClose={() => setOpen(null)}
            className="effort-popup"
          >
            <div className="popup-heading">推理强度</div>
            {[null, ...efforts].map((effort) => (
              <button
                key={effort || "default"}
                type="button"
                onClick={() => {
                  onChange({ ...selection, reasoning_effort: effort });
                  setOpen(null);
                }}
              >
                <span>{effort ? labels[effort] || effort : "模型默认"}</span>
                {selection.reasoning_effort === effort && (
                  <Icon name="check" size={14} />
                )}
              </button>
            ))}
          </Popup>
        )}
      </div>
    </div>
  );
}

export function ComposerMenu({
  onUpload,
  onCompact,
  onSummarize,
  onSkills,
  canCompact,
  compactReason,
  disabled,
}: {
  onUpload: () => void;
  onCompact: () => void;
  onSummarize: () => void;
  onSkills: () => void;
  canCompact: boolean;
  compactReason: string;
  disabled: boolean;
}) {
  const [open, setOpen] = useState(false);
  return (
    <div className="option-anchor">
      <button
        type="button"
        className="icon-button composer-plus"
        aria-label="更多输入选项"
        aria-expanded={open}
        disabled={disabled}
        onClick={() => setOpen(!open)}
      >
        <Icon name="plus" size={21} />
      </button>
      {open && (
        <Popup
          label="输入选项"
          onClose={() => setOpen(false)}
          className="composer-popup"
        >
          <button
            type="button"
            onClick={() => {
              setOpen(false);
              onUpload();
            }}
          >
            <Icon name="attach" size={16} />
            <span>添加附件</span>
          </button>
          <button
            type="button"
            disabled={!canCompact}
            title={compactReason}
            onClick={() => {
              setOpen(false);
              onCompact();
            }}
          >
            <Icon name="compact" size={16} />
            <span>
              压缩对话
              <small>
                {canCompact ? "本地整理上下文，原始记录保留" : compactReason}
              </small>
            </span>
          </button>
          <button
            type="button"
            disabled={!canCompact}
            title={compactReason}
            onClick={() => {
              setOpen(false);
              onSummarize();
            }}
          >
            <Icon name="compact" size={16} />
            <span>
              模型摘要压缩
              <small>
                {canCompact
                  ? "调用当前模型整理上下文，计入 API 用量"
                  : compactReason}
              </small>
            </span>
          </button>
          <button
            type="button"
            onClick={() => {
              setOpen(false);
              onSkills();
            }}
          >
            <Icon name="grid" size={16} />
            <span>
              选择 Skill<small>从当前范围可用的已安装技能中选择</small>
            </span>
          </button>
        </Popup>
      )}
    </div>
  );
}

export function SkillPicker({
  skills,
  current,
  loading,
  onChoose,
  onClose,
}: {
  skills: Skill[];
  current: string | null;
  loading: boolean;
  onChoose: (name: string | null) => void;
  onClose: () => void;
}) {
  return (
    <Modal title="选择 Skill" onClose={onClose}>
      <p className="modal-description">
        仅列出已安装且当前 Agent /
        项目可用的技能。选择用于下一次新请求，不会改动正在执行的任务。
      </p>
      <div className="skill-chooser">
        <button
          onClick={() => onChoose(null)}
          className={!current ? "selected" : ""}
        >
          <Icon name="chat" size={18} />
          <span>
            不指定 Skill<small>让 Aster 按任务需要使用工具</small>
          </span>
          {!current && <Icon name="check" size={15} />}
        </button>
        {loading ? (
          <p className="fine-print">正在读取可用 Skills…</p>
        ) : skills.length ? (
          skills.map((skill) => (
            <button
              key={skill.name}
              className={current === skill.name ? "selected" : ""}
              onClick={() => onChoose(skill.name)}
            >
              <Icon name="grid" size={18} />
              <span>
                {skill.name}
                <small>{skill.description}</small>
              </span>
              {current === skill.name && <Icon name="check" size={15} />}
            </button>
          ))
        ) : (
          <p className="fine-print">
            当前范围没有可选 Skill。可先前往侧栏 Skills 安装，再返回选择。
          </p>
        )}
      </div>
    </Modal>
  );
}
