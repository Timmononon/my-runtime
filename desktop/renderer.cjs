const { protocol } = require("electron");
const { readFile, realpath } = require("node:fs/promises");
const path = require("node:path");

const scheme = "aster-assets";
const assetOrigin = `${scheme}://bundle`;
const assetExtensions = new Set([".css", ".js", ".svg", ".png", ".jpg", ".jpeg", ".webp", ".ico", ".woff", ".woff2"]);

// Electron requires this registration before app.ready.
protocol.registerSchemesAsPrivileged([{
  scheme,
  privileges: { standard: true, secure: true, corsEnabled: true, supportFetchAPI: true },
}]);

function isAsset(url) {
  try {
    const [, folder, ...parts] = decodeURIComponent(url.pathname).split("/");
    return folder === "assets" && assetExtensions.has(path.extname(parts.at(-1) || "").toLowerCase()) &&
      parts.every((part) => part && !part.startsWith(".") && !/[\\:\x00]/.test(part));
  } catch {
    return false;
  }
}

async function loadRenderer(win, launch) {
  const contents = win.webContents;
  const session = contents.session;
  const origin = new URL(launch.url).origin;
  const root = await realpath(launch.static_root);
  const indexFile = path.join(root, "index.html");
  const assets = path.join(root, "assets");
  if (path.relative(indexFile, await realpath(indexFile)) || path.relative(assets, await realpath(assets))) {
    throw new Error("Invalid bundled renderer paths");
  }
  const index = await readFile(indexFile, "utf8");
  // An imported chunk can import the entry module again. Give both imports
  // the same URL so Chromium evaluates React only once.
  const html = index.replace(/\b(src|href)=(["'])(?:\.\/|\/)assets\//g, `$1=$2${assetOrigin}/assets/`);

  session.protocol.registerSource(scheme, { routes: [{
    match: { host: "bundle", path: "/assets/" },
    source: {
      type: "directory", root: assets, index: "",
      headers: {
        "Access-Control-Allow-Origin": origin,
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
      },
    },
  }] });
  session.webRequest.onBeforeRequest({ urls: [`${origin}/assets/*`, `${scheme}://*/*`] }, (details, callback) => {
    const url = new URL(details.url);
    if (details.webContentsId !== contents.id || details.resourceType === "mainFrame" || details.resourceType === "subFrame" ||
        !["GET", "HEAD"].includes(details.method) || url.username || url.password || !isAsset(url)) {
      callback({ cancel: true });
      return;
    }
    if (url.origin === origin) {
      callback({ redirectURL: assetOrigin + url.pathname + url.search });
      return;
    }
    callback({});
  });

  const response = await session.fetch(`${origin}/ui/auth`, {
    method: "POST",
    headers: { Origin: origin, "Content-Type": "application/json", "X-Aster-UI": "1" },
    credentials: "include",
    redirect: "error",
    body: JSON.stringify({ ticket: launch.ticket }),
  });
  if (!response.ok || (await response.json()).authenticated !== true) {
    throw new Error("Aster desktop sign-in failed");
  }

  // Chromium uses the supplied base as the document's security origin, so API
  // requests, cookies, uploads and streams use its native same-origin network stack.
  const documentURL = `data:text/html;charset=utf-8;base64,${Buffer.from(html).toString("base64")}`;
  await win.loadURL(documentURL, { baseURLForDataURL: launch.url });
  return documentURL;
}

module.exports = { loadRenderer };
