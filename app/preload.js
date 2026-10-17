const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("yeelight", {
  api: (pathname, options) => ipcRenderer.invoke("api", pathname, options),
  openExternal: (url) => ipcRenderer.invoke("open-external", url),
  revealPath: (targetPath) => ipcRenderer.invoke("reveal-path", targetPath),
  openPath: (targetPath) => ipcRenderer.invoke("open-path", targetPath),
  extensionPaths: () => ipcRenderer.invoke("extension-paths"),
  openRegionPicker: () => ipcRenderer.invoke("open-region-picker"),
  confirmRegionPicker: (rect) => ipcRenderer.invoke("region-picker-confirm", rect),
  cancelRegionPicker: () => ipcRenderer.invoke("region-picker-cancel"),
  setLaunchAtLogin: (enabled) => ipcRenderer.invoke("set-launch-at-login", enabled),
  installIinaPlugin: () => ipcRenderer.invoke("install-iina-plugin"),
  installChromeExtension: () => ipcRenderer.invoke("install-chrome-extension"),
  openScreenRecordingSettings: () => ipcRenderer.invoke("open-screen-recording-settings"),
  ensureMusicPermission: (input, allowRequest, force) =>
    ipcRenderer.invoke("ensure-music-permission", input, !!allowRequest, !!force),
  refreshMusicPermissions: () => ipcRenderer.invoke("refresh-music-permissions"),
  openFullWindow: () => ipcRenderer.invoke("open-full-window"),
  hideTrayPanel: () => ipcRenderer.invoke("hide-tray-panel"),
  resizeTrayPanel: (height) => ipcRenderer.invoke("resize-tray-panel", height),
  onStateChanged: (cb) => {
    const handler = (_evt, payload) => cb(payload);
    ipcRenderer.on("state-changed", handler);
    return () => ipcRenderer.removeListener("state-changed", handler);
  },
  getNativeTheme: () => ipcRenderer.invoke("get-native-theme"),
  onNativeTheme: (cb) => {
    const handler = (_evt, payload) => cb(payload);
    ipcRenderer.on("native-theme", handler);
    return () => ipcRenderer.removeListener("native-theme", handler);
  },
  appInfo: () => ipcRenderer.invoke("app-info"),
});
