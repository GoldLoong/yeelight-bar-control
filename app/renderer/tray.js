function $(id) {
  return document.getElementById(id);
}

function applyNativeTheme(payload) {
  const dark = !!(payload && payload.dark);
  const theme = dark ? "dark" : "light";
  document.documentElement.dataset.theme = theme;
  document.documentElement.style.colorScheme = theme;
  const meta = document.querySelector('meta[name="color-scheme"]');
  if (meta) meta.setAttribute("content", theme);
}

async function api(pathname, options = {}) {
  return window.yeelight.api(pathname, options);
}

function setToggle(el, on) {
  if (!el) return;
  el.setAttribute("aria-pressed", on ? "true" : "false");
}

function debounce(fn, ms) {
  let t = null;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
}

const SOURCE_LABEL = { iina: "IINA", browser: "浏览器", screen: "屏幕" };
// 略长于服务端电源保护窗，避免锁过期后被旧快照弹回
const POWER_LOCK_MS = 2800;

let lastModeId = null;
let modesCache = {};
let driveMode = "picture";
let mainPowerLockUntil = 0;
let bgPowerLockUntil = 0;
let syncLockUntil = 0;
let mainPowerBusy = false;
let bgPowerBusy = false;
let syncBusy = false;
/** @type {{ on: boolean, until: number } | null} */
let mainIntent = null;
/** @type {{ on: boolean, until: number } | null} */
let bgIntent = null;

function parsePowerOn(v) {
  if (v == null || v === "") return null;
  const s = String(v).toLowerCase();
  if (s === "on") return true;
  if (s === "off") return false;
  return null;
}

function agentTrayLog(message, data) {
}

function hexNorm(h) {
  return String(h || "")
    .trim()
    .toUpperCase()
    .replace(/^#/, "");
}

function setMainPowerUi(on, { lock = false, intent = false } = {}) {
  setToggle($("trayMainPower"), on);
  if (lock) {
    mainPowerLockUntil = performance.now() + POWER_LOCK_MS;
    bgPowerLockUntil = Math.max(bgPowerLockUntil, performance.now() + POWER_LOCK_MS);
  }
  if (intent) mainIntent = { on: !!on, until: performance.now() + POWER_LOCK_MS };
}

function setBgPowerUi(on, { lock = false, intent = false } = {}) {
  setToggle($("trayBgPower"), on);
  if (lock) {
    bgPowerLockUntil = performance.now() + POWER_LOCK_MS;
    mainPowerLockUntil = Math.max(mainPowerLockUntil, performance.now() + POWER_LOCK_MS);
  }
  if (intent) bgIntent = { on: !!on, until: performance.now() + POWER_LOCK_MS };
}

function setSyncUi(on, { lock = false } = {}) {
  setToggle($("traySync"), on);
  if (lock) syncLockUntil = performance.now() + POWER_LOCK_MS;
}

function applyChannelPower(kind, got) {
  if (got === null) return;
  const now = performance.now();
  const intent = kind === "main" ? mainIntent : bgIntent;
  const lockUntil = kind === "main" ? mainPowerLockUntil : bgPowerLockUntil;
  const busy = kind === "main" ? mainPowerBusy : bgPowerBusy;
  const el = kind === "main" ? $("trayMainPower") : $("trayBgPower");
  if (intent && now < intent.until) {
    if (got === intent.on) {
      if (kind === "main") mainIntent = null;
      else bgIntent = null;
      setToggle(el, got);
      agentTrayLog("intent confirmed", { kind, got });
      return;
    }
    agentTrayLog("block snapback", { kind, got, want: intent.on });
    return;
  }
  if (now > lockUntil && !busy) {
    setToggle(el, got);
  } else {
    agentTrayLog("skip apply (locked)", {
      kind,
      got,
      lockLeft: Math.round(lockUntil - now),
      busy,
    });
  }
}

function applyLampStateToUi(s) {
  if (!s || typeof s !== "object") return;
  // 与主窗口一致：优先 main_power，再回退 power
  const mainOn = parsePowerOn(s.main_power) ?? parsePowerOn(s.power);
  const bgOn = parsePowerOn(s.bg_power);
  applyChannelPower("main", mainOn);
  applyChannelPower("bg", bgOn);
}

function matchMode(state, cfg) {
  if (lastModeId && modesCache[lastModeId]) return lastModeId;
  const bright = Number(state?.bright);
  const ct = Number(state?.ct);
  const bgBright = Number(state?.bg_bright);
  const bgHex = hexNorm(state?.bg_rgb_hex || cfg?.ambient?.hex);
  const mainOn =
    (parsePowerOn(state?.main_power) ?? parsePowerOn(state?.power)) === true;
  const bgOn = parsePowerOn(state?.bg_power) === true;
  let best = null;
  let bestScore = -1;
  for (const [id, m] of Object.entries(modesCache)) {
    let score = 0;
    if (!!m.main_on === mainOn) score += 2;
    if (!!m.bg_on === bgOn) score += 2;
    if (Number(m.bright) === bright) score += 1;
    if (Math.abs(Number(m.ct) - ct) <= 50) score += 1;
    if (Number(m.bg_bright) === bgBright) score += 1;
    if (hexNorm(m.bg_hex) === bgHex) score += 3;
    if (score > bestScore) {
      bestScore = score;
      best = id;
    }
  }
  return bestScore >= 5 ? best : null;
}

function setActiveMode(modeId) {
  document.querySelectorAll(".tray-mode").forEach((b) => {
    b.classList.toggle("active", !!modeId && b.dataset.mode === modeId);
  });
}

function syncHintText(cfg, sync) {
  if (!cfg?.sync?.enabled) return "已关闭";
  if (sync?.active) {
    const src = SOURCE_LABEL[sync.source] || sync.source || "";
    return src ? `正在同步 · ${src}` : "正在同步";
  }
  return "待命";
}

function fitWindow() {
  const root = $("trayRoot");
  if (!root || !window.yeelight?.resizeTrayPanel) return;
  const height = Math.ceil(root.getBoundingClientRect().height);
  window.yeelight.resizeTrayPanel(height);
}

async function refresh() {
  try {
    const [{ data: cfg }, { data: st }, { data: sync }, modesRes] = await Promise.all([
      api("/api/config"),
      api("/api/state"),
      api("/api/sync/status"),
      api("/api/modes"),
    ]);
    modesCache = modesRes?.data?.modes || {};
    const s = (st && st.state) || {};
    applyLampStateToUi(s);
    if (performance.now() > syncLockUntil && !syncBusy) {
      setSyncUi(!!(cfg && cfg.sync && cfg.sync.enabled));
    }
    const bright = Number(s.bright ?? cfg?.sync?.iina?.base_main_bright ?? 50);
    const bgBright =
      cfg?.drive_mode === "music"
        ? Number(cfg?.music?.bright ?? 40)
        : Number(s.bg_bright ?? cfg?.ambient?.bright ?? 40);
    const ct = Number(s.ct ?? 4000);
    $("trayMainBright").value = String(bright);
    $("trayMainBrightVal").textContent = `${bright}%`;
    $("trayBgBright").value = String(bgBright);
    $("trayBgBrightVal").textContent = `${bgBright}%`;
    $("trayCt").value = String(ct);
    $("trayCtVal").textContent = `${ct}K`;
    const hex = (s.bg_rgb_hex || cfg?.ambient?.hex || "#2A3A55").toUpperCase();
    document.querySelectorAll("#traySwatches button").forEach((b) => {
      b.classList.toggle("active", (b.dataset.hex || "").toUpperCase() === hex);
    });
    $("traySyncHint").textContent = syncHintText(cfg, sync);
    setTrayDrive(cfg?.drive_mode);
    $("trayStatus").textContent = st?.connected === false ? "未连接" : "就绪";
    setActiveMode(matchMode(s, cfg));
  } catch (_) {
    $("trayStatus").textContent = "未就绪";
  }
  requestAnimationFrame(fitWindow);
}

function setTrayDrive(mode) {
  driveMode = mode === "music" ? "music" : "picture";
  document.querySelectorAll("#trayDrive button").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.drive === driveMode);
  });
  const root = $("trayRoot");
  if (root) root.classList.toggle("drive-music", driveMode === "music");
  if ($("trayBgPower")) $("trayBgPower").disabled = false;
}

$("trayMainPower").addEventListener("click", async () => {
  if (mainPowerBusy) return;
  const on = $("trayMainPower").getAttribute("aria-pressed") !== "true";
  mainPowerBusy = true;
  setMainPowerUi(on, { lock: true, intent: true });
  lastModeId = null;
  agentTrayLog("main power click", { on });
  try {
    const { status, data } = await api("/api/power", {
      method: "POST",
      body: { target: "main", on },
    });
    if (status >= 400) {
      mainIntent = null;
      setMainPowerUi(!on, { lock: true });
      $("trayStatus").textContent = "开关失败";
      agentTrayLog("main power fail", { status, on });
      return;
    }
    if (data?.state) {
      setMainPowerUi(on, { lock: true, intent: true });
      applyLampStateToUi(data.state);
    }
  } catch (_) {
    mainIntent = null;
    setMainPowerUi(!on, { lock: true });
    $("trayStatus").textContent = "开关失败";
    agentTrayLog("main power error", { on });
  } finally {
    mainPowerBusy = false;
  }
});

$("trayBgPower").addEventListener("click", async () => {
  if (bgPowerBusy) return;
  const on = $("trayBgPower").getAttribute("aria-pressed") !== "true";
  bgPowerBusy = true;
  setBgPowerUi(on, { lock: true, intent: true });
  lastModeId = null;
  agentTrayLog("bg power click", { on });
  try {
    const { status, data } = await api("/api/power", {
      method: "POST",
      body: { target: "bg", on },
    });
    if (status >= 400) {
      bgIntent = null;
      setBgPowerUi(!on, { lock: true });
      $("trayStatus").textContent = "开关失败";
      agentTrayLog("bg power fail", { status, on });
      return;
    }
    if (data?.state) {
      setBgPowerUi(on, { lock: true, intent: true });
      applyLampStateToUi(data.state);
    }
  } catch (_) {
    bgIntent = null;
    setBgPowerUi(!on, { lock: true });
    $("trayStatus").textContent = "开关失败";
    agentTrayLog("bg power error", { on });
  } finally {
    bgPowerBusy = false;
  }
});

$("traySync").addEventListener("click", async () => {
  if (syncBusy) return;
  const on = $("traySync").getAttribute("aria-pressed") !== "true";
  syncBusy = true;
  setSyncUi(on, { lock: true });
  $("traySyncHint").textContent = on ? "待命" : "已关闭";
  agentTrayLog("sync click", { on });
  try {
    const { status, data } = await api(`/api/sync/enable?enabled=${on}`, {
      method: "POST",
    });
    if (status >= 400) {
      setSyncUi(!on, { lock: true });
      $("traySyncHint").textContent = !on ? "待命" : "已关闭";
      $("trayStatus").textContent = "同步开关失败";
      agentTrayLog("sync fail", { status, on });
      return;
    }
    const enabled = data?.sync?.enabled;
    if (typeof enabled === "boolean") {
      setSyncUi(enabled, { lock: true });
      $("traySyncHint").textContent = enabled ? "待命" : "已关闭";
    }
  } catch (_) {
    setSyncUi(!on, { lock: true });
    $("traySyncHint").textContent = !on ? "待命" : "已关闭";
    $("trayStatus").textContent = "同步开关失败";
    agentTrayLog("sync error", { on });
  } finally {
    syncBusy = false;
  }
});

$("trayMainBright").addEventListener(
  "input",
  debounce((e) => {
    const bright = Number(e.target.value);
    $("trayMainBrightVal").textContent = `${bright}%`;
    lastModeId = null;
    // 调主灯亮度会开灯；锁住开关避免 refresh 把中间态盖掉
    setMainPowerUi(true, { lock: true, intent: true });
    api("/api/bright", { method: "POST", body: { target: "main", bright } }).catch(() => {});
  }, 80)
);

$("trayBgBright").addEventListener(
  "input",
  debounce((e) => {
    const bright = Number(e.target.value);
    $("trayBgBrightVal").textContent = `${bright}%`;
    lastModeId = null;
    // 氛围已关时不把开关强行拨成开（音乐跃动 mute 场景）
    if ($("trayBgPower")?.getAttribute("aria-pressed") === "true") {
      setBgPowerUi(true, { lock: true, intent: true });
    }
    api("/api/bright", { method: "POST", body: { target: "bg", bright } }).catch(() => {});
  }, 80)
);

$("trayCt").addEventListener(
  "input",
  debounce((e) => {
    const ct = Number(e.target.value);
    $("trayCtVal").textContent = `${ct}K`;
    lastModeId = null;
    setMainPowerUi(true, { lock: true, intent: true });
    api("/api/ct", { method: "POST", body: { ct } }).catch(() => {});
  }, 80)
);

document.querySelectorAll("#trayDrive button").forEach((btn) => {
  btn.addEventListener("click", async () => {
    if (btn.dataset.drive === "music") {
      await window.yeelight?.ensureMusicPermission?.("system", false);
    }
    await api("/api/drive", { method: "POST", body: { mode: btn.dataset.drive } });
    refresh();
  });
});

document.querySelectorAll(".tray-mode").forEach((btn) => {
  btn.addEventListener("click", () => {
    lastModeId = btn.dataset.mode;
    setActiveMode(lastModeId);
    api(`/api/modes/${btn.dataset.mode}/apply`, { method: "POST" }).then(() => refresh());
  });
});

document.querySelectorAll("#traySwatches button").forEach((btn) => {
  btn.addEventListener("click", () => {
    if (driveMode === "music") return;
    lastModeId = null;
    setBgPowerUi(true, { lock: true, intent: true });
    api("/api/bg/rgb", { method: "POST", body: { hex: btn.dataset.hex } }).then(() => refresh());
  });
});

$("trayOpenFull").addEventListener("click", () => {
  window.yeelight.openFullWindow();
});

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") refresh();
});

window.addEventListener("resize", () => requestAnimationFrame(fitWindow));

const debouncedRefresh = debounce(() => refresh(), 450);

if (window.yeelight?.onStateChanged) {
  window.yeelight.onStateChanged((payload) => {
    if (payload?.state) {
      applyLampStateToUi(payload.state);
      // 有明确 state 时先对齐开关，稍后再刷滑条等，避免立刻 GET 撞上旧快照
      debouncedRefresh();
      return;
    }
    refresh();
  });
}

if (window.yeelight?.onNativeTheme) {
  window.yeelight.onNativeTheme((payload) => applyNativeTheme(payload));
}
if (window.yeelight?.getNativeTheme) {
  window.yeelight.getNativeTheme().then(applyNativeTheme).catch(() => {});
} else if (window.matchMedia) {
  const mq = window.matchMedia("(prefers-color-scheme: dark)");
  applyNativeTheme({ dark: mq.matches });
  mq.addEventListener?.("change", (e) => applyNativeTheme({ dark: e.matches }));
}

refresh();
setInterval(() => {
  if (document.visibilityState === "visible") refresh();
}, 4000);
