import type { CSSProperties } from "react";
const paths: Record<string, string> = {
  compact: "M4 3h16M4 21h16M12 5v5m-3-3 3 3 3-3M12 19v-5m-3 3 3-3 3 3",
  plus: "M12 5v14M5 12h14",
  search: "m21 21-4.4-4.4M19 11a8 8 0 1 1-16 0 8 8 0 0 1 16 0",
  chat: "M21 11.5a8.5 8.5 0 0 1-8.5 8.5H4l-1 1v-9.5A8.5 8.5 0 0 1 11.5 3H13a8 8 0 0 1 8 8.5Z",
  grid: "M3 3h7v7H3zM14 3h7v7h-7zM3 14h7v7H3zM14 14h7v7h-7z",
  skills:
    "m14 6 4 4M3 17 15 5a1.4 1.4 0 0 1 2 0l2 2a1.4 1.4 0 0 1 0 2L7 21a1.4 1.4 0 0 1-2 0l-2-2a1.4 1.4 0 0 1 0-2ZM5 2v4M3 4h4M20 14v4M18 16h4M15 1v2M14 2h2",
  memory:
    "M6 3h14v18H6a3 3 0 0 1-3-3V6a3 3 0 0 1 3-3ZM3 18a3 3 0 0 1 3-3h14M10 3v7l3-2 3 2V3",
  clock: "M12 7v5l3 2M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0",
  folder:
    "M3 6a2 2 0 0 1 2-2h5l2 3h7a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z",
  file: "M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Zm0 0v6h6M8 12h8M8 16h6",
  attach: "m8 13 6-6a3 3 0 0 1 4 4l-8 8a5 5 0 0 1-7-7l9-9a7 7 0 0 1 10 10l-9 9",
  arrow: "M12 19V5m-6 6 6-6 6 6",
  chevron: "m9 5 7 7-7 7",
  down: "m6 9 6 6 6-6",
  stop: "M6 6h12v12H6z",
  check: "m5 12 4 4L19 6",
  close: "m6 6 12 12M6 18 18 6",
  branch: "M6 3v12a5 5 0 0 0 10 0V9M16 3v6m-3-3 3 3 3-3",
  download: "M12 3v12m-5-5 5 5 5-5M4 17v4h16v-4",
  external: "M14 3h7v7m0-7L10 14M10 3H3v18h18v-7",
  menu: "M4 6h16M4 12h16M4 18h16",
  panel: "M3 4h18v16H3zM15 4v16",
  settings:
    "M10 2h4l.5 2.2 1.9.8 1.9-1.2 2.8 2.8-1.2 1.9.8 1.9L22 10v4l-2.2.5-.8 1.9 1.2 1.9-2.8 2.8-1.9-1.2-1.9.8L14 22h-4l-.5-2.2-1.9-.8-1.9 1.2-2.8-2.8 1.2-1.9-.8-1.9L2 14v-4l2.2-.5.8-1.9-1.2-1.9 2.8-2.8L8.5 5l1.9-.8Z M15.5 12a3.5 3.5 0 1 1-7 0 3.5 3.5 0 0 1 7 0Z",
  logout: "M9 4H4v16h5M14 8l4 4-4 4m-5-4h12",
  copy: "M8 8h12v12H8zM16 8V4H4v12h4",
  refresh:
    "M20 7v5h-5M4 17v-5h5M6 7a7 7 0 0 1 12-1l2 6M4 12l2 6a7 7 0 0 0 12-1",
  shield: "m12 3 8 3v6c0 5-8 9-8 9S4 17 4 12V6Zm-4 9 3 3 5-6",
  bolt: "m13 2-9 12h7l-1 8 10-13h-7z",
  more: "M5 12h.01M12 12h.01M19 12h.01",
  pause: "M8 5v14M16 5v14",
  play: "m7 4 13 8-13 8z",
  trash: "M4 7h16M9 7V4h6v3M6 7l1 14h10l1-14M10 11v6M14 11v6",
  globe:
    "M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0M3 12h18M12 3c5 5 5 13 0 18-5-5-5-13 0-18",
  sun: "M12 3v2M12 19v2M3 12h2M19 12h2M5 5l2 2M17 17l2 2M5 19l2-2M17 7l2-2M16 12a4 4 0 1 1-8 0 4 4 0 0 1 8 0",
  info: "M12 11v6M12 7h.01M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0",
};
export function Icon({
  name,
  size = 18,
  className = "",
  style,
}: {
  name: string;
  size?: number;
  className?: string;
  style?: CSSProperties;
}) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.65"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      style={style}
      aria-hidden="true"
    >
      <path d={paths[name] || paths.file} />
    </svg>
  );
}
export function Star({
  size = 32,
  className = "",
}: {
  size?: number;
  className?: string;
}) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 48 48"
      fill="none"
      className={className}
      aria-hidden="true"
    >
      <path
        d="M24 3c2.6 12.9 4.1 14.4 17 17-12.9 2.6-14.4 4.1-17 17-2.6-12.9-4.1-14.4-17-17C19.9 17.4 21.4 15.9 24 3Z"
        fill="currentColor"
      />
      <path
        d="M39 30c1 5 1.5 5.5 6 6.5-4.5 1-5 1.5-6 6.5-1-5-1.5-5.5-6-6.5 4.5-1 5-1.5 6-6.5Z"
        fill="currentColor"
        opacity=".55"
      />
    </svg>
  );
}
