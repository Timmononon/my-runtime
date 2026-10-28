import { useState } from "react";
import { Icon, Star } from "./icons";
import { Markdown, Popup } from "./ui";
import { branchBoundaries, displayContent, groupConversation } from "./state";
import type { MessageRecord } from "./types";

export default function Conversation({
  records,
  stream,
  runId,
  busy,
  readOnly = false,
  onBranch,
  report,
}: {
  records: MessageRecord[];
  stream: string;
  runId: string | null;
  busy: boolean;
  readOnly?: boolean;
  onBranch?: (index: number) => void;
  report: (error: unknown) => void;
}) {
  const groups = groupConversation(records),
    boundaries = branchBoundaries(records);
  const [boundaryMenu, setBoundaryMenu] = useState<string | null>(null);
  const last = groups.at(-1),
    appendStream =
      !!stream && last?.role === "assistant" && last.runId === runId;
  return (
    <>
      {groups.map((group) => {
        const live = busy && group.runId === runId;
        if (group.role === "user") {
          const message = group.records[0].record.message;
          return (
            <article
              className="message user"
              key={group.id}
              data-testid="message-user"
            >
              <div className="message-body">
                <Markdown>{displayContent(message)}</Markdown>
                {message.metadata.artifacts?.length ? (
                  <div className="message-attachments">
                    {message.metadata.artifacts.map((a) => (
                      <span key={a.artifact_id}>
                        <Icon name="file" size={14} />
                        {a.filename || a.path?.split(/[\\/]/).pop() || "附件"}
                      </span>
                    ))}
                  </div>
                ) : null}
              </div>
            </article>
          );
        }
        const textRecords = group.records.filter(
          ({ record }) =>
            record.message.role === "assistant" && record.message.content,
        );
        const finalText = textRecords.at(-1)?.record.message.content || "";
        return (
          <article
            className={`message assistant assistant-group ${live ? "live" : ""}`}
            key={group.id}
            data-testid="message-assistant"
            data-message-boundary={group.boundary}
          >
            <div className="message-avatar">
              <Star size={25} />
            </div>
            <div className="message-body">
              <div className="message-label">
                Aster
                {group.records.some(
                  ({ record }) =>
                    record.message.metadata.inherited_from_session,
                ) && <span>继承的上下文</span>}
              </div>
              {group.records.map(({ record }) => {
                const message = record.message;
                if (message.role !== "assistant") return null;
                const calls = message.tool_calls || [];
                return (
                  <div className="assistant-segment" key={record.id}>
                    {message.content && <Markdown>{message.content}</Markdown>}
                    {calls.length > 0 && (
                      <details className="tool-group">
                        <summary>
                          <span className="tool-group-icon">
                            <Icon name="bolt" size={14} />
                          </span>
                          <span>
                            {calls.length === 1
                              ? `使用 ${calls[0].name}`
                              : `使用 ${calls.length} 个工具`}
                          </span>
                          <Icon name="down" size={13} />
                        </summary>
                        <div className="tool-list">
                          {calls.map((call) => {
                            const result = group.records.find(
                              (item) =>
                                item.record.message.tool_call_id === call.id,
                            )?.record.message;
                            return (
                              <div key={call.id}>
                                <Icon
                                  name={
                                    result?.metadata.is_error
                                      ? "info"
                                      : result
                                        ? "check"
                                        : "clock"
                                  }
                                  size={14}
                                />
                                <code>{call.name}</code>
                                <span>
                                  {result
                                    ? result.metadata.is_error
                                      ? "返回错误"
                                      : "已返回"
                                    : "等待结果"}
                                </span>
                              </div>
                            );
                          })}
                          <p>
                            执行参数在审批时展示，内部推理不会出现在对话中。
                          </p>
                        </div>
                      </details>
                    )}
                  </div>
                );
              })}
              {appendStream && group === last && (
                <div className="assistant-segment streaming">
                  <Markdown>{stream}</Markdown>
                  <span className="stream-caret" />
                </div>
              )}
              {!live && (
                <div className="message-actions">
                  {finalText && (
                    <button
                      aria-label="复制回答"
                      onClick={() =>
                        void navigator.clipboard
                          .writeText(finalText)
                          .catch(report)
                      }
                    >
                      <Icon name="copy" size={14} />
                    </button>
                  )}
                  {!readOnly && onBranch && boundaries.has(group.boundary) && (
                    <>
                      <button onClick={() => onBranch(group.boundary)}>
                        <Icon name="branch" size={14} />
                        从这里分支
                      </button>
                    </>
                  )}
                  {!readOnly &&
                    onBranch &&
                    group.records.filter((r) => boundaries.has(r.rawIndex + 1))
                      .length > 1 && (
                      <div className="option-anchor">
                        <button
                          aria-label="选择更早的分支位置"
                          aria-expanded={boundaryMenu === group.id}
                          onClick={() =>
                            setBoundaryMenu(
                              boundaryMenu === group.id ? null : group.id,
                            )
                          }
                        >
                          <Icon name="more" size={16} />
                        </button>
                        {boundaryMenu === group.id && (
                          <Popup
                            label="完整历史分支位置"
                            onClose={() => setBoundaryMenu(null)}
                            className="boundary-popup"
                          >
                            <div className="popup-heading">
                              从完整历史位置继续
                            </div>
                            {group.records
                              .filter((r) => boundaries.has(r.rawIndex + 1))
                              .map(({ record, rawIndex }) => (
                                <button
                                  key={record.id}
                                  onClick={() => {
                                    setBoundaryMenu(null);
                                    onBranch(rawIndex + 1);
                                  }}
                                >
                                  <span>
                                    原始消息 {rawIndex + 1}
                                    <small>
                                      {record.message.role === "tool"
                                        ? "工具结果已返回"
                                        : record.message.content.slice(0, 48) ||
                                          "助手步骤完成"}
                                    </small>
                                  </span>
                                  <Icon name="branch" size={14} />
                                </button>
                              ))}
                          </Popup>
                        )}
                      </div>
                    )}
                </div>
              )}
            </div>
          </article>
        );
      })}
      {stream && !appendStream && (
        <article
          className="message assistant streaming"
          data-testid="message-assistant"
        >
          <div className="message-avatar">
            <Star size={25} />
          </div>
          <div className="message-body">
            <div className="message-label">
              Aster<span>正在回应</span>
            </div>
            <Markdown>{stream}</Markdown>
            <span className="stream-caret" />
          </div>
        </article>
      )}
    </>
  );
}
