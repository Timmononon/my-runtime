import { useEffect, useRef, useState } from "react";
import { post, request, readableError } from "./api";
import { LatestRequest } from "./state";
import { Spinner } from "./ui";
type Status = {
  mode: string;
  paused: boolean;
  attached: boolean;
  bridge_configured: boolean;
  pages: { page_id: string; url: string; available: boolean }[];
};
export default function BrowserControl({
  selected,
  allowedPages,
  revision,
}: {
  selected: string | null;
  allowedPages: string[];
  revision: string;
}) {
  const [status, setStatus] = useState<Status | null>(null),
    [url, setUrl] = useState(allowedPages[0] || ""),
    [busy, setBusy] = useState(false),
    [issue, setIssue] = useState("");
  const gate = useRef(new LatestRequest()),
    currentSession = useRef(selected),
    flight = useRef(false),
    attempts = useRef(new Map<string, string>());
  currentSession.current = selected;
  const load = async () => {
    if (!selected) return;
    const generation = gate.current.begin();
    try {
      const value = await request<Status>(
        `/browser-control/sessions/${encodeURIComponent(selected)}`,
      );
      if (
        gate.current.current(generation) &&
        currentSession.current === selected
      ) {
        setStatus(value);
        setIssue("");
        return true;
      }
    } catch (error) {
      if (
        gate.current.current(generation) &&
        currentSession.current === selected
      )
        setIssue(readableError(error));
    }
    return false;
  };
  useEffect(() => {
    setStatus(null);
    setIssue("");
    setBusy(false);
    void load();
    return () => gate.current.invalidate();
  }, [selected, revision]);
  useEffect(() => {
    if (!allowedPages.includes(url)) setUrl(allowedPages[0] || "");
  }, [allowedPages]);
  const act = async (operation: string) => {
    if (!selected || flight.current) return;
    flight.current = true;
    const key = `${selected}:${operation}:${operation === "attach" ? url : ""}`;
    const requestId = attempts.current.get(key) || crypto.randomUUID();
    attempts.current.set(key, requestId);
    setBusy(true);
    setIssue("");
    try {
      const value = await post<{
        is_error: boolean;
        content: string;
        outcome: string;
      }>(
        `/browser-control/sessions/${encodeURIComponent(selected)}/${operation}`,
        {
          request_id: requestId,
          arguments: operation === "attach" ? { url } : {},
          approved: true,
        },
      );
      if (!["unknown", "outcome_unknown"].includes(value.outcome))
        attempts.current.delete(key);
      if (value.is_error) throw new Error(value.content);
      if (currentSession.current === selected) await load();
    } catch (error) {
      if (currentSession.current === selected) setIssue(readableError(error));
    } finally {
      flight.current = false;
      if (currentSession.current === selected) setBusy(false);
    }
  };
  return (
    <article className="settings-card browser-control">
      <h4>当前会话浏览器</h4>
      {!selected ? (
        <p className="fine-print">打开一个对话后，可管理它的浏览器连接。</p>
      ) : (
        <>
          {issue && (
            <p className="inline-error" role="alert">
              {issue}
            </p>
          )}
          {status ? (
            <>
              <p role="status">
                {status.attached ? "已连接" : "尚未连接"} ·{" "}
                {status.paused ? "已暂停，等待你操作" : "自动化可继续"} ·{" "}
                {status.mode === "bridge" ? "现有浏览器" : "独立浏览器"}
              </p>
              {status.mode === "bridge" && (
                <>
                  <label className="field">
                    连接的已授权页面
                    <select
                      aria-label="连接的已授权页面"
                      value={url}
                      onChange={(e) => setUrl(e.target.value)}
                    >
                      <option value="">选择页面</option>
                      {allowedPages.map((page) => (
                        <option key={page}>{page}</option>
                      ))}
                    </select>
                  </label>
                  <button
                    className="button secondary"
                    disabled={busy || !status.bridge_configured || !url}
                    onClick={() => void act("attach")}
                  >
                    连接此页面
                  </button>
                </>
              )}
              {status.pages.map((page) => (
                <p className="fine-print" key={page.page_id}>
                  {page.url} · {page.available ? "可用" : "不可用"}
                </p>
              ))}
              <div className="modal-actions">
                <button
                  className="button secondary"
                  disabled={busy || !status.attached || status.paused}
                  onClick={() => void act("pause")}
                >
                  暂停浏览器自动化
                </button>
                <button
                  className="button secondary"
                  disabled={busy || !status.attached || !status.paused}
                  onClick={() => void act("resume")}
                >
                  恢复浏览器自动化
                </button>
                <button
                  className="text-button"
                  disabled={busy || !status.attached}
                  onClick={() => void act("detach")}
                >
                  断开连接
                </button>
              </div>
            </>
          ) : !issue ? (
            <Spinner />
          ) : null}
          <button
            className="text-button"
            disabled={busy}
            onClick={() => {
              void load().then((loaded) => {
                if (loaded) attempts.current.clear();
              });
            }}
          >
            刷新浏览器状态
          </button>
        </>
      )}
    </article>
  );
}
