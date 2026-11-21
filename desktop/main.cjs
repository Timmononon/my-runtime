const { app, BrowserWindow, Menu, clipboard, dialog, shell } = require("electron");
const { execFile } = require("node:child_process");
const { existsSync } = require("node:fs");
const path = require("node:path");
const { promisify } = require("node:util");
const { loadRenderer } = require("./renderer.cjs");

const runtimeRoot = app.isPackaged ? process.resourcesPath : path.resolve(__dirname, "..");
const desktopScripts = app.isPackaged ? runtimeRoot : __dirname;
const launcher = path.join(desktopScripts, "launch.py");
const icon = path.join(__dirname, "assets", "aster.ico");
const runFile = promisify(execFile);
const workbenchAddress = /^http:\/\/(?:127\.0\.0\.1|localhost|\[::1\]):([1-9]\d{0,4})\/$/;
const loginTicket = /^[A-Za-z0-9_-]{32,128}$/;
const previewPath = /^\/[A-Za-z0-9_-]{43}\/.+/;
const linkDispositions = new Set(["foreground-tab", "background-tab", "new-window"]);
const webPreferences = {
  contextIsolation: true,
  nodeIntegration: false,
  sandbox: true,
  partition: "aster-desktop",
};

let workbench;
let stopping = false;

function parseURL(value) {
  try {
    return new URL(value);
  } catch {
    return null;
  }
}

async function runPython(python, args, timeout) {
  const { stdout } = await runFile(python, ["-X", "utf8", ...args], {
    shell: false,
    windowsHide: true,
    cwd: runtimeRoot,
    encoding: "utf8",
    maxBuffer: 64 * 1024,
    timeout,
  });
  return JSON.parse(stdout);
}

async function launchWorkbench(python) {
  const cwd = app.commandLine.getSwitchValue("aster-cwd") || (app.isPackaged ? "" : runtimeRoot);
  const port = app.commandLine.getSwitchValue("aster-port");
  const dataDir = app.commandLine.getSwitchValue("aster-data-dir");
  const args = [launcher];
  if (cwd) args.push("--cwd", cwd);
  if (port) args.push("--port", port);
  if (dataDir) args.push("--data-dir", dataDir);

  const launch = await runPython(python, args, 65_000);
  const portMatch = typeof launch?.url === "string" && workbenchAddress.exec(launch.url);
  if (!portMatch || portMatch[0] !== launch.url || Number(portMatch[1]) > 65_535 ||
      typeof launch.ticket !== "string" || loginTicket.exec(launch.ticket)?.[0] !== launch.ticket ||
      typeof launch.data_dir !== "string" || !path.isAbsolute(launch.data_dir) ||
      typeof launch.static_root !== "string" || !path.isAbsolute(launch.static_root)) {
    throw new Error("Invalid response from the Aster launcher");
  }
  return launch;
}

function restrictNavigation(contents, allows) {
  const guard = (event) => {
    if (!allows(parseURL(event.url))) event.preventDefault();
  };
  contents.on("will-navigate", guard);
  contents.on("will-redirect", guard);
}

function configureWorkbench(win, origin) {
  const contents = win.webContents;
  const isPreview = (url) => url?.protocol === "http:" && url.hostname === "127.0.0.1" &&
    url.port && url.origin !== origin && !url.username && !url.password && previewPath.test(url.pathname);

  restrictNavigation(contents, (url) => url?.origin === origin);

  contents.session.setPermissionCheckHandler(() => false);
  contents.session.setPermissionRequestHandler((_requester, _permission, callback) => callback(false));

  // The workbench cookie is host-scoped. Preview servers on other loopback ports do not need it.
  contents.session.webRequest.onBeforeSendHeaders((details, callback) => {
    if (parseURL(details.url)?.origin !== origin) {
      for (const header of Object.keys(details.requestHeaders)) {
        if (header.toLowerCase() === "cookie") delete details.requestHeaders[header];
      }
    }
    callback({ requestHeaders: details.requestHeaders });
  });

  contents.setWindowOpenHandler(({ url: target, disposition, postBody }) => {
    const url = parseURL(target);
    if (!postBody && (target === "about:blank" || isPreview(url))) {
      return { action: "allow", overrideBrowserWindowOptions: {
        parent: win, autoHideMenuBar: true, webPreferences: { preload: "" },
      } };
    }
    if (linkDispositions.has(disposition) && !postBody &&
        (url?.protocol === "https:" || url?.protocol === "http:") && !url.username && !url.password) {
      void shell.openExternal(url.href).catch(() => {
        dialog.showErrorBox("无法打开链接", "请检查 Windows 的默认浏览器设置。");
      });
    }
    return { action: "deny" };
  });
  contents.on("did-create-window", (preview) => {
    // The existing preview button opens about:blank before requesting a lease.
    preview.setMenu(null);
    restrictNavigation(preview.webContents, isPreview);
    preview.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  });
}

function installMenu(python, dataDir) {
  Menu.setApplicationMenu(Menu.buildFromTemplate([
    {
      label: "Aster",
      submenu: [{
        label: "连接飞书…",
        async click(item) {
          item.enabled = false;
          try {
            const result = await runPython(python, [
              path.join(desktopScripts, "feishu_connect.py"), "--launch-terminal", "--data-dir", dataDir,
            ], 10_000);
            if (!result?.launched) throw new Error("Feishu connection window did not start");
          } catch {
            dialog.showErrorBox("未能打开飞书连接窗口", "请检查 Aster 安装目录的完整性后重试。");
          } finally {
            item.enabled = true;
          }
        },
      }, { type: "separator" }, {
        label: "退出并停止后台",
        async click(item) {
          if (stopping) return;
          stopping = true;
          item.enabled = false;
          try {
            const result = await runPython(python, ["-m", "aster", "--data-dir", dataDir, "daemon", "stop"], 45_000);
            if (result?.stopped !== true) throw new Error("The Aster runtime is still running");
          } catch {
            dialog.showErrorBox("未能停止 Aster 后台", "请查看 Aster 后台日志，再试一次。");
            return;
          } finally {
            stopping = false;
            item.enabled = true;
          }
          app.quit();
        },
      }],
    },
    {
      label: "编辑",
      submenu: [
        { role: "undo" },
        { role: "redo" },
        { type: "separator" },
        { role: "cut" },
        { role: "copy" },
        { role: "paste" },
        { role: "selectAll" },
      ],
    },
  ]));
}

async function main() {
  const python = app.isPackaged ? path.join(runtimeRoot, "runtime", "python.exe") :
    app.commandLine.getSwitchValue("aster-python") || path.join(runtimeRoot, ".venv", "Scripts", "python.exe");
  if (!existsSync(python) || !existsSync(launcher)) {
    dialog.showErrorBox("Aster 运行组件缺失", app.isPackaged ? "请重新运行 Aster 安装程序。" : "请检查项目的 Python 环境和桌面启动脚本。");
    app.exit(1);
    return;
  }
  const launch = await launchWorkbench(python);

  installMenu(python, launch.data_dir);
  const win = new BrowserWindow({
    title: "Aster",
    width: 1280,
    height: 900,
    minWidth: 640,
    minHeight: 450,
    backgroundColor: "#f8f8f5",
    show: false,
    icon: existsSync(icon) ? icon : undefined,
    webPreferences: { ...webPreferences, preload: path.join(__dirname, "preload.cjs") },
  });
  workbench = win;
  win.on("close", (event) => {
    if (stopping) event.preventDefault();
  });
  win.on("closed", () => {
    workbench = undefined;
    app.quit();
  });
  const origin = new URL(launch.url).origin;
  configureWorkbench(win, origin);
  const documentURL = await loadRenderer(win, launch);
  const contents = win.webContents;
  contents.ipc.handle("aster:copy-text", async (event, text) => {
    const frame = event.senderFrame;
    // Chromium gives our local document a data: URL and a loopback security origin.
    if (event.sender !== contents || !frame || frame !== contents.mainFrame ||
        frame.url !== documentURL || frame.origin !== origin || typeof text !== "string") return false;
    try {
      await clipboard.writeText(text);
      return true;
    } catch {
      return false;
    }
  });
  win.show();
}

app.setName("Aster");
app.setAppUserModelId("Aster.Desktop");
const profile = app.commandLine.getSwitchValue("user-data-dir");
if (profile) app.setPath("userData", path.resolve(profile));

if (app.requestSingleInstanceLock()) {
  app.on("second-instance", () => {
    if (!workbench) return;
    if (workbench.isMinimized()) workbench.restore();
    workbench.focus();
  });
  app.whenReady().then(main).catch(() => {
    // Launcher errors can include private sign-in output.
    dialog.showErrorBox("无法打开 Aster", "请检查 Aster 的工作区、Python 环境和后台日志，然后重试。");
    app.exit(1);
  });
} else {
  app.quit();
}
