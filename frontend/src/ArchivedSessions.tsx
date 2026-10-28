import { useEffect, useRef, useState } from "react";
import { readableError, request } from "./api";
import { Icon } from "./icons";
import { dateLabel, Empty, Spinner } from "./ui";
import { LatestRequest } from "./state";
import type { Session } from "./types";
import type { SessionAction } from "./SessionLifecycle";

export default function ArchivedSessions({
  revision,
  pending,
  onOpen,
  onAction,
  report,
  operationError,
}: {
  revision: number;
  pending: string | null;
  onOpen: (session: Session) => void;
  onAction: (session: Session, action: SessionAction) => void;
  report: (error: unknown) => void;
  operationError: string;
}) {
  const [sessions, setSessions] = useState<Session[]>([]),
    [loading, setLoading] = useState(false),
    [more, setMore] = useState(false),
    [error, setError] = useState("");
  const gate = useRef(new LatestRequest()),
    offset = useRef(0),
    fetching = useRef(false);
  const load = async (append = false) => {
    if (append && fetching.current) return;
    const generation = gate.current.begin();
    const start = append ? offset.current : 0;
    fetching.current = true;
    setLoading(true);
    setError("");
    try {
      const result = await request<{ sessions: Session[] }>(
        `/sessions?include_side_chats=true&state=archived&limit=100&offset=${start}`,
      );
      if (!gate.current.current(generation)) return;
      setSessions((old) => [
        ...new Map(
          [...(append ? old : []), ...result.sessions].map((session) => [
            session.id,
            session,
          ]),
        ).values(),
      ]);
      offset.current = start + result.sessions.length;
      setMore(result.sessions.length === 100);
    } catch (failure) {
      if (gate.current.current(generation)) {
        setError(readableError(failure));
        report(failure);
      }
    } finally {
      if (gate.current.current(generation)) {
        fetching.current = false;
        setLoading(false);
      }
    }
  };
  useEffect(() => {
    void load();
    return () => {
      gate.current.invalidate();
      fetching.current = false;
    };
  }, [revision]);
  return (
    <section className="archived-sessions" aria-label="已归档对话">
      <div className="settings-section-heading">
        <div>
          <Icon name="folder" size={21} />
          <h3>已归档对话</h3>
        </div>
        <p>查看归档记录，或取消归档后继续对话。永久删除后无法恢复。</p>
      </div>
      {operationError && (
        <p className="inline-error" role="alert">
          {operationError}
        </p>
      )}
      {error && (
        <div className="archive-list-error" role="alert">
          <span>{error}</span>
          <button className="text-button" onClick={() => void load()}>
            重试载入归档
          </button>
        </div>
      )}
      <div className="archive-list">
        {sessions.map((session) => {
          const title = session.title || "新对话";
          return (
            <div className="archive-row" key={session.id}>
              <button
                className="archive-open"
                aria-label={`查看归档对话 ${title}`}
                disabled={pending === session.id}
                onClick={() => onOpen(session)}
              >
                <strong>{title}</strong>
                <small>
                  {dateLabel(session.updated_at)} · {session.cwd}
                </small>
              </button>
              <button
                className="button secondary"
                aria-label={`取消归档对话 ${title}`}
                disabled={!!pending}
                onClick={() => onAction(session, "unarchive")}
              >
                {pending === session.id ? (
                  <Spinner />
                ) : (
                  <Icon name="refresh" size={14} />
                )}
                取消归档
              </button>
              <button
                className="icon-button"
                aria-label={`永久删除归档对话 ${title}`}
                title="永久删除对话"
                disabled={!!pending}
                onClick={() => onAction(session, "delete")}
              >
                <Icon name="trash" size={16} />
              </button>
            </div>
          );
        })}
      </div>
      {loading && (
        <div className="loading-history" role="status">
          <Spinner />
          正在载入归档
        </div>
      )}
      {!loading && !error && !sessions.length && (
        <Empty
          title="没有已归档的对话"
          detail="在对话旁的管理菜单中选择“归档对话”。"
        />
      )}
      {more && (
        <button
          className="load-more-sessions"
          disabled={loading}
          onClick={() => void load(true)}
        >
          加载更多归档
        </button>
      )}
    </section>
  );
}
