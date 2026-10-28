import { useEffect, useRef, useState } from "react";
import { request } from "./api";
import { compactTokens, contextOccupancy } from "./contextUsage";
import type { ContextUsage } from "./types";
import { Popup } from "./ui";

const rows = [
  { key: "system_tokens", label: "系统提示词", color: "system" },
  { key: "tools_tokens", label: "工具定义", color: "tools" },
  { key: "message_tokens", label: "对话消息", color: "messages" },
  { key: "calibration_tokens", label: "估算修正", color: "calibration" },
] as const;
const circumference = 2 * Math.PI * 5.5;

export default function ContextMeter({
  sessionId,
  revision,
  model,
  providerId,
  active,
}: {
  sessionId: string | null;
  revision: string;
  model: string;
  providerId: string;
  active: boolean;
}) {
  const [value, setValue] = useState<ContextUsage | null>(null),
    [open, setOpen] = useState(false);
  const anchor = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    let current = true;
    let sequence = 0;
    if (!sessionId) {
      setValue(null);
      return;
    }
    const refresh = () => {
      const requestSequence = ++sequence;
      void request<ContextUsage>(
        `/sessions/${encodeURIComponent(sessionId)}/context-usage`,
      )
        .then((result) => {
          if (current && sequence === requestSequence)
            setValue(result.session_id === sessionId ? result : null);
        })
        .catch(() => {
          // An older Runtime or a failed read has no trustworthy occupancy.
          if (current && sequence === requestSequence) setValue(null);
        });
    };
    refresh();
    const timer = active ? window.setInterval(refresh, 2000) : null;
    return () => {
      current = false;
      if (timer !== null) window.clearInterval(timer);
    };
  }, [sessionId, revision, active]);
  const matches =
    value?.session_id === sessionId &&
    (!value.model || value.model === model) &&
    (!value.provider_id || value.provider_id === providerId);
  const currentValue = matches ? value : null;
  const context = contextOccupancy(currentValue);
  useEffect(() => {
    setOpen(false);
  }, [sessionId, model, providerId]);
  useEffect(() => {
    if (!open) return;
    const escape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("keydown", escape);
    return () => document.removeEventListener("keydown", escape);
  }, [open]);
  const parts = rows.filter(
    (row) =>
      row.key !== "calibration_tokens" ||
      !!currentValue?.breakdown.calibration_tokens,
  );
  const total = parts.reduce(
    (sum, row) => sum + (currentValue?.breakdown[row.key] || 0),
    0,
  );
  const reading = context ? `${context.percent}%` : "—";
  return (
    <span className="context-meter">
      <button
        ref={anchor}
        type="button"
        className="context-meter-trigger"
        aria-label={
          context ? `上下文已用 ${reading}` : "上下文使用情况，暂无完整统计"
        }
        aria-haspopup="dialog"
        aria-expanded={open}
        title={
          context
            ? `上下文估算 ~${compactTokens(context.used)} / ${compactTokens(context.window)}`
            : "暂无完整上下文统计"
        }
        onClick={() => setOpen(!open)}
      >
        <svg viewBox="0 0 14 14" width="14" height="14" aria-hidden="true">
          <circle className="context-meter-track" cx="7" cy="7" r="5.5" />
          {context && context.percent > 0 && (
            <circle
              className="context-meter-fill"
              cx="7"
              cy="7"
              r="5.5"
              strokeDasharray={`${(circumference * context.percent) / 100} ${circumference}`}
              transform="rotate(-90 7 7)"
            />
          )}
        </svg>
        <span>{reading}</span>
      </button>
      {open && (
        <Popup
          label="上下文已用"
          anchor={anchor.current}
          align="start"
          className="context-meter-panel"
          onClose={() => setOpen(false)}
        >
          <div className="context-meter-heading">
            <span>上下文已用</span>
            <strong>{context ? reading : "未知"}</strong>
            <b>
              {context
                ? `~${compactTokens(context.used)} / ${compactTokens(context.window)}`
                : "暂无完整统计"}
            </b>
          </div>
          <div className="context-meter-bar" aria-hidden="true">
            {parts.map((row) => {
              const width =
                context && total
                  ? (context.percent *
                      (currentValue?.breakdown[row.key] || 0)) /
                    total
                  : 0;
              return width > 0 ? (
                <span
                  key={row.key}
                  className={`context-meter-segment context-color-${row.color}`}
                  style={{ width: `${width}%` }}
                />
              ) : null;
            })}
          </div>
          <dl className="context-meter-rows">
            {parts.map((row) => (
              <div key={row.key}>
                <dt>
                  <i
                    className={`context-meter-swatch context-color-${row.color}`}
                    aria-hidden="true"
                  />
                  {row.label}
                </dt>
                <dd>
                  {currentValue?.breakdown[row.key] == null
                    ? "未知"
                    : `~${compactTokens(currentValue.breakdown[row.key])}`}
                </dd>
              </div>
            ))}
          </dl>
          <p className="context-meter-note">
            {context
              ? "当前活动上下文估算，不含未发送草稿。压缩后会更新，原始历史保留。"
              : "上下文统计尚不完整，后续运行后会更新。未发送草稿不计入，原始历史保留。"}
          </p>
        </Popup>
      )}
    </span>
  );
}
