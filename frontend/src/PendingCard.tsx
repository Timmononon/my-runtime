import { useState } from "react";
import { redact } from "./api";
import { Icon } from "./icons";
import type { Pending } from "./types";

export default function PendingCard({
  pending,
  busy,
  submit,
}: {
  pending: Pending;
  busy: boolean;
  submit: (answer: string | boolean) => Promise<void>;
}) {
  const [answer, setAnswer] = useState("");
  const approval = pending.kind === "approval";
  return (
    <div className={`pending-card ${approval ? "approval" : "question"}`}>
      <div className="pending-heading">
        <span>
          <Icon name={approval ? "shield" : "chat"} size={19} />
        </span>
        <div>
          <span className="eyebrow">
            {approval ? "由你决定" : "需要你确认"}
          </span>
          <h3>
            {approval
              ? "允许这次操作吗？"
              : pending.payload.question || "请补充信息"}
          </h3>
        </div>
      </div>
      {approval ? (
        <>
          <p>
            {pending.payload.description ||
              `Aster 希望调用 ${pending.payload.tool_name}。这次授权仅用于当前操作。`}
          </p>
          <div className="approval-tool">
            <code>{pending.payload.tool_name}</code>
            <span>
              {pending.payload.risk === "external"
                ? "外部操作"
                : pending.payload.risk === "execute"
                  ? "运行程序"
                  : "写入操作"}
            </span>
          </div>
          <pre className="approval-arguments">
            {JSON.stringify(redact(pending.payload.arguments || {}), null, 2)}
          </pre>
          <div className="pending-actions">
            <button
              className="button secondary"
              disabled={busy}
              onClick={() => void submit(false)}
            >
              拒绝
            </button>
            <button
              className="button primary"
              disabled={busy}
              onClick={() => void submit(true)}
            >
              <Icon name="check" size={15} />
              允许这一次
            </button>
          </div>
        </>
      ) : (
        <>
          <div className="question-options">
            {pending.payload.options?.map((option, i) => {
              const text =
                typeof option === "string" ? option : JSON.stringify(option);
              return (
                <button
                  key={i}
                  onClick={() => setAnswer(text)}
                  className={answer === text ? "selected" : ""}
                >
                  {text}
                  <span>
                    {answer === text ? <Icon name="check" size={14} /> : i + 1}
                  </span>
                </button>
              );
            })}
          </div>
          <label className="field">
            <span className="sr-only">补充回答</span>
            <input
              placeholder="也可以输入自己的回答…"
              value={answer}
              onChange={(e) => setAnswer(e.target.value)}
            />
          </label>
          <div className="pending-actions">
            <button
              className="button primary"
              disabled={!answer.trim() || busy}
              onClick={() => void submit(answer)}
            >
              确认回答
              <Icon name="arrow" size={14} />
            </button>
          </div>
        </>
      )}
    </div>
  );
}
