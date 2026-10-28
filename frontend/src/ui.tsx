import { useEffect, useLayoutEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { createPortal } from "react-dom";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Icon } from "./icons";

export function Popup({
  children,
  onClose,
  label,
  className = "",
  anchor,
  align = "end",
}: {
  children: ReactNode;
  onClose: () => void;
  label: string;
  className?: string;
  anchor?: HTMLElement | null;
  align?: "start" | "end";
}) {
  const root = useRef<HTMLDivElement>(null),
    close = useRef(onClose);
  close.current = onClose;
  const [position, setPosition] = useState({ left: 8, top: 8 });
  useLayoutEffect(() => {
    if (!anchor) return;
    const place = () => {
      const trigger = anchor.getBoundingClientRect();
      const bounds = root.current?.getBoundingClientRect();
      const width = bounds?.width || 230,
        height = bounds?.height || 100;
      const top =
        trigger.bottom + height + 8 > window.innerHeight
          ? Math.max(8, trigger.top - height - 4)
          : trigger.bottom + 4;
      setPosition({
        left: Math.max(
          8,
          Math.min(
            align === "start" ? trigger.left : trigger.right - width,
            window.innerWidth - width - 8,
          ),
        ),
        top,
      });
    };
    place();
    window.addEventListener("resize", place);
    document.addEventListener("scroll", place, true);
    return () => {
      window.removeEventListener("resize", place);
      document.removeEventListener("scroll", place, true);
    };
  }, [anchor, align]);
  useEffect(() => {
    const previous = document.activeElement as HTMLElement;
    const first = root.current?.querySelector<HTMLElement>(
      "button:not([disabled]),input,select",
    );
    first?.focus({ preventScroll: true });
    const outside = (event: PointerEvent) => {
      if (
        root.current &&
        !root.current.contains(event.target as Node) &&
        !(anchor || root.current.parentElement)?.contains(event.target as Node)
      )
        close.current();
    };
    const key = (event: KeyboardEvent) => {
      if (
        event.defaultPrevented ||
        !root.current?.contains(document.activeElement)
      )
        return;
      if (event.key === "Tab") close.current();
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        close.current();
      }
      if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
        const items = Array.from(
          root.current?.querySelectorAll<HTMLElement>(
            "button:not([disabled])",
          ) || [],
        );
        if (!items.length) return;
        const index = items.indexOf(document.activeElement as HTMLElement);
        const next =
          event.key === "Home"
            ? 0
            : event.key === "End"
              ? items.length - 1
              : (index + (event.key === "ArrowDown" ? 1 : -1) + items.length) %
                items.length;
        event.preventDefault();
        items[next].focus();
      }
    };
    document.addEventListener("pointerdown", outside);
    document.addEventListener("keydown", key);
    return () => {
      document.removeEventListener("pointerdown", outside);
      document.removeEventListener("keydown", key);
      previous?.focus({ preventScroll: true });
    };
  }, [anchor]);
  const content = (
    <div
      className={`popup ${className}`}
      style={
        anchor
          ? {
              position: "fixed",
              left: position.left,
              top: position.top,
              right: "auto",
              bottom: "auto",
            }
          : undefined
      }
      ref={root}
      role="dialog"
      aria-label={label}
      onPointerDown={(event) => event.stopPropagation()}
      onClick={(event) => event.stopPropagation()}
    >
      {children}
    </div>
  );
  return anchor ? createPortal(content, document.body) : content;
}

export function Modal({
  title,
  children,
  onClose,
  wide = false,
  className = "",
}: {
  title: string;
  children: ReactNode;
  onClose: () => void;
  wide?: boolean;
  className?: string;
}) {
  const ref = useRef<HTMLDivElement>(null),
    closeRef = useRef(onClose);
  closeRef.current = onClose;
  useEffect(() => {
    const previous = document.activeElement as HTMLElement;
    ref.current?.focus({ preventScroll: true });
    const key = (e: KeyboardEvent) => {
      const backdrops = document.querySelectorAll(".modal-backdrop");
      if (
        e.defaultPrevented ||
        backdrops[backdrops.length - 1] !== ref.current?.parentElement
      )
        return;
      if (e.key === "Escape") {
        e.preventDefault();
        e.stopPropagation();
        closeRef.current();
      }
      if (e.key === "Tab") {
        const list = ref.current?.querySelectorAll<HTMLElement>(
          "button:not([disabled]),input:not([disabled]),textarea:not([disabled]),select:not([disabled]),a[href]",
        );
        if (!list?.length) return;
        const first = list[0],
          last = list[list.length - 1];
        if (
          e.shiftKey &&
          (document.activeElement === first ||
            document.activeElement === ref.current)
        ) {
          e.preventDefault();
          last.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault();
          first.focus();
        }
      }
    };
    document.addEventListener("keydown", key);
    return () => {
      document.removeEventListener("keydown", key);
      previous?.focus({ preventScroll: true });
    };
  }, []);
  return createPortal(
    <div
      className="modal-backdrop"
      onPointerDown={(e) => e.stopPropagation()}
      onClick={(e) => e.stopPropagation()}
      onMouseDown={(e) => {
        e.stopPropagation();
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        ref={ref}
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        className={`modal ${wide ? "wide" : ""} ${className}`}
      >
        <div className="modal-heading">
          <h2>{title}</h2>
          <button className="icon-button" onClick={onClose} aria-label="关闭">
            <Icon name="close" />
          </button>
        </div>
        {children}
      </div>
    </div>,
    document.body,
  );
}
export function Markdown({ children }: { children: string }) {
  return (
    <div className="markdown">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        skipHtml
        components={{
          a: ({ href, children }) => (
            <a href={href} target="_blank" rel="noopener noreferrer">
              {children}
              <Icon name="external" size={11} />
            </a>
          ),
          img: ({ alt }) => (
            <span className="image-placeholder">
              <Icon name="file" size={15} />
              {alt || "图片"} · 外部图片未自动加载
            </span>
          ),
        }}
      >
        {children}
      </ReactMarkdown>
    </div>
  );
}
export function Empty({
  icon = "folder",
  title,
  detail,
}: {
  icon?: string;
  title: string;
  detail?: string;
}) {
  return (
    <div className="empty-state">
      <span>
        <Icon name={icon} size={25} />
      </span>
      <h3>{title}</h3>
      {detail && <p>{detail}</p>}
    </div>
  );
}
export function Spinner() {
  return <span className="spinner" aria-label="加载中" />;
}
export function Status({ status }: { status: string }) {
  const names: Record<string, string> = {
    queued: "排队中",
    running: "正在执行",
    waiting_approval: "等待批准",
    waiting_input: "等待回答",
    cancelling: "正在停止",
    completed: "已完成",
    failed: "执行失败",
    cancelled: "已取消",
    timed_out: "已超时",
    budget_exhausted: "额度已用尽",
    needs_reconciliation: "结果待核对",
    outcome_unknown: "结果未知",
  };
  return (
    <span className={`status-badge ${status}`}>
      <i />
      {names[status] || status}
    </span>
  );
}
export const activeStatus = (status?: string) =>
  [
    "queued",
    "running",
    "waiting_approval",
    "waiting_input",
    "cancelling",
  ].includes(status || "");
export const dateLabel = (seconds: number) =>
  new Intl.DateTimeFormat("zh-CN", {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(seconds * 1000));
export const sizeLabel = (bytes?: number) =>
  bytes === undefined
    ? "附件"
    : bytes < 1024
      ? `${bytes} B`
      : bytes < 1048576
        ? `${Math.round(bytes / 1024)} KB`
        : `${(bytes / 1048576).toFixed(1)} MB`;
