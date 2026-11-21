/*!
 * Source navigation marks.
 * GitHub mark from https://github.com/primer/octicons/blob/main/icons/mark-github-16.svg
 * Copyright (c) 2026 GitHub Inc.
 * Feishu mark adapted from https://github.com/DouyinFE/semi-design/blob/main/packages/semi-icons/src/svgs/feishu_logo.svg
 * Copyright (c) 2021 DouyinFE
 * @license MIT
 *
 * Permission is hereby granted, free of charge, to any person obtaining a copy
 * of this software and associated documentation files (the "Software"), to deal
 * in the Software without restriction, including without limitation the rights
 * to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
 * copies of the Software, and to permit persons to whom the Software is
 * furnished to do so, subject to the following conditions:
 *
 * The above copyright notice and this permission notice shall be included in all
 * copies or substantial portions of the Software.
 *
 * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
 * IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
 * FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
 * AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
 * LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
 * OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
 * SOFTWARE.
 */
import type { CSSProperties } from "react";
const paths: Record<string, string> = {
  github:
    "M6.766 11.328c-2.063-.25-3.516-1.734-3.516-3.656 0-.781.281-1.625.75-2.188-.203-.515-.172-1.609.063-2.062.625-.078 1.468.25 1.968.703.594-.187 1.219-.281 1.985-.281.765 0 1.39.094 1.953.265.484-.437 1.344-.765 1.969-.687.218.422.25 1.515.046 2.047.5.593.766 1.39.766 2.203 0 1.922-1.453 3.375-3.547 3.64.531.344.89 1.094.89 1.954v1.625c0 .468.391.734.86.547C13.781 14.359 16 11.53 16 8.03 16 3.61 12.406 0 7.984 0 3.563 0 0 3.61 0 8.031a7.88 7.88 0 0 0 5.172 7.422c.422.156.828-.125.828-.547v-1.25c-.219.094-.5.156-.75.156-1.031 0-1.64-.562-2.078-1.609-.172-.422-.36-.672-.719-.719-.187-.015-.25-.093-.25-.187 0-.188.313-.328.625-.328.453 0 .844.281 1.25.86.313.452.64.655 1.031.655s.641-.14 1-.5c.266-.265.47-.5.657-.656",
  feishu:
    "M6.14 3.81C8.77 5.99 11.02 8.49 12.74 11.43L14.44 9.76C15.25 8.97 16.21 8.34 17.26 7.93C16.78 6.32 16 4.98 14.94 3.65C14.81 3.49 14.62 3.4 14.41 3.4H6.28C6.07 3.4 5.97 3.67 6.14 3.81Z " +
    "M11.04 14.56C12.27 15.08 13.38 15.53 14.69 15.88C17.02 16.51 19.23 15.57 20.32 13.49L21.64 10.85C21.94 10.21 22.31 9.62 22.76 9.07C21.97 8.78 21.31 8.63 20.45 8.63C18.55 8.63 16.76 9.36 15.4 10.69L13.38 12.69C12.66 13.39 11.88 14.02 11.04 14.56Z " +
    "M1.63 9.75C1.48 9.61 1.24 9.71 1.24 9.91V17.87C1.24 18.1 1.35 18.31 1.53 18.43C3.67 19.85 6.15 20.6 8.72 20.6C11.17 20.6 13.55 19.92 15.61 18.63C16.39 18.14 17 17.71 17.65 17.08C16.6 17.39 15.45 17.41 14.28 17.09C9.38 15.76 5.2 13.2 1.63 9.75Z",
  "nav-chat":
    "M20.5 11a6.75 6.75 0 0 1-6.75 6.75H8.5l-4.75 2.5V11a6.75 6.75 0 0 1 6.75-6.75h3.25A6.75 6.75 0 0 1 20.5 11Z " +
    "M8 9.5h8M8 12.75h5.5",
  "nav-skills":
    "M4 17.3 13.8 7.5a1.65 1.65 0 0 1 2.33 0l.37.37a1.65 1.65 0 0 1 0 2.33L6.7 20a1.65 1.65 0 0 1-2.33 0L4 19.63a1.65 1.65 0 0 1 0-2.33Z " +
    "M12 9.3l2.7 2.7M7 3.4v4.4M4.8 5.6h4.4M19 13.6V18M16.8 15.8h4.4",
  "nav-memory":
    "M8 3.75h10a1.25 1.25 0 0 1 1.25 1.25v14A1.25 1.25 0 0 1 18 20.25H7.5a3 3 0 0 1-3-3v-10A3.5 3.5 0 0 1 8 3.75Z " +
    "M4.5 17.25a2.5 2.5 0 0 1 2.5-2.5h12.25M10 3.75v7.5l2.5-1.6 2.5 1.6v-7.5",
  "nav-tasks":
    "M8.75 5H6.25A1.75 1.75 0 0 0 4.5 6.75v12A1.75 1.75 0 0 0 6.25 20.5h11.5a1.75 1.75 0 0 0 1.75-1.75v-12A1.75 1.75 0 0 0 17.75 5h-2.5 " +
    "M10 3.5h4A1.25 1.25 0 0 1 15.25 4.75V6.5h-6.5V4.75A1.25 1.25 0 0 1 10 3.5Z " +
    "M8 11.25l1 1 2.2-2.2M8 15.75l1 1 2.2-2.2M13.5 11.5H16M13.5 16H16",
  "nav-automation":
    "M20.25 12A8.25 8.25 0 1 1 17.83 6.17L20 8.35M20 4.75v3.6h-3.6M12 8.5v4l2.5 1.5",
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
  const isBrandMark = name === "github" || name === "feishu";
  return (
    <svg
      width={size}
      height={size}
      viewBox={name === "github" ? "0 0 16 16" : "0 0 24 24"}
      fill={isBrandMark ? "currentColor" : "none"}
      stroke={isBrandMark ? "none" : "currentColor"}
      strokeWidth={name.startsWith("nav-") ? 2.05 : 1.65}
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
