import { useRef, useState } from "react";
import { Icon } from "./icons";
import { Popup, Spinner } from "./ui";
import type { Session } from "./types";

export type SessionState = "active" | "archived";
export type SessionAction = "archive" | "unarchive" | "delete";
export function sessionState(session: Session | null): SessionState {
  return session?.archived_at != null ? "archived" : "active";
}
export const stateLabels: Record<SessionState, string> = {
  active: "常用对话",
  archived: "已归档",
};

export function WorkspaceSessionRows({
  sessions,
  selected,
  pending,
  selectedBlocked,
  onOpen,
  onAction,
}: {
  sessions: Session[];
  selected: string | null;
  pending: string | null;
  selectedBlocked: boolean;
  onOpen: (id: string) => void;
  onAction: (session: Session, action: SessionAction) => void;
}) {
  return sessions.map((session) => (
    <div
      key={session.id}
      className={`recent-row ${selected === session.id ? "selected" : ""}`}
    >
      <button
        className={`recent-item ${selected === session.id ? "selected" : ""}`}
        onClick={() => onOpen(session.id)}
        title={session.title || session.cwd}
      >
        <Icon
          name={session.lineage?.kind === "side_chat" ? "panel" : "chat"}
          size={14}
        />
        <span>{session.title || "新对话"}</span>
      </button>
      <SessionActions
        session={session}
        disabled={!!pending || (selected === session.id && selectedBlocked)}
        pending={pending === session.id}
        reason={
          pending
            ? "正在更新对话，请稍候。"
            : "任务正在运行或请求处理中，请稍候再操作。"
        }
        onAction={(action) => onAction(session, action)}
        compact
      />
    </div>
  ));
}

export function SessionActions({
  session,
  disabled,
  pending,
  reason,
  onAction,
  compact = false,
  onSideChat,
  sideChatDisabled = false,
  sideChatBusy = false,
}: {
  session: Session;
  disabled: boolean;
  pending: boolean;
  reason: string;
  onAction: (action: SessionAction) => void;
  compact?: boolean;
  onSideChat?: () => void;
  sideChatDisabled?: boolean;
  sideChatBusy?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const anchor = useRef<HTMLDivElement>(null);
  const state = sessionState(session);
  const action = (value: SessionAction) => {
    setOpen(false);
    onAction(value);
  };
  return (
    <div className="option-anchor session-actions" ref={anchor}>
      <button
        className="icon-button"
        aria-label={
          compact ? `管理对话 ${session.title || "新对话"}` : "管理当前对话"
        }
        title={onSideChat ? "对话操作" : "归档或删除对话"}
        aria-expanded={open}
        onClick={() => setOpen(!open)}
      >
        {pending ? <Spinner /> : <Icon name="more" size={18} />}
      </button>
      {open && (
        <Popup
          label="当前对话操作"
          className="session-popup"
          onClose={() => setOpen(false)}
          anchor={compact ? anchor.current : undefined}
        >
          {onSideChat && (
            <button
              disabled={sideChatDisabled || sideChatBusy}
              onClick={() => {
                setOpen(false);
                onSideChat();
              }}
            >
              {sideChatBusy ? <Spinner /> : <Icon name="panel" size={15} />}
              {sideChatBusy ? "正在打开侧边聊天…" : "侧边聊天"}
            </button>
          )}
          <button
            disabled={disabled}
            onClick={() =>
              action(state === "archived" ? "unarchive" : "archive")
            }
          >
            <Icon
              name={state === "archived" ? "refresh" : "folder"}
              size={15}
            />
            {state === "archived" ? "取消归档" : "归档对话"}
          </button>
          <button disabled={disabled} onClick={() => action("delete")}>
            <Icon name="trash" size={15} />
            永久删除对话
          </button>
          {disabled && <p className="lifecycle-help">{reason}</p>}
        </Popup>
      )}
    </div>
  );
}

export function SessionReadonlyNotice({
  session,
  disabled,
  onAction,
}: {
  session: Session;
  disabled: boolean;
  onAction: (action: SessionAction) => void;
}) {
  const state = sessionState(session);
  if (state === "active") return null;
  return (
    <div className="session-readonly" role="status">
      <Icon name="info" size={17} />
      <span>
        已归档的对话仅供查看，取消归档后可继续对话。归档列表在设置中。
      </span>
      <button
        className="button secondary"
        disabled={disabled}
        onClick={() => onAction("unarchive")}
      >
        取消归档
      </button>
    </div>
  );
}
