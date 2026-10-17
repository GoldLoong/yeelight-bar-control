const {
  app,
  BrowserWindow,
  Tray,
  Menu,
  nativeImage,
  ipcMain,
  shell,
  screen,
  systemPreferences,
  nativeTheme,
  desktopCapturer,
} = require("electron");
const path = require("path");
const fs = require("fs");
const { spawn, execFile } = require("child_process");
const http = require("http");

const API = "http://127.0.0.1:8787";
const BUNDLE_ID = "com.yeelightbar.control";
const PRODUCT = "Yeelight 显示器挂灯";
const NEED_REV = 4;

app.setName(PRODUCT);
app.setPath("userData", path.join(app.getPath("appData"), BUNDLE_ID));

let mainWindow = null;
let trayWindow = null;
let tray = null;
let pyProc = null;
let quitting = false;
let regionPickerWindow = null;
let regionPickerResolve = null;
let trayBlurTimer = null;

function isPackaged() {
  return app.isPackaged;
}

function resourcesRoot() {
  if (isPackaged()) return process.resourcesPath;
  return path.join(__dirname, "..");
}

function configDir() {
  return path.join(app.getPath("appData"), BUNDLE_ID);
}

function pythonBin() {
  if (process.env.YEELIGHT_PYTHON) return process.env.YEELIGHT_PYTHON;
  if (!isPackaged()) {
    const venv = path.join(resourcesRoot(), ".venv", "bin", "python");
    if (fs.existsSync(venv)) return venv;
    return "python3";
  }
  const arch = process.arch === "arm64" ? "arm64" : "x64";
  const candidates = [
    path.join(process.resourcesPath, "python", "bin", "python3"),
    path.join(process.resourcesPath, "python", arch, "bin", "python3"),
    path.join(process.resourcesPath, "python", arch, "bin", "python"),
    path.join(process.resourcesPath, "python", arch === "arm64" ? "x64" : "arm64", "bin", "python3"),
  ];
  for (const p of candidates) {
    if (fs.existsSync(p)) return p;
  }
  return "python3";
}

function pythonHome() {
  if (!isPackaged()) return "";
  const bin = pythonBin();
  if (!bin || bin === "python3") return "";
  // .../python/x64/bin/python3 -> .../python/x64
  return path.dirname(path.dirname(bin));
}

function mediaStatus(kind) {
  try {
    if (systemPreferences && typeof systemPreferences.getMediaAccessStatus === "function") {
      return String(systemPreferences.getMediaAccessStatus(kind) || "");
    }
  } catch (_) {}
  return "";
}

function musicPermissionsPath() {
  return path.join(configDir(), "music-permissions.json");
}

/** 进程内只探测/打开设置各一次；新版本首次启动会重置后再检查一次。 */
let screenProbeOnce = false;
let settingsOpenedScreen = false;
let settingsOpenedMic = false;

function readMusicPermissionsFile() {
  try {
    const raw = JSON.parse(fs.readFileSync(musicPermissionsPath(), "utf8"));
    return raw && typeof raw === "object" ? raw : {};
  } catch (_) {
    return {};
  }
}

function pickFlag(extra, key, memoryVal, prev) {
  if (Object.prototype.hasOwnProperty.call(extra, key)) return !!extra[key];
  return !!(prev[key] || memoryVal);
}

function writeMusicPermissions(extra = {}) {
  const prev = readMusicPermissionsFile();
  const electronScreen = mediaStatus("screen");
  const electronMic = mediaStatus("microphone");
  // 录屏授权只认：Electron granted，或本次由 CGPreflight 确认。
  const preflightOk = extra.preflight === true || extra.source === "preflight";
  let screen = "";
  if (electronScreen === "granted" || preflightOk) screen = "granted";
  else if (extra.screen != null) screen = String(extra.screen);
  else if (electronScreen) screen = electronScreen;
  else {
    screen =
      prev.screen === "granted" && prev.screenValidated && prev.source === "preflight"
        ? "granted"
        : electronScreen || prev.screen || "";
  }

  let microphone = electronMic || "";
  if (extra.microphone != null) microphone = String(extra.microphone);
  else if (electronMic === "granted") microphone = "granted";
  else if (prev.micValidated && prev.microphone === "granted") microphone = "granted";

  const screenValidated =
    screen === "granted" && (electronScreen === "granted" || preflightOk || prev.source === "preflight");
  const micValidated =
    microphone === "granted" &&
    (extra.micValidated === true ||
      extra.source === "validated" ||
      electronMic === "granted" ||
      !!prev.micValidated);

  const payload = {
    screen,
    microphone,
    screenValidated,
    micValidated,
    electronScreen,
    electronMic,
    updatedAt: Date.now(),
    request: extra.request || prev.request || "",
    source: preflightOk
      ? "preflight"
      : extra.source || (electronScreen === "granted" ? "electron" : prev.source || ""),
    screenProbeOnce: pickFlag(extra, "screenProbeOnce", screenProbeOnce, prev),
    settingsOpenedScreen: pickFlag(extra, "settingsOpenedScreen", settingsOpenedScreen, prev),
    settingsOpenedMic: pickFlag(extra, "settingsOpenedMic", settingsOpenedMic, prev),
    permissionCheckVersion:
      extra.permissionCheckVersion != null
        ? String(extra.permissionCheckVersion)
        : prev.permissionCheckVersion || "",
  };
  try {
    fs.mkdirSync(configDir(), { recursive: true });
    fs.writeFileSync(musicPermissionsPath(), JSON.stringify(payload), "utf8");
  } catch (_) {}
  // #region agent log
  fetch("http://127.0.0.1:7799/ingest/7bf1365e-5871-4c08-866c-f5f2d726fc79", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Debug-Session-Id": "d219fd" },
    body: JSON.stringify({
      sessionId: "d219fd",
      hypothesisId: "P1",
      location: "main.js:writeMusicPermissions",
      message: "wrote music permissions",
      data: {
        screen: payload.screen,
        electronScreen: payload.electronScreen,
        screenValidated: payload.screenValidated,
        source: payload.source,
        preflightOk,
        permissionCheckVersion: payload.permissionCheckVersion,
      },
      timestamp: Date.now(),
    }),
  }).catch(() => {});
  // #endregion
  return payload;
}

async function fetchServerMusicPermissions() {
  try {
    const r = await api("/api/permissions/music", { timeout: 3000, silent: true });
    return r && r.data ? r.data : null;
  } catch (_) {
    return null;
  }
}

async function openScreenRecordingSettings() {
  const urls = [
    "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture",
    "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?Privacy_ScreenCapture",
  ];
  for (const u of urls) {
    try {
      await shell.openExternal(u);
      break;
    } catch (_) {}
  }
  return { ok: true };
}

/**
 * 真正向系统登记/申请屏幕录制。
 * 只打开设置页不会让新版本出现在列表里；必须先触达录屏 API。
 * 未签名应用每次替换后 TCC 会失效，force 时应无视「已 denied / 已探测过」。
 */
async function probeScreenCaptureAccess({ force = false } = {}) {
  const before = mediaStatus("screen");
  let probed = false;
  let probeError = "";
  if (force || !screenProbeOnce) {
    screenProbeOnce = true;
    probed = true;
    if (typeof desktopCapturer?.getSources === "function") {
      try {
        await desktopCapturer.getSources({
          types: ["screen"],
          thumbnailSize: { width: 1, height: 1 },
        });
      } catch (err) {
        probeError = String(err && err.message ? err.message : err).slice(0, 200);
      }
    }
    // 同步用 CGRequest 再挂一次（Python 子进程身份）；失败忽略。
    try {
      await api("/api/permissions/screen/request", { method: "POST", timeout: 4000, silent: true });
    } catch (_) {}
  }
  writeMusicPermissions({ screenProbeOnce: true });
  const server = await fetchServerMusicPermissions();
  const after = mediaStatus("screen");
  const ok = after === "granted" || server?.preflight === true;
  // #region agent log
  fetch("http://127.0.0.1:7799/ingest/7bf1365e-5871-4c08-866c-f5f2d726fc79", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Debug-Session-Id": "d219fd" },
    body: JSON.stringify({
      sessionId: "d219fd",
      hypothesisId: "TCC1",
      location: "main.js:probeScreenCaptureAccess",
      message: "screen probe",
      data: {
        force,
        probed,
        before,
        after,
        preflight: server?.preflight,
        ok,
        probeError: probeError || undefined,
      },
      timestamp: Date.now(),
    }),
  }).catch(() => {});
  // #endregion
  if (ok) {
    writeMusicPermissions({
      screen: "granted",
      source: server?.preflight ? "preflight" : "electron",
      preflight: server?.preflight === true,
      screenValidated: true,
      screenProbeOnce: true,
    });
  }
  return { ok, before, after, preflight: server?.preflight, probed, probeError };
}

async function openMicrophoneSettings() {
  const urls = [
    "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone",
    "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?Privacy_Microphone",
  ];
  for (const u of urls) {
    try {
      await shell.openExternal(u);
      break;
    } catch (_) {}
  }
  return { ok: true };
}

/**
 * @param {string} input mic|system
 * @param {{ allowRequest?: boolean, force?: boolean }} [opts]
 *   allowRequest：允许探测/打开设置（新版本首次或用户点击）
 *   force：无视本版已探测标记，再次触达录屏 API（更新后 TCC 失效时必需）
 */
async function ensureMusicPermission(input, opts = {}) {
  const kind = input === "mic" ? "mic" : "system";
  const allowRequest = opts.allowRequest === true;
  const force = opts.force === true;
  const server = await fetchServerMusicPermissions();
  if (kind === "mic") {
    let perms = writeMusicPermissions({ request: kind });
    const serverGranted =
      server?.effective_microphone === "granted" || Number(server?.mic_auth) === 3;
    if (serverGranted || perms.microphone === "granted" || mediaStatus("microphone") === "granted") {
      perms = writeMusicPermissions({
        request: kind,
        microphone: "granted",
        source: "validated",
        micValidated: true,
      });
      return { ok: true, input: kind, ...perms };
    }
    if (!allowRequest) {
      return { ok: false, input: kind, needsPermission: true, ...perms };
    }
    if (typeof systemPreferences.askForMediaAccess === "function") {
      try {
        await systemPreferences.askForMediaAccess("microphone");
      } catch (_) {}
      perms = writeMusicPermissions({ request: kind });
    }
    const again = await fetchServerMusicPermissions();
    if (Number(again?.mic_auth) === 3 || mediaStatus("microphone") === "granted") {
      perms = writeMusicPermissions({
        request: kind,
        microphone: "granted",
        source: "validated",
        micValidated: true,
      });
      return { ok: true, input: kind, ...perms };
    }
    const prev = readMusicPermissionsFile();
    settingsOpenedMic = settingsOpenedMic || !!prev.settingsOpenedMic;
    if (force || !settingsOpenedMic) {
      settingsOpenedMic = true;
      await openMicrophoneSettings();
      perms = writeMusicPermissions({ request: kind, settingsOpenedMic: true });
    } else {
      perms = writeMusicPermissions({ request: kind });
    }
    return { ok: perms.microphone === "granted", input: kind, ...perms };
  }

  // —— 屏幕录制 / 系统声音 ——
  const electronScreen = mediaStatus("screen");
  const preflight = server?.preflight === true;
  // #region agent log
  fetch("http://127.0.0.1:7799/ingest/7bf1365e-5871-4c08-866c-f5f2d726fc79", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Debug-Session-Id": "d219fd" },
    body: JSON.stringify({
      sessionId: "d219fd",
      hypothesisId: "TCC1",
      location: "main.js:ensureMusicPermission",
      message: "screen ensure enter",
      data: { electronScreen, preflight, probeOnce: screenProbeOnce, allowRequest, force },
      timestamp: Date.now(),
    }),
  }).catch(() => {});
  // #endregion

  if (!force && (preflight || electronScreen === "granted")) {
    const perms = writeMusicPermissions({
      request: kind,
      screen: "granted",
      source: preflight ? "preflight" : "electron",
      preflight,
      screenValidated: true,
    });
    return { ok: true, input: kind, ...perms, preflight };
  }

  let perms = writeMusicPermissions({
    request: kind,
    screen: electronScreen || "denied",
    source: "unset",
    screenValidated: false,
  });

  if (!allowRequest) {
    return { ok: false, input: kind, needsPermission: true, ...perms };
  }

  const prev = readMusicPermissionsFile();
  if (!force) {
    screenProbeOnce = screenProbeOnce || !!prev.screenProbeOnce;
    settingsOpenedScreen = settingsOpenedScreen || !!prev.settingsOpenedScreen;
  } else {
    // 用户重试 / 新版本：必须重新触达 API，否则设置页看不到本程序新实例
    screenProbeOnce = false;
  }

  const probe = await probeScreenCaptureAccess({ force: force || !screenProbeOnce });
  if (probe.ok) {
    perms = readMusicPermissionsFile();
    return { ok: true, input: kind, ...perms, preflight: probe.preflight === true };
  }

  // 用户强制重试时每次都打开设置；版本首次只开一次
  if (force || !settingsOpenedScreen) {
    settingsOpenedScreen = true;
    await openScreenRecordingSettings();
    perms = writeMusicPermissions({
      request: kind,
      settingsOpenedScreen: true,
      screenProbeOnce: true,
    });
  }
  // #region agent log
  fetch("http://127.0.0.1:7799/ingest/7bf1365e-5871-4c08-866c-f5f2d726fc79", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Debug-Session-Id": "d219fd" },
    body: JSON.stringify({
      sessionId: "d219fd",
      hypothesisId: "TCC1",
      location: "main.js:ensureMusicPermission",
      message: "screen ensure unresolved after probe",
      data: {
        electronScreen: mediaStatus("screen"),
        openedSettings: settingsOpenedScreen,
        probeOnce: screenProbeOnce,
        force,
        probed: probe.probed,
      },
      timestamp: Date.now(),
    }),
  }).catch(() => {});
  // #endregion
  return { ok: false, input: kind, needsPermission: true, ...perms };
}

/** 每个新版本首次启动：重置一次性标记并申请/检查一次；同版本后续启动跳过。 */
async function runVersionFirstLaunchPermissionCheck(cfg) {
  const ver = app.getVersion();
  const prev = readMusicPermissionsFile();
  if (prev.permissionCheckVersion === ver) {
    screenProbeOnce = !!prev.screenProbeOnce;
    settingsOpenedScreen = !!prev.settingsOpenedScreen;
    settingsOpenedMic = !!prev.settingsOpenedMic;
    writeMusicPermissions();
    // #region agent log
    fetch("http://127.0.0.1:7799/ingest/7bf1365e-5871-4c08-866c-f5f2d726fc79", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Debug-Session-Id": "d219fd" },
      body: JSON.stringify({
        sessionId: "d219fd",
        hypothesisId: "V1",
        location: "main.js:runVersionFirstLaunchPermissionCheck",
        message: "skip — same version already checked",
        data: { ver },
        timestamp: Date.now(),
      }),
    }).catch(() => {});
    // #endregion
    return { ran: false, version: ver };
  }

  screenProbeOnce = false;
  settingsOpenedScreen = false;
  settingsOpenedMic = false;
  writeMusicPermissions({
    screenProbeOnce: false,
    settingsOpenedScreen: false,
    settingsOpenedMic: false,
    permissionCheckVersion: "",
  });

  // #region agent log
  fetch("http://127.0.0.1:7799/ingest/7bf1365e-5871-4c08-866c-f5f2d726fc79", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Debug-Session-Id": "d219fd" },
    body: JSON.stringify({
      sessionId: "d219fd",
      hypothesisId: "TCC1",
      location: "main.js:runVersionFirstLaunchPermissionCheck",
      message: "new version first launch — force probe once",
      data: { ver, prev: prev.permissionCheckVersion || "" },
      timestamp: Date.now(),
    }),
  }).catch(() => {});
  // #endregion

  // 新版本必须 force：未签名更新后旧 TCC 条目失效，且状态常误报 denied，不探测就进不了列表
  let screenResult = null;
  let micResult = null;
  try {
    screenResult = await ensureMusicPermission("system", { allowRequest: true, force: true });
    if (cfg?.drive_mode === "music" && cfg?.music?.input === "mic") {
      micResult = await ensureMusicPermission("mic", { allowRequest: true, force: true });
    }
  } finally {
    writeMusicPermissions({ permissionCheckVersion: ver });
  }
  return { ran: true, version: ver, screenResult, micResult };
}

function pythonEnv() {
  const root = resourcesRoot();
  const perms = writeMusicPermissions();
  const env = {
    ...process.env,
    PYTHONUNBUFFERED: "1",
    PYTHONPATH: [root, process.env.PYTHONPATH || ""].filter(Boolean).join(":"),
    YEELIGHT_CONFIG_DIR: configDir(),
    YEELIGHT_EXAMPLE_CONFIG: path.join(root, "config.example.json"),
    YEELIGHT_LEGACY_CONFIG: isPackaged() ? "" : path.join(root, "config.json"),
    YEELIGHT_SCREEN_ACCESS: perms.screen || "",
    YEELIGHT_MIC_ACCESS: perms.microphone || "",
  };
  const home = pythonHome();
  if (home) env.PYTHONHOME = home;
  return env;
}

function extraPath(...parts) {
  return path.join(resourcesRoot(), ...parts);
}

function api(pathname, options = {}) {
  return new Promise((resolve, reject) => {
    const url = new URL(pathname, API);
    const timeout = options.timeout || (String(pathname).includes("/api/state") ? 12000 : 8000);
    const method = String(options.method || "GET").toUpperCase();
    const req = http.request(
      url,
      {
        method,
        headers: { "Content-Type": "application/json", ...(options.headers || {}) },
        timeout,
      },
      (res) => {
        let body = "";
        res.on("data", (c) => (body += c));
        res.on("end", () => {
          try {
            const result = { status: res.statusCode, data: body ? JSON.parse(body) : null };
            if (
              method !== "GET" &&
              method !== "HEAD" &&
              result.status >= 200 &&
              result.status < 300 &&
              !options.silent
            ) {
              // 下一 tick，避免渲染进程还在等 invoke 时就撞上强制刷新
              setImmediate(() =>
                broadcastStateChanged(null, {
                  pathname,
                  method,
                  state: result.data?.state || null,
                })
              );
            }
            resolve(result);
          } catch (e) {
            reject(e);
          }
        });
      }
    );
    req.on("error", reject);
    req.on("timeout", () => {
      req.destroy();
      reject(new Error("api timeout"));
    });
    if (options.body) req.write(JSON.stringify(options.body));
    req.end();
  });
}

function broadcastStateChanged(exceptWcId = null, payload = {}) {
  for (const win of BrowserWindow.getAllWindows()) {
    if (win.isDestroyed()) continue;
    if (exceptWcId != null && win.webContents.id === exceptWcId) continue;
    try {
      win.webContents.send("state-changed", payload);
    } catch (_) {}
  }
}

async function waitForApi(retries = 48) {
  for (let i = 0; i < retries; i++) {
    try {
      const r = await api("/api/health");
      if (r.data && r.data.ok) return true;
    } catch (_) {}
    await new Promise((r) => setTimeout(r, 250));
  }
  return false;
}

function startPython() {
  if (pyProc) return;
  const python = pythonBin();
  const env = pythonEnv();
  pyProc = spawn(python, ["-m", "server.main"], {
    cwd: resourcesRoot(),
    env,
    stdio: ["ignore", "pipe", "pipe"],
  });
  pyProc.stdout.on("data", (d) => console.log(`[py] ${d}`));
  pyProc.stderr.on("data", (d) => console.error(`[py] ${d}`));
  pyProc.on("exit", (code) => {
    console.log(`python exited: ${code}`);
    pyProc = null;
  });
}

function stopPython() {
  if (!pyProc) return;
  pyProc.kill("SIGTERM");
  pyProc = null;
}

function applyAccessoryPolicy() {
  try {
    if (typeof app.dock?.hide === "function") app.dock.hide();
  } catch (_) {}
  try {
    if (typeof app.setActivationPolicy === "function") {
      app.setActivationPolicy("accessory");
    }
  } catch (_) {}
}

function attachWindowHide(win) {
  win.on("close", (e) => {
    if (!quitting) {
      e.preventDefault();
      win.hide();
    }
  });
  win.on("minimize", () => {
    win.hide();
  });
}

function createWindow() {
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.show();
    mainWindow.focus();
    try {
      mainWindow.webContents.send("state-changed", { reason: "show" });
    } catch (_) {}
    return;
  }
  mainWindow = new BrowserWindow({
    width: 400,
    height: 760,
    minWidth: 360,
    minHeight: 640,
    title: PRODUCT,
    backgroundColor: "#000000",
    show: false,
    skipTaskbar: true,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  mainWindow.loadFile(path.join(__dirname, "renderer", "index.html"));
  attachWindowHide(mainWindow);
  mainWindow.once("ready-to-show", () => {
    mainWindow.show();
    mainWindow.focus();
  });
}

function trayIcon() {
  const iconDir = path.join(__dirname, "build");
  const tpl2x = path.join(iconDir, "trayTemplate@2x.png");
  const tpl1x = path.join(iconDir, "trayTemplate.png");
  try {
    // createFromPath 会识别 @2x；勿把 36px 图当成 36pt，否则菜单栏图标过大
    if (fs.existsSync(tpl2x)) {
      const img = nativeImage.createFromPath(tpl2x);
      img.setTemplateImage(true);
      return img;
    }
    if (fs.existsSync(tpl1x)) {
      const img = nativeImage.createFromPath(tpl1x);
      img.setTemplateImage(true);
      return img;
    }
  } catch (_) {}
  const svg = `
    <svg xmlns="http://www.w3.org/2000/svg" width="36" height="36" viewBox="0 0 18 18">
      <rect x="4" y="2.5" width="10" height="2.1" rx="1.05" fill="black"/>
      <rect x="3.2" y="5.4" width="11.6" height="8.8" rx="1.5" fill="none" stroke="black" stroke-width="1.35"/>
      <rect x="8.25" y="14" width="1.5" height="2.1" fill="black"/>
      <rect x="5.9" y="15.7" width="6.2" height="1.1" rx="0.5" fill="black"/>
    </svg>`;
  const url = "data:image/svg+xml;base64," + Buffer.from(svg).toString("base64");
  try {
    const img = nativeImage.createFromDataURL(url);
    img.setTemplateImage(true);
    return img;
  } catch {
    return nativeImage.createEmpty();
  }
}

function positionTrayWindow() {
  if (!trayWindow || !tray) return;
  const tb = tray.getBounds();
  const { width, height } = trayWindow.getBounds();
  const x = Math.round(tb.x + tb.width / 2 - width / 2);
  const y = Math.round(tb.y + tb.height + 6);
  trayWindow.setPosition(x, Math.max(24, y), false);
}

function hideTrayPanel() {
  if (trayWindow && !trayWindow.isDestroyed() && trayWindow.isVisible()) {
    trayWindow.hide();
  }
}

function toggleTrayPanel() {
  if (!trayWindow || trayWindow.isDestroyed()) {
    createTrayWindow();
  }
  if (trayWindow.isVisible()) {
    hideTrayPanel();
    return;
  }
  pushTrayTheme();
  positionTrayWindow();
  trayWindow.show();
  trayWindow.focus();
}

const TRAY_WIDTH = 280;

function nativeThemePayload() {
  return {
    dark: !!nativeTheme.shouldUseDarkColors,
    themeSource: nativeTheme.themeSource,
  };
}

function pushTrayTheme() {
  if (!trayWindow || trayWindow.isDestroyed()) return;
  try {
    trayWindow.webContents.send("native-theme", nativeThemePayload());
  } catch (_) {}
}

function createTrayWindow() {
  if (trayWindow && !trayWindow.isDestroyed()) return;
  trayWindow = new BrowserWindow({
    width: TRAY_WIDTH,
    height: 380,
    resizable: false,
    movable: false,
    minimizable: false,
    maximizable: false,
    fullscreenable: false,
    frame: false,
    show: false,
    skipTaskbar: true,
    alwaysOnTop: true,
    // 系统菜单栏弹层：popover 毛玻璃 + 渲染层按浅/深色自适应文字
    transparent: true,
    backgroundColor: "#00000000",
    hasShadow: true,
    roundedCorners: true,
    vibrancy: "popover",
    visualEffectState: "active",
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  trayWindow.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
  trayWindow.setWindowButtonVisibility(false);
  trayWindow.loadFile(path.join(__dirname, "renderer", "tray.html"));
  trayWindow.webContents.on("did-finish-load", () => pushTrayTheme());
  trayWindow.on("blur", () => {
    clearTimeout(trayBlurTimer);
    trayBlurTimer = setTimeout(() => hideTrayPanel(), 120);
  });
  trayWindow.on("close", (e) => {
    if (!quitting) {
      e.preventDefault();
      hideTrayPanel();
    }
  });
}

nativeTheme.on("updated", () => pushTrayTheme());

async function buildTrayMenu() {
  let syncOn = false;
  let mainOn = false;
  let bgOn = false;
  try {
    const [cfg, st] = await Promise.all([api("/api/config"), api("/api/state")]);
    syncOn = !!(cfg.data && cfg.data.sync && cfg.data.sync.enabled);
    const s = (st.data && st.data.state) || {};
    mainOn = String(s.power || s.main_power || "").toLowerCase() === "on";
    bgOn = String(s.bg_power || "").toLowerCase() === "on";
  } catch (_) {}

  return Menu.buildFromTemplate([
    { label: "打开完整控制", click: () => createWindow() },
    { type: "separator" },
    {
      label: "主灯",
      type: "checkbox",
      checked: mainOn,
      click: async (item) => {
        try {
          await api("/api/power", { method: "POST", body: { target: "main", on: item.checked } });
        } catch (e) {
          console.error(e);
        }
      },
    },
    {
      label: "氛围灯",
      type: "checkbox",
      checked: bgOn,
      click: async (item) => {
        try {
          await api("/api/power", { method: "POST", body: { target: "bg", on: item.checked } });
        } catch (e) {
          console.error(e);
        }
      },
    },
    {
      label: "同步",
      type: "checkbox",
      checked: syncOn,
      click: async (item) => {
        try {
          await api(`/api/sync/enable?enabled=${item.checked}`, { method: "POST" });
        } catch (e) {
          console.error(e);
        }
      },
    },
    { type: "separator" },
    {
      label: "退出",
      click: () => {
        quitting = true;
        app.quit();
      },
    },
  ]);
}

async function createTray() {
  tray = new Tray(trayIcon());
  tray.setToolTip(PRODUCT);
  tray.on("click", () => toggleTrayPanel());
  tray.on("right-click", async () => {
    hideTrayPanel();
    tray.popUpContextMenu(await buildTrayMenu());
  });
}

function applyLaunchAtLogin(enabled) {
  try {
    app.setLoginItemSettings({
      openAtLogin: !!enabled,
      openAsHidden: true,
    });
  } catch (e) {
    console.error("setLoginItemSettings", e);
  }
}

async function syncLoginItemFromConfig() {
  try {
    const cfg = await api("/api/config");
    applyLaunchAtLogin(!!(cfg.data && cfg.data.launch_at_login));
  } catch (_) {}
}

function copyDir(src, dest) {
  fs.mkdirSync(dest, { recursive: true });
  fs.cpSync(src, dest, { recursive: true, force: true });
}

function installIinaPlugin() {
  const src = extraPath("iina-plugin");
  if (!fs.existsSync(src)) return { ok: false, error: "包内找不到 IINA 插件" };
  const dest = path.join(
    app.getPath("home"),
    "Library/Application Support/com.colliderli.iina/plugins/com.yeelightbar.sync.iinaplugin"
  );
  try {
    fs.rmSync(dest, { recursive: true, force: true });
    copyDir(src, dest);
    execFile("defaults", ["write", "com.colliderli.iina", "PluginEnabled.com.yeelightbar.sync", "-bool", "true"], () => {});
    execFile(
      "defaults",
      ["write", "com.colliderli.iina", "PluginOrder", "-array-add", "com.yeelightbar.sync"],
      () => {}
    );
    return { ok: true, dest };
  } catch (e) {
    return { ok: false, error: String(e && e.message ? e.message : e) };
  }
}

function installChromeExtension() {
  const src = extraPath("browser-extension", "chromium");
  if (!fs.existsSync(src)) return { ok: false, error: "包内找不到 Chrome 扩展" };
  const dest = path.join(configDir(), "chrome-extension");
  try {
    fs.rmSync(dest, { recursive: true, force: true });
    copyDir(src, dest);
    shell.showItemInFolder(dest);
    execFile("open", ["-a", "Google Chrome", "chrome://extensions"], () => {});
    return { ok: true, dest };
  } catch (e) {
    return { ok: false, error: String(e && e.message ? e.message : e) };
  }
}

function closeRegionPicker(result) {
  if (regionPickerResolve) {
    regionPickerResolve(result);
    regionPickerResolve = null;
  }
  if (regionPickerWindow && !regionPickerWindow.isDestroyed()) {
    regionPickerWindow.close();
  }
  regionPickerWindow = null;
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.show();
    mainWindow.focus();
  }
}

function virtualScreenBounds() {
  const displays = screen.getAllDisplays();
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  for (const d of displays) {
    const b = d.bounds;
    minX = Math.min(minX, b.x);
    minY = Math.min(minY, b.y);
    maxX = Math.max(maxX, b.x + b.width);
    maxY = Math.max(maxY, b.y + b.height);
  }
  if (!Number.isFinite(minX)) {
    const primary = screen.getPrimaryDisplay().bounds;
    return { x: primary.x, y: primary.y, width: primary.width, height: primary.height };
  }
  return { x: minX, y: minY, width: maxX - minX, height: maxY - minY };
}

function displayIdForPoint(x, y) {
  const d = screen.getDisplayNearestPoint({ x, y });
  return d?.id ?? null;
}

ipcMain.handle("api", async (_evt, pathname, options) => {
  return api(pathname, options || {});
});

ipcMain.handle("open-external", async (_evt, url) => {
  await shell.openExternal(url);
});

ipcMain.handle("reveal-path", async (_evt, targetPath) => {
  if (!targetPath) return { ok: false };
  const result = shell.showItemInFolder(targetPath);
  return { ok: true, result };
});

ipcMain.handle("open-path", async (_evt, targetPath) => {
  if (!targetPath) return { ok: false };
  const err = await shell.openPath(targetPath);
  return { ok: !err, error: err || null };
});

ipcMain.handle("extension-paths", async () => {
  return {
    chromeDir: extraPath("browser-extension", "chromium"),
    chromeUserDir: path.join(configDir(), "chrome-extension"),
    safariProject: extraPath("browser-extension", "safari", "YeelightBarSync", "YeelightBarSync.xcodeproj"),
    readme: extraPath("browser-extension", "README.md"),
    iinaPlugin: extraPath("iina-plugin"),
  };
});

ipcMain.handle("set-launch-at-login", async (_evt, enabled) => {
  applyLaunchAtLogin(!!enabled);
  try {
    await api("/api/config", { method: "PATCH", body: { launch_at_login: !!enabled } });
  } catch (_) {}
  return { ok: true, enabled: !!enabled };
});

ipcMain.handle("install-iina-plugin", async () => installIinaPlugin());
ipcMain.handle("install-chrome-extension", async () => installChromeExtension());

ipcMain.handle("open-screen-recording-settings", async () => {
  // 先触达录屏 API，让本程序（新版本）出现在系统列表里，再打开设置页
  await probeScreenCaptureAccess({ force: true });
  settingsOpenedScreen = true;
  await openScreenRecordingSettings();
  writeMusicPermissions({ settingsOpenedScreen: true, screenProbeOnce: true });
  return { ok: true };
});
ipcMain.handle("ensure-music-permission", async (_evt, input, allowRequest, force) =>
  ensureMusicPermission(input, { allowRequest: !!allowRequest, force: !!force })
);
ipcMain.handle("refresh-music-permissions", async () => writeMusicPermissions());

ipcMain.handle("open-full-window", async () => {
  hideTrayPanel();
  createWindow();
  return { ok: true };
});

ipcMain.handle("hide-tray-panel", async () => {
  hideTrayPanel();
  return { ok: true };
});

ipcMain.handle("resize-tray-panel", async (_evt, height) => {
  if (!trayWindow || trayWindow.isDestroyed()) return { ok: false };
  const h = Math.max(200, Math.min(640, Math.ceil(Number(height) || 0)));
  trayWindow.setContentSize(TRAY_WIDTH, h);
  if (trayWindow.isVisible()) positionTrayWindow();
  return { ok: true, width: TRAY_WIDTH, height: h };
});

ipcMain.handle("app-info", async () => {
  return {
    packaged: isPackaged(),
    configDir: configDir(),
    product: PRODUCT,
    version: app.getVersion(),
  };
});

ipcMain.handle("get-native-theme", async () => nativeThemePayload());

ipcMain.handle("open-region-picker", async () => {
  if (regionPickerWindow && !regionPickerWindow.isDestroyed()) {
    regionPickerWindow.focus();
    return new Promise((resolve) => {
      const prev = regionPickerResolve;
      regionPickerResolve = (result) => {
        if (prev) prev(result);
        resolve(result);
      };
    });
  }

  return new Promise((resolve) => {
    regionPickerResolve = resolve;
    const bounds = virtualScreenBounds();
    regionPickerWindow = new BrowserWindow({
      x: bounds.x,
      y: bounds.y,
      width: bounds.width,
      height: bounds.height,
      frame: false,
      transparent: true,
      resizable: false,
      movable: false,
      fullscreenable: false,
      hasShadow: false,
      alwaysOnTop: true,
      skipTaskbar: true,
      focusable: true,
      show: false,
      backgroundColor: "#00000000",
      webPreferences: {
        preload: path.join(__dirname, "preload.js"),
        contextIsolation: true,
        nodeIntegration: false,
      },
    });
    regionPickerWindow.setAlwaysOnTop(true, "screen-saver");
    regionPickerWindow.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
    regionPickerWindow.loadFile(path.join(__dirname, "renderer", "region-picker.html"));
    regionPickerWindow.once("ready-to-show", () => {
      regionPickerWindow.show();
      regionPickerWindow.focus();
    });
    regionPickerWindow.on("closed", () => {
      regionPickerWindow = null;
      if (regionPickerResolve) {
        regionPickerResolve({ ok: false, cancelled: true });
        regionPickerResolve = null;
      }
    });
  });
});

ipcMain.handle("region-picker-cancel", async () => {
  closeRegionPicker({ ok: false, cancelled: true });
  return { ok: true };
});

ipcMain.handle("region-picker-confirm", async (_evt, rect) => {
  const left = Math.round(Number(rect?.left) || 0);
  const top = Math.round(Number(rect?.top) || 0);
  const width = Math.max(8, Math.round(Number(rect?.width) || 0));
  const height = Math.max(8, Math.round(Number(rect?.height) || 0));
  const display_id =
    rect?.display_id != null ? Number(rect.display_id) : displayIdForPoint(left + width / 2, top + height / 2);
  const user_rect = { display_id, left, top, width, height };
  try {
    await api("/api/config", {
      method: "PATCH",
      body: {
        sync: {
          screen: {
            user_rect,
            capture_target: "user_rect",
          },
        },
      },
    });
    closeRegionPicker({ ok: true, user_rect });
    return { ok: true, user_rect };
  } catch (e) {
    closeRegionPicker({ ok: false, error: String(e && e.message ? e.message : e) });
    return { ok: false, error: String(e && e.message ? e.message : e) };
  }
});

async function ensureApi() {
  let ready = false;
  try {
    const r = await api("/api/health");
    const rev = Number(r.data && r.data.api_rev) || 0;
    if (r.data && r.data.ok && rev >= NEED_REV) {
      ready = true;
    } else if (r.data && r.data.ok) {
      console.warn(`检测到旧 API (rev=${rev})，尝试重启本地服务`);
      try {
        const { execSync } = require("child_process");
        execSync("lsof -tiTCP:8787 -sTCP:LISTEN | xargs kill -9", { stdio: "ignore" });
      } catch (_) {}
      await new Promise((r) => setTimeout(r, 400));
      startPython();
      ready = await waitForApi();
    }
  } catch (_) {
    startPython();
    ready = await waitForApi();
  }
  if (!ready) console.error("API 未能启动，界面仍可打开但控制不可用");
  return ready;
}

const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on("second-instance", () => createWindow());
  app.whenReady().then(async () => {
    applyAccessoryPolicy();
    await ensureApi();
    await createTray();
    createTrayWindow();
    await syncLoginItemFromConfig();
    const openedAtLogin = !!(app.getLoginItemSettings && app.getLoginItemSettings().wasOpenedAtLogin);
    let showMain = !openedAtLogin;
    try {
      const cfg = await api("/api/config");
      if (openedAtLogin && cfg.data && cfg.data.onboarding && cfg.data.onboarding.complete) {
        showMain = false;
      } else if (cfg.data && cfg.data.onboarding && cfg.data.onboarding.complete === false) {
        showMain = true;
      }
      // 每个新版本首次启动检查/申请一次权限；同版本不再自动弹。
      await runVersionFirstLaunchPermissionCheck(cfg.data || {});
    } catch (_) {}
    if (showMain) createWindow();
  });
}

app.on("before-quit", () => {
  quitting = true;
  stopPython();
});

app.on("window-all-closed", (e) => {
  e.preventDefault?.();
});
