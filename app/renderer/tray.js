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

let lastModeId = null;
let modesCache = {};
let driveMode = "picture";

function hexNorm(h) {
  return String(h || "")
    .trim()
    .toUpperCase()
    .replace(/^#/, "");
}

function matchMode(state, cfg) {
  if (lastModeId && modesCache[lastModeId]) return lastModeId;
  const bright = Number(state?.bright);
  const ct = Number(state?.ct);
  const bgBright = Number(state?.bg_bright);
  const bgHex = hexNorm(state?.bg_rgb_hex || cfg?.ambient?.hex);
  const mainOn = String(state?.power || state?.main_power || "").toLowerCase() === "on";
  const bgOn = String(state?.bg_power || "").toLowerCase() === "on";
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
    const mainOn = String(s.power || s.main_power || "").toLowerCase() === "on";
    const bgOn = String(s.bg_power || "").toLowerCase() === "on";
    setToggle($("trayMainPower"), mainOn);
    setToggle($("trayBgPower"), bgOn);
    setToggle($("traySync"), !!(cfg && cfg.sync && cfg.sync.enabled));
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

$("trayMainPower").addEventListener("click", () => {
  const on = $("trayMainPower").getAttribute("aria-pressed") !== "true";
  setToggle($("trayMainPower"), on);
  lastModeId = null;
  api("/api/power", { method: "POST", body: { target: "main", on } }).catch(() => {});
});

function setTrayDrive(mode) {
  driveMode = mode === "music" ? "music" : "picture";
  document.querySelectorAll("#trayDrive button").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.drive === driveMode);
  });
  const root = $("trayRoot");
  if (root) root.classList.toggle("drive-music", driveMode === "music");
  if ($("trayBgPower")) $("trayBgPower").disabled = driveMode === "music";
}

$("trayBgPower").addEventListener("click", () => {
  if (driveMode === "music") return;
  const on = $("trayBgPower").getAttribute("aria-pressed") !== "true";
  setToggle($("trayBgPower"), on);
  lastModeId = null;
  api("/api/power", { method: "POST", body: { target: "bg", on } }).catch(() => {});
});

$("traySync").addEventListener("click", () => {
  const on = $("traySync").getAttribute("aria-pressed") !== "true";
  setToggle($("traySync"), on);
  $("traySyncHint").textContent = on ? "待命" : "已关闭";
  api(`/api/sync/enable?enabled=${on}`, { method: "POST" }).catch(() => {});
});

$("trayMainBright").addEventListener(
  "input",
  debounce((e) => {
    const bright = Number(e.target.value);
    $("trayMainBrightVal").textContent = `${bright}%`;
    lastModeId = null;
    api("/api/bright", { method: "POST", body: { target: "main", bright } }).catch(() => {});
  }, 80)
);

$("trayBgBright").addEventListener(
  "input",
  debounce((e) => {
    const bright = Number(e.target.value);
    $("trayBgBrightVal").textContent = `${bright}%`;
    lastModeId = null;
    api("/api/bright", { method: "POST", body: { target: "bg", bright } }).catch(() => {});
  }, 80)
);

$("trayCt").addEventListener(
  "input",
  debounce((e) => {
    const ct = Number(e.target.value);
    $("trayCtVal").textContent = `${ct}K`;
    lastModeId = null;
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

if (window.yeelight?.onStateChanged) {
  window.yeelight.onStateChanged(() => {
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
