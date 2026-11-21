const { contextBridge, ipcRenderer } = require("electron");

if (process.isMainFrame) {
  contextBridge.exposeInMainWorld("asterClipboard", {
    async writeText(text) {
      if (typeof text !== "string" || !navigator.userActivation.isActive ||
          !(await ipcRenderer.invoke("aster:copy-text", text))) {
        throw new Error("Clipboard write unavailable");
      }
    },
  });
}
