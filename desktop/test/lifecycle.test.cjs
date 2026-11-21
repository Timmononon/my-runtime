const assert = require("node:assert/strict");
const { execFileSync, spawnSync } = require("node:child_process");
const { randomUUID } = require("node:crypto");
const { mkdir, mkdtemp, readFile, realpath, rm } = require("node:fs/promises");
const { createServer: createHttpServer } = require("node:http");
const { createServer } = require("node:net");
const { tmpdir } = require("node:os");
const path = require("node:path");
const test = require("node:test");
const { _electron: electron } = require("playwright");

const appDirectory = path.resolve(__dirname, "..");
const installedExe = process.env.ASTER_TEST_DESKTOP_EXE;
const resources = installedExe && path.join(path.dirname(installedExe), "resources");
const python = resources ? path.join(resources, "runtime", "python.exe") :
  path.resolve(appDirectory, "..", ".venv", "Scripts", "python.exe");

async function availablePort() {
  const server = createServer();
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  const { port } = server.address();
  await new Promise((resolve, reject) =>
    server.close((error) => (error ? reject(error) : resolve())),
  );
  return port;
}

async function occupyDefaultPort() {
  const listener = createServer((socket) => socket.destroy());
  try {
    await new Promise((resolve, reject) => {
      listener.once("error", reject);
      listener.listen(8765, "127.0.0.1", resolve);
    });
  } catch (error) {
    if (!["EADDRINUSE", "EACCES"].includes(error.code)) throw error;
    return null;
  }
  return listener;
}

function freshAccount(home) {
  const windows = process.env.SystemRoot;
  return {
    SystemRoot: windows, SystemDrive: process.env.SystemDrive, WINDIR: windows,
    COMSPEC: path.join(windows, "System32", "cmd.exe"), PATHEXT: process.env.PATHEXT,
    PATH: [path.join(windows, "System32"), windows, path.join(windows, "System32", "WindowsPowerShell", "v1.0")].join(";"),
    USERPROFILE: home, APPDATA: path.join(home, "AppData", "Roaming"), LOCALAPPDATA: path.join(home, "AppData", "Local"),
    TMP: path.join(home, "Temp"), TEMP: path.join(home, "Temp"),
  };
}

async function verifyAnswerCopy(desktop, page, text) {
  // Real clipboard access is opt-in on the disposable GitHub-hosted VM only.
  const hosted = process.env.ASTER_TEST_SYSTEM_CLIPBOARD === "1" &&
    process.env.GITHUB_ACTIONS === "true" && process.env.RUNNER_ENVIRONMENT === "github-hosted";
  await desktop.evaluate(({ clipboard }, { hosted, text }) => {
    const nativeWrite = clipboard.writeText;
    const capture = { writes: 0, reject: false };
    const write = async (value) => {
      if (capture.reject || value !== text) throw new Error("Synthetic clipboard rejection");
      if (hosted) await nativeWrite.call(clipboard, value);
      capture.writes++;
    };
    clipboard.writeText = write;
    if (clipboard.writeText !== write) throw new Error("Could not isolate clipboard writes");
    globalThis.asterCopyQA = capture;
  }, { hosted, text });

  await page.getByRole("button", { name: "剪贴板测试会话", exact: true }).click();
  assert.deepEqual(await page.evaluate(() => Object.keys(window.asterClipboard)), ["writeText"]);
  const rejectedInput = await page.evaluate(async () => {
    try { await window.asterClipboard.writeText({ type: "text/html" }); } catch { return true; }
    return false;
  });
  assert.equal(rejectedInput, true);
  await page.getByRole("button", { name: "复制回答", exact: true }).click();
  await page.getByRole("button", { name: "已复制回答", exact: true }).waitFor();
  const matched = await desktop.evaluate(async ({ clipboard }, { hosted, text }) => {
    if (globalThis.asterCopyQA.writes !== 1) return false;
    return !hosted || (await clipboard.readText()).replace(/\r\n/g, "\n") === text;
  }, { hosted, text });
  assert.equal(matched, true);
  if (hosted) console.log("Verified the synthetic answer in the GitHub-hosted Windows system clipboard.");

  await desktop.evaluate(() => { globalThis.asterCopyQA.reject = true; });
  await page.getByRole("button", { name: /^(已)?复制回答$/ }).click();
  await page.getByRole("alert").filter({ hasText: "复制失败，请选中回答文字后按 Ctrl+C 复制。" }).waitFor();
  assert.equal(await page.getByRole("button", { name: "已复制回答", exact: true }).count(), 0);
  assert.equal(await desktop.evaluate(() => globalThis.asterCopyQA.writes), 1);

  const server = createHttpServer((_req, res) => {
    res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
    res.end("<title>Artifact preview</title>");
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  try {
    const opening = desktop.waitForEvent("window");
    await page.evaluate(() => { window.open("about:blank", "_blank"); });
    const preview = await opening;
    const url = `http://127.0.0.1:${server.address().port}/${"a".repeat(43)}/example.html`;
    await preview.goto(url);
    const access = await preview.evaluate(async () => ({
      bridge: typeof window.asterClipboard,
      read: (await navigator.permissions.query({ name: "clipboard-read" })).state,
      write: (await navigator.permissions.query({ name: "clipboard-write" })).state,
    }));
    assert.equal(access.bridge, "undefined");
    assert.notEqual(access.read, "granted");
    assert.notEqual(access.write, "granted");
    await preview.close();
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
}

test("the desktop signs in, reuses the backend, and stops it from the menu", {
  skip: process.platform !== "win32",
  timeout: 210_000,
}, async () => {
  const tempParent = await realpath(tmpdir());
  const temporary = await mkdtemp(path.join(tempParent, "aster-desktop-"));
  const home = path.join(temporary, "测试 用户");
  const env = freshAccount(home);
  const data = installedExe ? path.join(home, ".aster") : path.join(temporary, "运行 数据");
  const workspace = installedExe ? path.join(data, "workspace") : path.join(temporary, "测试 项目");
  const port = await availablePort();
  let origin;
  let desktop;
  let listener;

  async function openWorkbench() {
    const args = [`--user-data-dir=${path.join(temporary, "profile")}`];
    if (!installedExe) args.unshift(appDirectory, `--aster-data-dir=${data}`, `--aster-cwd=${workspace}`, `--aster-port=${port}`);
    desktop = await electron.launch({
      executablePath: installedExe || require("electron"), args, env, cwd: home,
      timeout: 45_000,
    });
    const page = await desktop.firstWindow({ timeout: 45_000 });
    await page.getByText("我的工作空间", { exact: true }).waitFor();
    const bootstrap = await page.evaluate(async () => {
      const response = await fetch("/ui/bootstrap", {
        headers: { "X-Aster-UI": "1" },
      });
      if (!response.ok) throw new Error("Desktop workbench did not authenticate");
      return response.json();
    });
    const url = new URL(await page.evaluate(() => location.href));
    assert.equal(url.hostname, "127.0.0.1");
    assert.equal(url.pathname, "/");
    if (installedExe) assert.notEqual(url.port, "8765");
    else assert.equal(url.port, String(port));
    if (origin) assert.equal(url.origin, origin);
    origin = url.origin;
    assert.equal(bootstrap.cwd, workspace);
    assert.equal(await page.evaluate(() => typeof window.require), "undefined");
    return { page, instanceId: bootstrap.instance_id, configured: bootstrap.provider_configured };
  }

  async function closeWindow() {
    const closed = desktop.waitForEvent("close", { timeout: 20_000 });
    await desktop.evaluate(({ BrowserWindow }) => {
      setImmediate(() => BrowserWindow.getAllWindows()[0].close());
    });
    await closed;
    desktop = undefined;
  }

  try {
    await Promise.all([env.APPDATA, env.LOCALAPPDATA, env.TEMP].map((dir) => mkdir(dir, { recursive: true })));
    if (installedExe) listener = await occupyDefaultPort();
    else await mkdir(workspace);
    const { page, instanceId: firstInstance, configured } = await openWorkbench();
    assert.equal(configured, false);
    if (installedExe) {
      const running = JSON.parse(await readFile(path.join(data, "service.json"), "utf8"));
      assert.equal(await realpath(running.executable), await realpath(python));
      const blocked = spawnSync(python, [path.join(resources, "launch.py"), "--stop-installed", "--data-dir", data], {
        env, windowsHide: true, encoding: "utf8", timeout: 20_000,
      });
      assert.equal(blocked.status, 2);
      assert.match(blocked.stderr, /Close the installed Aster desktop/);

      const fakeKey = "aster-offline-setup-verification";
      await page.getByRole("button", { name: "配置模型密钥", exact: true }).click();
      await page.getByPlaceholder("输入密钥，仅用于本机保存").fill(fakeKey);
      await page.getByRole("button", { name: "保存提供商", exact: true }).click();
      await page.getByText("已保存。后续运行将使用新的配置。", { exact: true }).waitFor();
      assert.equal(await page.getByPlaceholder("输入新密钥以替换").inputValue(), "");
      assert.ok((await readFile(path.join(data, "credentials.yaml"), "utf8")).includes(fakeKey));
      const providers = await page.evaluate(async () => {
        const response = await fetch("/providers", { headers: { "X-Aster-UI": "1" } });
        return response.json();
      });
      assert.ok(providers.providers.some((provider) => provider.id === "deepseek" && provider.configured));
      assert.equal(JSON.stringify(providers).includes(fakeKey), false);
    }
    const policy = await page.locator('meta[http-equiv="Content-Security-Policy"]').getAttribute("content");
    assert.match(policy, /connect-src 'self'/);
    assert.match(policy, /frame-src 'none'/);
    assert.equal(await page.locator('meta[name="referrer"]').getAttribute("content"), "no-referrer");
    const entryScript = await page.locator("script[type='module'][src]").getAttribute("src");
    assert.equal(new URL(entryScript).protocol, "aster-assets:");
    for (const url of ["/", new URL(entryScript).pathname]) {
      const response = await fetch(new URL(url, origin));
      assert.ok([401, 404].includes(response.status));
      assert.match(response.headers.get("content-type"), /application\/json/);
    }

    await page.reload();
    await page.getByText("我的工作空间", { exact: true }).waitFor();
    const navigation = page.getByRole("navigation", { name: "主导航" });
    await navigation.getByRole("button", { name: "GitHub", exact: true }).click();
    await page.getByRole("heading", { name: "GitHub", exact: true }).waitFor();

    const roundTrip = await page.evaluate(async (cwd) => {
      const headers = { "X-Aster-UI": "1" };
      const created = await fetch("/sessions", {
        method: "POST", headers: { ...headers, "Content-Type": "application/json" },
        body: JSON.stringify({ cwd, title: "桌面端附件验收" }),
      });
      if (created.status !== 201) throw new Error(`Session creation failed (${created.status})`);
      const session = await created.json();
      const content = "来自桌面工作台的附件。";
      const uploaded = await fetch(`/sessions/${session.id}/attachments?filename=desktop.txt`, {
        method: "POST", headers: { ...headers, "Content-Type": "text/plain" },
        body: new Blob([content], { type: "text/plain" }),
      });
      if (uploaded.status !== 201) throw new Error(`Attachment upload failed (${uploaded.status})`);
      const attachment = await uploaded.json();
      const download = await fetch(`/artifacts/${attachment.artifact_id}`, { headers });
      return {
        cookieHidden: !document.cookie.includes("aster_ui="),
        equal: download.ok && (await download.text()) === content,
      };
    }, workspace);
    assert.deepEqual(roundTrip, { cookieHidden: true, equal: true });
    await closeWindow();

    const health = await fetch(`${origin}/health`);
    assert.equal(health.status, 200);
    assert.equal((await health.json()).instance_id, firstInstance);
    const reopened = await openWorkbench();
    assert.equal(reopened.instanceId, firstInstance);
    assert.equal(reopened.configured, Boolean(installedExe));

    const appClosed = desktop.waitForEvent("close", { timeout: 45_000 });
    await desktop.evaluate(({ Menu }) => {
      const menu = Menu.getApplicationMenu().items.find((item) => item.label === "Aster");
      const stop = menu?.submenu.items.find((item) => item.label === "退出并停止后台");
      if (!stop?.enabled) throw new Error("Missing stop-and-quit menu action");
      setImmediate(() => stop.click());
    });
    await appClosed;
    desktop = undefined;

    const status = JSON.parse(execFileSync(python, [
      "-X", "utf8", "-m", "aster", "--data-dir", data, "daemon", "status",
    ], { env, encoding: "utf8", windowsHide: true, timeout: 15_000 }));
    assert.equal(status.running, false);
    assert.equal(status.locked, false);
    const copiedText = `Aster 剪贴板验收 Ω ${randomUUID()}\n第二行保留原样`;
    const seed = [
      "import sys",
      "from pathlib import Path",
      "from aster.domain import Message",
      "from aster.state.store import Store",
      "store=Store(Path(sys.argv[1])/'runtime.db')",
      "session=store.create_session(sys.argv[2],title='剪贴板测试会话')",
      "store.append_message(session['id'],None,Message('assistant',sys.argv[3]))",
      "store.close()",
    ].join("\n");
    execFileSync(python, ["-X", "utf8", "-c", seed, data, workspace, copiedText], { env, windowsHide: true });
    origin = undefined;
    const restarted = await openWorkbench();
    assert.notEqual(restarted.instanceId, firstInstance);
    assert.equal(restarted.configured, Boolean(installedExe));
    await verifyAnswerCopy(desktop, restarted.page, copiedText);
    await closeWindow();
    if (installedExe) {
      execFileSync(python, [path.join(resources, "launch.py"), "--stop-installed", "--data-dir", data], {
        env, windowsHide: true, timeout: 40_000,
      });
      const afterSetup = JSON.parse(execFileSync(python, ["-m", "aster", "--data-dir", data, "daemon", "status"], {
        env, encoding: "utf8", windowsHide: true, timeout: 15_000,
      }));
      assert.equal(afterSetup.running, false);
      assert.equal(afterSetup.locked, false);
    }
  } finally {
    try {
      await desktop?.close();
    } finally {
      if (listener) await new Promise((resolve) => listener.close(resolve));
      execFileSync(python, ["-X", "utf8", "-m", "aster", "--data-dir", data, "daemon", "stop"], {
        env, windowsHide: true,
        timeout: 30_000,
      });
      assert.equal(path.dirname(await realpath(temporary)), tempParent);
      await rm(temporary, { recursive: true, force: true, maxRetries: 4, retryDelay: 200 });
    }
  }
});
