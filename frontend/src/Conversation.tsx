import { useEffect, useState } from "react";
import { Avatar, Button, Chip } from "@heroui/react";
import { Icon, Star } from "./icons";
import { Markdown, Popup } from "./ui";
import { branchBoundaries, displayContent, groupConversation } from "./state";
import type { MessageRecord } from "./types";
import { toolErrorSummary } from "./toolErrors";

declare global {
  interface Window {
    asterClipboard?: { writeText: (text: string) => Promise<void> };
  }
}

function CopyAnswer({ text, report }: { text: string; report: (error: unknown) => void }) {
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    if (!copied) return;
    const timeout = window.setTimeout(() => setCopied(false), 2000);
    return () => window.clearTimeout(timeout);
  }, [copied]);

  async function copy() {
    try {
      const clipboard = window.asterClipboard ?? navigator.clipboard;
      await clipboard.writeText(text);
      setCopied(true);
    } catch {
      setCopied(false);
      report(new Error("复制失败，请选中回答文字后按 Ctrl+C 复制。"));
    }
  }

  const label = copied ? "已复制回答" : "复制回答";
  return (
    <Button isIconOnly size="sm" variant="ghost" aria-label={label} onPress={copy}>
      <Icon name={copied ? "check" : "copy"} size={14} />
    </Button>
  );
}

function AssistantAvatar() {
  return (
    <Avatar
      className="message-avatar"
      size="sm"
      color="accent"
      variant="soft"
      aria-hidden="true"
    >
      <Avatar.Fallback>
        <Star size={19} />
      </Avatar.Fallback>
    </Avatar>
  );
}

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
              className="message thread-message user"
              key={group.id}
              data-testid="message-user"
            >
              <div className="message-body">
                {message.metadata.artifacts?.length ? (
                  <div className="message-attachments">
                    {message.metadata.artifacts.map((a) => (
                      <Chip key={a.artifact_id} variant="soft" size="sm">
                        <Icon name="file" size={14} />
                        <Chip.Label>
                          {a.filename || a.path?.split(/[\\/]/).pop() || "附件"}
                        </Chip.Label>
                      </Chip>
                    ))}
                  </div>
                ) : null}
                <div className="message-bubble">
                  <Markdown>{displayContent(message)}</Markdown>
                </div>
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
            className={`message thread-message assistant assistant-group ${live ? "live" : ""}`}
            key={group.id}
            data-testid="message-assistant"
            data-message-boundary={group.boundary}
          >
            <AssistantAvatar />
            <div className="message-body">
              <div className="message-label">
                Aster
                {group.records.some(
                  ({ record }) =>
                    record.message.metadata.inherited_from_session,
                ) && (
                  <Chip size="sm" variant="soft">
                    继承的上下文
                  </Chip>
                )}
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
                            const error = toolErrorSummary(result, call.name);
                            return (
                              <div className="tool-result-row" key={call.id}>
                                <div className="tool-result-header">
                                  <Icon
                                    name={
                                      error
                                        ? "info"
                                        : result
                                          ? "check"
                                          : "clock"
                                    }
                                    size={14}
                                  />
                                  <code>{call.name}</code>
                                  <Chip
                                    size="sm"
                                    variant="soft"
                                    color={
                                      !result
                                        ? "default"
                                        : result.metadata.outcome === "unknown"
                                          ? "warning"
                                          : result.metadata.is_error
                                            ? "danger"
                                            : "success"
                                    }
                                  >
                                    {result
                                      ? result.metadata.outcome === "unknown"
                                        ? "结果待核验"
                                        : result.metadata.is_error
                                          ? "返回错误"
                                          : "已返回"
                                      : "等待结果"}
                                  </Chip>
                                </div>
                                {error && (
                                  <div
                                    className="tool-result-error"
                                    aria-label={`${call.name} 错误原因`}
                                  >
                                    <strong>{error.title}</strong>
                                    <p>{error.detail}</p>
                                  </div>
                                )}
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
                  {finalText && <CopyAnswer text={finalText} report={report} />}
                  {!readOnly && onBranch && boundaries.has(group.boundary) && (
                    <Button
                      size="sm"
                      variant="ghost"
                      onPress={() => onBranch(group.boundary)}
                    >
                      <Icon name="branch" size={14} />
                      从这里分支
                    </Button>
                  )}
                  {!readOnly &&
                    onBranch &&
                    group.records.filter((r) => boundaries.has(r.rawIndex + 1))
                      .length > 1 && (
                      <div className="option-anchor">
                        <Button
                          isIconOnly
                          size="sm"
                          variant="ghost"
                          aria-label="选择更早的分支位置"
                          aria-expanded={boundaryMenu === group.id}
                          onPress={() =>
                            setBoundaryMenu(
                              boundaryMenu === group.id ? null : group.id,
                            )
                          }
                        >
                          <Icon name="more" size={16} />
                        </Button>
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
          className="message thread-message assistant streaming"
          data-testid="message-assistant"
        >
          <AssistantAvatar />
          <div className="message-body">
            <div className="message-label">
              Aster
              <Chip size="sm" variant="soft" color="accent">
                正在回应
              </Chip>
            </div>
            <Markdown>{stream}</Markdown>
            <span className="stream-caret" />
          </div>
        </article>
      )}
    </>
  );
}
