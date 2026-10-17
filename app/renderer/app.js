const $ = (id) => document.getElementById(id);

async function api(pathname, options) {
  if (window.yeelight && window.yeelight.api) {
    return window.yeelight.api(pathname, options);
  }
  const res = await fetch(`http://127.0.0.1:8787${pathname}`, {
    method: options?.method || "GET",
    headers: { "Content-Type": "application/json" },
    body: options?.body ? JSON.stringify(options.body) : undefined,
  });
  const data = await res.json().catch(() => null);
  return { status: res.status, data };
}

function setToggle(el, on) {
  if (!el) return;
  el.setAttribute("aria-pressed", on ? "true" : "false");
  const orb = el.querySelector?.(".power-orb");
  if (orb) orb.textContent = on ? "ON" : "OFF";
}

/** 底栏电源与对应控制区联动；短时锁定，避免 refreshState 把用户刚点的状态盖掉 */
let mainPowerLockUntil = 0;
let bgPowerLockUntil = 0;

function parsePowerOn(v) {
  if (v == null || v === "") return null;
  const s = String(v).toLowerCase();
  if (s === "on") return true;
  if (s === "off") return false;
  return null;
}

function setMainPowerUi(on, { lock = false } = {}) {
  setToggle($("mainPower"), on);
  $("panel-main")?.classList.toggle("is-off", !on);
  if (lock) mainPowerLockUntil = performance.now() + 800;
}

function setBgPowerUi(on, { lock = false } = {}) {
  setToggle($("bgPower"), on);
  $("panel-ambient")?.classList.toggle("is-off", !on);
  if (lock) bgPowerLockUntil = performance.now() + 800;
}

function debounce(fn, ms) {
  let t;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
}

function hexToRgb(hex) {
  const h = hex.replace("#", "");
  return {
    r: parseInt(h.slice(0, 2), 16),
    g: parseInt(h.slice(2, 4), 16),
    b: parseInt(h.slice(4, 6), 16),
  };
}

function rgbToHex(r, g, b) {
  return (
    "#" +
    [r, g, b]
      .map((v) => Math.max(0, Math.min(255, v | 0)).toString(16).padStart(2, "0"))
      .join("")
      .toUpperCase()
  );
}

/* —— Tabs —— */
document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((t) => {
      t.classList.toggle("active", t === tab);
      t.setAttribute("aria-selected", t === tab ? "true" : "false");
    });
    const id = tab.dataset.tab;
    document.querySelectorAll(".panel").forEach((p) => {
      const on = p.id === `panel-${id}`;
      p.hidden = !on;
      p.classList.toggle("active", on);
    });
    if (id === "main") drawCtWheel();
    if (id === "ambient") drawRgbWheel();
    // 切页时强制对齐灯具真实开关/亮度，避免按钮与实态不一致
    refreshState({ force: true }).catch(() => {});
    if (id === "sync") refreshSync().catch(() => {});
  });
});

$("btnMenu").addEventListener("click", () => {
  document.querySelector('.tab[data-tab="sync"]').click();
  $("deviceCard")?.scrollIntoView({ behavior: "smooth", block: "start" });
});

/* —— CT / RGB wheels —— */
let mainCt = 4000;
const ctCanvas = $("ctWheel");
const ctCursor = $("ctCursor");
const ctCenter = $("ctCenter");
const rgbCanvas = $("rgbWheel");
const rgbKnob = $("rgbKnob");
let bgHex = "#2A3A55";
let ctDragging = false;
let mainBrightDragging = false;

function placeKnob(knob, ang, radiusPx, centerPx = 140) {
  knob.style.left = `${centerPx + Math.cos(ang) * radiusPx}px`;
  knob.style.top = `${centerPx + Math.sin(ang) * radiusPx}px`;
}

/** 色温近似显示色（暖橙 → 中性白 → 冷蓝） */
function ctToRgb(ct) {
  const u = Math.max(0, Math.min(1, (Number(ct) - 1700) / 4800));
  if (u < 0.5) {
    const t = u / 0.5;
    return {
      r: Math.round(232 + (241 - 232) * t),
      g: Math.round(136 + (238 - 136) * t),
      b: Math.round(48 + (232 - 48) * t),
    };
  }
  const t = (u - 0.5) / 0.5;
  return {
    r: Math.round(241 + (126 - 241) * t),
    g: Math.round(238 + (173 - 238) * t),
    b: Math.round(232 + (224 - 232) * t),
  };
}

function ctUnit(ct) {
  return Math.max(0, Math.min(1, (Number(ct) - 1700) / 4800));
}

function drawCtWheel() {
  const u = ctUnit(mainCt);
  // 光标在色盘内横向跟随（面宽 248 = 280-32）
  if (ctCursor) {
    ctCursor.style.left = `${u * 248}px`;
  }
  $("mainCtVal").textContent = `${mainCt}K`;
  if (ctCenter) {
    const { r, g, b } = ctToRgb(mainCt);
    ctCenter.style.background = `rgba(${Math.round(r * 0.22)}, ${Math.round(g * 0.22)}, ${Math.round(b * 0.22)}, 0.55)`;
    ctCenter.style.boxShadow = `0 0 28px rgba(${r}, ${g}, ${b}, 0.35), inset 0 1px 0 rgba(255,255,255,0.1)`;
  }
}

function ctFromPointer(ev) {
  const rect = ctCanvas.getBoundingClientRect();
  if (rect.width < 8 || rect.height < 8) return;
  const scale = rect.width / 280;
  const inset = 16 * scale;
  const x = ev.clientX - rect.left;
  let u = (x - inset) / (rect.width - inset * 2);
  u = Math.max(0, Math.min(1, u));
  const next = Math.round((1700 + u * 4800) / 50) * 50;
  const clamped = Math.max(1700, Math.min(6500, next));
  if (clamped === mainCt) return;
  mainCt = clamped;
  drawCtWheel();
  pushCt();
}

/** 最新值合并：前一条还在飞时只记最新色温，避免 TCP/请求堆积后灯不跟随 */
let _ctInflight = false;
let _ctWant = null;

function flushCt(forceCt) {
  const ct = forceCt != null ? forceCt : mainCt;
  setMainPowerUi(true, { lock: true });
  if (_ctInflight) {
    _ctWant = ct;
    return;
  }
  _ctInflight = true;
  _ctWant = null;
  api("/api/ct", { method: "POST", body: { ct } })
    .catch(() => {})
    .finally(() => {
      _ctInflight = false;
      if (_ctWant != null && _ctWant !== ct) {
        const next = _ctWant;
        _ctWant = null;
        flushCt(next);
      }
    });
}

const pushCt = debounce(() => flushCt(), 50);

ctCanvas.addEventListener("pointerdown", (e) => {
  ctDragging = true;
  ctCanvas.setPointerCapture(e.pointerId);
  ctFromPointer(e);
});
ctCanvas.addEventListener("pointermove", (e) => {
  if (ctDragging) ctFromPointer(e);
});
const endCtDrag = () => {
  if (!ctDragging) return;
  ctDragging = false;
  flushCt(mainCt);
};
ctCanvas.addEventListener("pointerup", endCtDrag);
ctCanvas.addEventListener("pointercancel", endCtDrag);
ctCanvas.addEventListener("lostpointercapture", endCtDrag);

function hsvToRgb(h, s, v) {
  const c = v * s;
  const x = c * (1 - Math.abs(((h / 60) % 2) - 1));
  const m = v - c;
  let rp = 0,
    gp = 0,
    bp = 0;
  if (h < 60) [rp, gp, bp] = [c, x, 0];
  else if (h < 120) [rp, gp, bp] = [x, c, 0];
  else if (h < 180) [rp, gp, bp] = [0, c, x];
  else if (h < 240) [rp, gp, bp] = [0, x, c];
  else if (h < 300) [rp, gp, bp] = [x, 0, c];
  else [rp, gp, bp] = [c, 0, x];
  return {
    r: Math.round((rp + m) * 255),
    g: Math.round((gp + m) * 255),
    b: Math.round((bp + m) * 255),
  };
}

function rgbToHsv(r, g, b) {
  r /= 255;
  g /= 255;
  b /= 255;
  const max = Math.max(r, g, b);
  const min = Math.min(r, g, b);
  const d = max - min;
  let h = 0;
  if (d) {
    if (max === r) h = ((g - b) / d) % 6;
    else if (max === g) h = (b - r) / d + 2;
    else h = (r - g) / d + 4;
    h *= 60;
    if (h < 0) h += 360;
  }
  const s = max ? d / max : 0;
  return { h, s, v: max };
}

function drawRgbWheel() {
  const { r, g, b } = hexToRgb(bgHex);
  const { h, s } = rgbToHsv(r, g, b);
  const ang = ((h - 90) * Math.PI) / 180;
  const compact = $("rgbWrap")?.classList.contains("compact");
  const center = compact ? 120 : 140;
  const radius = (compact ? 76 : 88) + Math.max(0.2, s) * (compact ? 22 : 26);
  placeKnob(rgbKnob, ang, radius, center);
  $("bgColorHex").textContent = bgHex;
  $("bgSwatch").style.background = `radial-gradient(circle at 40% 35%, ${bgHex}, #141416 72%)`;
  if ($("swatch")) $("swatch").style.background = bgHex;
}

function rgbFromPointer(ev) {
  const rect = rgbCanvas.getBoundingClientRect();
  const x = ev.clientX - rect.left - rect.width / 2;
  const y = ev.clientY - rect.top - rect.height / 2;
  const dist = Math.sqrt(x * x + y * y);
  if (dist < 70) return;
  let hue = (Math.atan2(y, x) * 180) / Math.PI + 90;
  if (hue < 0) hue += 360;
  const sat = Math.max(0.55, Math.min(1, (dist - 70) / 70));
  const { r, g, b } = hsvToRgb(hue, sat, 1);
  bgHex = rgbToHex(r, g, b);
  drawRgbWheel();
  pushRgb();
}

const pushRgb = debounce(() => {
  setBgPowerUi(true, { lock: true });
  api("/api/bg/rgb", { method: "POST", body: { hex: bgHex } }).catch(() => {});
}, 80);

let rgbDragging = false;
rgbCanvas.addEventListener("pointerdown", (e) => {
  rgbDragging = true;
  rgbCanvas.setPointerCapture(e.pointerId);
  rgbFromPointer(e);
});
rgbCanvas.addEventListener("pointermove", (e) => {
  if (rgbDragging) rgbFromPointer(e);
});
rgbCanvas.addEventListener("pointerup", () => {
  rgbDragging = false;
});

/* —— Modes —— */
const MODE_FX_LABEL = { static: "静态", breath: "呼吸", cycle: "循环", flow: "燃彩" };
let modesCache = {};
let editingModeId = null;
let draftMode = null;
let ambientFx = {
  fx: "static",
  bright: 40,
  speed_ms: 3800,
  hex: "#2A3A55",
  colors: ["#2A5BFF", "#AF52DE", "#34C759", "#FF9500"],
  direction: "out",
};
let cycleEditIndex = 0;

async function loadModes() {
  const { data } = await api("/api/modes");
  modesCache = data?.modes || {};
}

let _modeApplySeq = 0;
let _modeApplyTimer = null;
function applyMode(modeId) {
  const m = modesCache[modeId];
  // 先更新 UI，连点只下发最后一次
  document.querySelectorAll(".mode-card").forEach((c) => {
    c.classList.toggle("selected", c.dataset.mode === modeId);
  });
  if (m) {
    // 亮度/色温可先反映模式；底栏电源等 apply 后再按灯具实态对齐
    $("mainBright").value = m.bright;
    $("mainBrightVal").textContent = m.bright;
    $("bgBright").value = m.bg_bright;
    $("bgBrightVal").textContent = m.bg_bright;
    mainCt = m.ct;
    bgHex = m.bg_hex;
    ambientFx = {
      fx: m.bg_fx || "static",
      bright: m.bg_bright,
      speed_ms: m.bg_speed_ms || 3800,
      hex: m.bg_hex,
      colors: m.bg_colors || ambientFx.colors,
      direction: m.bg_direction || "out",
    };
    drawCtWheel();
    drawRgbWheel();
    setAmbientFxUi(ambientFx.fx);
  }
  clearTimeout(_modeApplyTimer);
  _modeApplyTimer = setTimeout(async () => {
    const seq = ++_modeApplySeq;
    const { status, data } = await api(`/api/modes/${modeId}/apply`, { method: "POST" });
    if (seq !== _modeApplySeq) return;
    if (status >= 400) {
      $("connText").textContent = "模式切换失败（可能配额冷却）";
      return;
    }
    // 用 apply 回写状态，避免立刻 force 快照偶发空值把底栏电源刷乱
    if (data?.state) applyLampStateToUi(data.state, { force: true });
    else await refreshState({ force: true }).catch(() => {});
  }, 40);
}

document.querySelectorAll(".mode-card").forEach((card) => {
  const run = () => applyMode(card.dataset.mode);
  card.addEventListener("click", (e) => {
    if (e.target.closest(".mode-gear-btn")) return;
    run();
  });
  card.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      run();
    }
  });
});

document.querySelectorAll(".mode-gear-btn").forEach((btn) => {
  btn.addEventListener("click", async (e) => {
    e.stopPropagation();
    await loadModes();
    openModeSheet(btn.dataset.mode);
  });
});

function openModeSheet(modeId) {
  editingModeId = modeId;
  draftMode = { ...(modesCache[modeId] || {}) };
  renderModeSheet();
  $("modeSheet").hidden = false;
}

function closeModeSheet() {
  $("modeSheet").hidden = true;
  editingModeId = null;
  draftMode = null;
}

function renderModeSheet() {
  if (!draftMode) return;
  $("modeNameLabel").textContent = draftMode.name || editingModeId;
  setToggle($("modeMainToggle"), !!draftMode.main_on);
  setToggle($("modeBgToggle"), !!draftMode.bg_on);
  $("modeMainTags").innerHTML = `
    <span class="tag">亮度${draftMode.bright}%</span>
    <span class="tag">色温${draftMode.ct}K</span>`;
  const fx = MODE_FX_LABEL[draftMode.bg_fx] || draftMode.bg_fx;
  $("modeBgTags").innerHTML = `
    <span class="tag">${fx}</span>
    <span class="mini-swatch" id="modeBgSwatch" style="background:${draftMode.bg_hex}"></span>
    <span class="tag">亮度${draftMode.bg_bright}%</span>
    ${draftMode.bg_fx !== "static" ? `<span class="tag">速率${draftMode.bg_speed_ms}ms</span>` : ""}`;
}

$("modeSheetBack")?.addEventListener("click", closeModeSheet);
$("modeMainToggle")?.addEventListener("click", () => {
  if (!draftMode) return;
  draftMode.main_on = $("modeMainToggle").getAttribute("aria-pressed") !== "true";
  setToggle($("modeMainToggle"), draftMode.main_on);
});
$("modeBgToggle")?.addEventListener("click", () => {
  if (!draftMode) return;
  draftMode.bg_on = $("modeBgToggle").getAttribute("aria-pressed") !== "true";
  setToggle($("modeBgToggle"), draftMode.bg_on);
});
$("modeEditMain")?.addEventListener("click", () => {
  closeModeSheet();
  document.querySelector('.tab[data-tab="main"]').click();
});
$("modeEditBg")?.addEventListener("click", () => {
  closeModeSheet();
  document.querySelector('.tab[data-tab="ambient"]').click();
});
$("modeSave")?.addEventListener("click", async () => {
  if (!editingModeId || !draftMode) return;
  // 把当前氛围灯页状态并入草稿
  draftMode.bg_fx = ambientFx.fx;
  draftMode.bg_bright = Number($("bgBright").value);
  draftMode.bg_speed_ms = Number($("bgSpeed")?.value || ambientFx.speed_ms);
  draftMode.bg_hex = bgHex;
  draftMode.bg_colors = ambientFx.colors;
  draftMode.bg_direction = ambientFx.direction;
  draftMode.bright = Number($("mainBright").value);
  draftMode.ct = mainCt;
  await api(`/api/modes/${editingModeId}`, { method: "PATCH", body: draftMode });
  await loadModes();
  await applyMode(editingModeId);
  closeModeSheet();
});
$("modeReset")?.addEventListener("click", async () => {
  if (!editingModeId) return;
  const { data } = await api(`/api/modes/${editingModeId}/reset`, { method: "POST" });
  if (data?.mode) {
    modesCache[editingModeId] = data.mode;
    draftMode = { ...data.mode };
    renderModeSheet();
  }
});

function setAmbientFxUi(fx) {
  ambientFx.fx = fx;
  document.querySelectorAll(".fx-mode").forEach((b) => {
    b.classList.toggle("active", b.dataset.fx === fx);
  });
  $("fxPanelStatic").hidden = fx !== "static";
  $("fxPanelBreath").hidden = fx !== "breath";
  $("fxPanelCycle").hidden = fx !== "cycle";
  $("fxPanelFlow").hidden = fx !== "flow";
  $("speedBlock").hidden = fx === "static";
  $("bgSpeedVal").textContent = String(ambientFx.speed_ms);
  $("bgSpeed").value = String(ambientFx.speed_ms);
  document.querySelectorAll("#flowDir .dir-btn").forEach((b) => {
    b.classList.toggle("active", b.dataset.dir === ambientFx.direction);
  });
  renderCycleSeq();
}

function renderCycleSeq() {
  const dots = document.querySelectorAll("#cycleSeq .color-dot");
  ambientFx.colors.slice(0, 4).forEach((hex, i) => {
    if (dots[i]) dots[i].style.background = hex;
  });
}

async function pushAmbientFx(extra = {}) {
  Object.assign(ambientFx, extra);
  ambientFx.bright = Number($("bgBright").value);
  ambientFx.speed_ms = Number($("bgSpeed")?.value || ambientFx.speed_ms);
  ambientFx.hex = bgHex;
  setBgPowerUi(true, { lock: true });
  return api("/api/bg/fx", {
    method: "POST",
    body: {
      fx: ambientFx.fx,
      bright: ambientFx.bright,
      speed_ms: ambientFx.speed_ms,
      hex: ambientFx.hex,
      colors: ambientFx.colors,
      direction: ambientFx.direction,
    },
  });
}

document.querySelectorAll(".fx-mode").forEach((btn) => {
  btn.addEventListener("click", async () => {
    if (btn.disabled) return;
    setAmbientFxUi(btn.dataset.fx);
    await pushAmbientFx({ fx: btn.dataset.fx });
  });
});

document.querySelectorAll("#flowDir .dir-btn").forEach((btn) => {
  btn.addEventListener("click", async () => {
    await pushAmbientFx({ direction: btn.dataset.dir });
    setAmbientFxUi("flow");
  });
});

$("bgSpeed")?.addEventListener(
  "input",
  debounce(async (e) => {
    const v = Number(e.target.value);
    $("bgSpeedVal").textContent = String(v);
    await pushAmbientFx({ speed_ms: v });
  }, 180)
);

/* —— Controls —— */
const SOURCES = ["iina", "browser", "screen"];
const SMOOTH_DEFAULT = { iina: 20, browser: 8, screen: 12 };
const SOURCE_LABELS = { iina: "IINA", browser: "浏览器", screen: "屏幕" };
const RANGE_DEFAULT_MAIN = 5;
const RANGE_DEFAULT_BG = 30;
const CAPTURE_TARGET_LABELS = {
  display: "整屏",
  front_window: "前台窗口",
  user_rect: "自选区域",
};
let _cachedDisplays = null;
let _lastActiveSource = "iina";
let _priorityDragSrc = null;

function sid(source, name) {
  return $(`${source}-${name}`);
}

async function refreshConfig() {
  const { data } = await api("/api/config");
  if (!data) return null;
  $("hostInput").value = data.host || "";
  setToggle($("syncEnable"), !!data.sync?.enabled);
  for (const source of SOURCES) {
    const en = data.sync?.[source]?.enabled;
    if (typeof en === "boolean") setToggle($(`${source}Enable`), en);
  }

  const activeParams =
    data.sync?.browser?.enabled && !data.sync?.iina?.enabled && !data.sync?.screen?.enabled
      ? data.sync.browser
      : data.sync?.screen?.enabled && !data.sync?.iina?.enabled && !data.sync?.browser?.enabled
        ? data.sync.screen
        : data.sync?.iina || {};
  const baseMain = Number(activeParams.base_main_bright ?? 50);
  const baseBg = Number(activeParams.base_ambient_bright ?? 40);
  $("mainBright").value = String(baseMain);
  $("mainBrightVal").textContent = String(baseMain);
  $("bgBright").value = String(baseBg);
  $("bgBrightVal").textContent = String(baseBg);

  for (const source of SOURCES) {
    fillSourceForm(source, data.sync?.[source] || {});
  }
  fillScreenExtras(data.sync?.screen || {});
  renderPriorityList(data.sync?.priority);
  await refreshDisplays();
  for (const source of SOURCES) {
    fillDisplaySelect(sid(source, "displaySelect"), data.sync?.[source]?.display_id);
  }

  $("launchAtLogin").checked = !!data.launch_at_login;
  if (data.ambient) {
    ambientFx = {
      fx: data.ambient.fx || "static",
      bright: data.ambient.bright ?? 40,
      speed_ms: data.ambient.speed_ms ?? 3800,
      hex: data.ambient.hex || bgHex,
      colors: data.ambient.colors || ambientFx.colors,
      direction: data.ambient.direction || "out",
    };
    bgHex = ambientFx.hex;
    setAmbientFxUi(ambientFx.fx);
  }
  applyDriveUi(data);
  return data;
}

let driveMode = "picture";

function applyDriveUi(data) {
  driveMode = data?.drive_mode === "music" ? "music" : "picture";
  document.body.classList.toggle("drive-music", driveMode === "music");
  document.querySelectorAll(".drive-switch .seg-btn").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.drive === driveMode);
  });
  const music = data?.music || {};
  if (driveMode === "music") {
    const bright = Number(music.bright ?? 40);
    $("bgBright").value = String(bright);
    $("bgBrightVal").textContent = String(bright);
  }
  if ($("musicBrightHint")) $("musicBrightHint").hidden = driveMode !== "music";
  if ($("driveStatus") && !data?.statusError) {
    $("driveStatus").textContent =
      driveMode === "music"
        ? "音乐跃动开着，氛围灯跟声音。屏幕灯可以照常用模式改"
        : "追色按优先级跟画面";
  }
  const input = music.input === "mic" ? "mic" : "system";
  document.querySelectorAll("#musicInputSeg .seg-btn").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.input === input);
  });
  const styles = new Set(music.styles || ["beat"]);
  for (const id of ["musicStyleBeat", "musicStyleLoud", "musicStyleSpec"]) {
    const el = $(id);
    if (el) el.checked = styles.has(el.dataset.style);
  }
  const palette = music.palette === "ambient" ? "ambient" : "builtin";
  document.querySelectorAll("#musicPaletteSeg .seg-btn").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.palette === palette);
  });
  if ($("bgPower")) $("bgPower").disabled = driveMode === "music";
}

async function setDrive(mode) {
  let status = 0;
  let data = null;
  try {
    if (mode === "music") {
      const input =
        document.querySelector("#musicInputSeg .seg-btn.active")?.dataset?.input || "system";
      // 日常切换只校验，不弹权限；新版本首次启动已在主进程申请过。
      await window.yeelight?.ensureMusicPermission?.(input, false);
    }
    const res = await api("/api/drive", { method: "POST", body: { mode } });
    status = res?.status || 0;
    data = res?.data;
  } catch (_) {
    status = 0;
  }
  if (!data || status >= 400) {
    const msg = data?.detail || "切换失败，本地服务未启动";
    if ($("driveStatus")) $("driveStatus").textContent = msg;
    if ($("musicStatus")) $("musicStatus").textContent = msg;
    return;
  }
  applyDriveUi({ drive_mode: data?.drive_mode, music: data?.music });
  await refreshMusicStatus();
}

function checkedMusicStyles() {
  return ["musicStyleBeat", "musicStyleLoud", "musicStyleSpec"]
    .map((id) => $(id))
    .filter((el) => el && el.checked)
    .map((el) => el.dataset.style);
}

async function patchMusic(partial) {
  if (partial?.input) {
    await window.yeelight?.ensureMusicPermission?.(partial.input, false);
  }
  const { data } = await api("/api/music", { method: "PATCH", body: partial });
  if (data?.music) applyDriveUi({ drive_mode: data.drive_mode || driveMode, music: data.music });
  await refreshMusicStatus();
}

function setMusicPermActionsVisible(show) {
  const el = $("musicPermActions");
  if (el) el.hidden = !show;
}

async function refreshMusicStatus() {
  // 不再每次轮询写权限文件 / 触发探测，避免和 TCC 打架。
  const { data } = await api("/api/music");
  if (!data || !$("musicStatus")) return;
  const err = data.status?.error;
  const input = data.music?.input === "mic" ? "麦克风" : "电脑播放";
  if (data.drive_mode !== "music") {
    $("musicStatus").textContent = "未开启。在模式页或菜单切换追色 / 音乐跃动";
    setMusicPermActionsVisible(false);
    return;
  }
  $("musicStatus").textContent = err || (data.status?.running ? `正在听${input}` : "正在启动");
  const needScreen =
    data.music?.input !== "mic" &&
    !!(err && (String(err).includes("屏幕录制") || String(err).includes("录制权限")));
  setMusicPermActionsVisible(needScreen);
  if ($("driveStatus")) {
    $("driveStatus").textContent = err || `音乐跃动 · 正在听${input}`;
  }
}

$("btnMusicPermSettings")?.addEventListener("click", async () => {
  // 会先探测登记本程序，再打开设置（不只是空开页面）
  await window.yeelight?.openScreenRecordingSettings?.();
});

$("btnMusicPermRetry")?.addEventListener("click", async () => {
  const input =
    document.querySelector("#musicInputSeg .seg-btn.active")?.dataset?.input || "system";
  // force：更新后旧权限失效时必须重新触达系统 API
  await window.yeelight?.ensureMusicPermission?.(input, true, true);
  if (driveMode !== "music") await setDrive("music");
  else await patchMusic({ input });
  await refreshMusicStatus();
});

document.querySelectorAll(".drive-switch .seg-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    if (btn.dataset.drive && btn.dataset.drive !== driveMode) setDrive(btn.dataset.drive);
  });
});

document.querySelectorAll("#musicInputSeg .seg-btn").forEach((btn) => {
  btn.addEventListener("click", () => patchMusic({ input: btn.dataset.input }));
});

document.querySelectorAll("#musicPaletteSeg .seg-btn").forEach((btn) => {
  btn.addEventListener("click", () => patchMusic({ palette: btn.dataset.palette }));
});

document.querySelectorAll(".music-checks input").forEach((el) => {
  el.addEventListener("change", () => {
    const styles = checkedMusicStyles();
    if (!styles.length) {
      el.checked = true;
      return;
    }
    patchMusic({ styles: checkedMusicStyles() });
  });
});

function fillSourceForm(source, p) {
  const ch = p.channels || {};
  const colorEl = sid(source, "chColor");
  const mainEl = sid(source, "chMainBright");
  const bgEl = sid(source, "chBgBright");
  if (colorEl) colorEl.checked = !!ch.ambient_color;
  if (mainEl) mainEl.checked = !!ch.main_brightness;
  if (bgEl) bgEl.checked = !!ch.ambient_brightness;
  syncOptionVisibility(source);

  const sm = Math.round(
    Number(p.color_smooth ?? (SMOOTH_DEFAULT[source] || 20) / 100) * 100
  );
  const colorSmooth = sid(source, "colorSmooth");
  if (colorSmooth) {
    colorSmooth.value = String(sm);
    const sv = sid(source, "smoothVal");
    if (sv) sv.textContent = String(sm);
  }
  const mbs = Math.round(Number(p.main_bright_smooth ?? 0.2) * 100);
  const mainSmooth = sid(source, "mainBrightSmooth");
  if (mainSmooth) {
    mainSmooth.value = String(mbs);
    const el = sid(source, "mainBrightSmoothVal");
    if (el) el.textContent = String(mbs);
  }
  const bbs = Math.round(Number(p.ambient_bright_smooth ?? 0.2) * 100);
  const bgSmooth = sid(source, "bgBrightSmooth");
  if (bgSmooth) {
    bgSmooth.value = String(bbs);
    const el = sid(source, "bgBrightSmoothVal");
    if (el) el.textContent = String(bbs);
  }
  const mbr = Number(p.main_bright_range ?? 5);
  const mainRange = sid(source, "mainBrightRange");
  if (mainRange) {
    mainRange.value = String(mbr);
    const el = sid(source, "mainBrightRangeVal");
    if (el) el.textContent = String(mbr);
  }
  const bbr = Number(p.ambient_bright_range ?? 30);
  const bgRange = sid(source, "bgBrightRange");
  if (bgRange) {
    bgRange.value = String(bbr);
    const el = sid(source, "bgBrightRangeVal");
    if (el) el.textContent = String(bbr);
  }
  setRegionSeg(source, p.region || "window");
  const segEl = sid(source, "segmentColor");
  if (segEl) segEl.checked = !!p.segment_color;
  applySegmentRegionHints(source, !!p.segment_color);
  const exit = sid(source, "exitSelect");
  if (exit) exit.value = p.on_exit || "ambient_mode";
  fillDisplaySelect(sid(source, "displaySelect"), p.display_id);
}

function syncOptionVisibility(source) {
  const colorOn = !!sid(source, "chColor")?.checked;
  const mainOn = !!sid(source, "chMainBright")?.checked;
  const bgOn = !!sid(source, "chBgBright")?.checked;
  const hide = (name, hidden) => {
    const el = sid(source, name);
    if (el) el.hidden = hidden;
  };
  hide("groupColorOpts", !colorOn);
  hide("groupMainBrightOpts", !mainOn);
  hide("groupBgBrightOpts", !bgOn);
  const segOn = !!sid(source, "segmentColor")?.checked;
  const segHint = sid(source, "segmentHint");
  if (segHint) segHint.hidden = !(colorOn && segOn);
  applySegmentRegionHints(source, colorOn && segOn);
}

/** 左右分色开启时：高亮「大范围/整屏」，弱化「顶部」；必要时自动切到大范围 */
function applySegmentRegionHints(source, segmentOn) {
  const seg = document.querySelector(`#${source}-regionSeg`);
  if (seg) {
    seg.querySelectorAll(".seg-btn").forEach((btn) => {
      const r = btn.dataset.region;
      btn.classList.toggle("preferred", !!segmentOn && r === "full");
      btn.classList.toggle("dimmed", !!segmentOn && r === "top");
    });
  }
  if (source === "screen") {
    document.querySelectorAll("#screen-captureTargetSeg .seg-btn").forEach((btn) => {
      const t = btn.dataset.captureTarget;
      btn.classList.toggle("preferred", !!segmentOn && t === "display");
      btn.classList.toggle("dimmed", !!segmentOn && t === "user_rect");
    });
  }
}

function maybeUpgradeRegionForSegment(source) {
  const active = document.querySelector(`#${source}-regionSeg .seg-btn.active`);
  const region = active?.dataset.region || "window";
  if (region === "top") {
    setRegionSeg(source, "full");
    return "full";
  }
  return null;
}

function fillDisplaySelect(sel, selected) {
  if (!sel) return;
  sel.innerHTML = "";
  const displays = _cachedDisplays || [];
  if (!displays.length) {
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = "未检测到显示器";
    sel.appendChild(opt);
    return;
  }
  for (const d of displays) {
    const opt = document.createElement("option");
    opt.value = String(d.id);
    opt.textContent = `显示器 ${d.id} · ${d.width}×${d.height}${d.is_primary ? "（主）" : ""}`;
    sel.appendChild(opt);
  }
  if (selected != null) sel.value = String(selected);
}

async function refreshDisplays() {
  const { data } = await api("/api/displays");
  _cachedDisplays = data?.displays || [];
  for (const source of SOURCES) {
    const sel = sid(source, "displaySelect");
    const cur = sel?.value ? Number(sel.value) : null;
    fillDisplaySelect(sel, cur);
  }
}

function channelSyncing(kind) {
  // 主/氛围页滑杆：任一源开启对应通道且源已开，则滑杆显示基础亮度
  for (const source of SOURCES) {
    const en = $(`${source}Enable`);
    if (en?.getAttribute("aria-pressed") !== "true") continue;
    if (kind === "main" && sid(source, "chMainBright")?.checked) return true;
    if (kind === "bg" && sid(source, "chBgBright")?.checked) return true;
  }
  return false;
}

function fillScreenExtras(p) {
  const target = p.capture_target || "front_window";
  setCaptureTargetSeg(target);
  updateScreenTargetUi(target);
  const elig = Array.isArray(p.eligibility) ? p.eligibility : ["target_valid"];
  const always = $("screen-elig-always");
  const targetValid = $("screen-elig-target_valid");
  const fullscreenOnly = $("screen-elig-fullscreen_only");
  if (always) always.checked = elig.includes("always");
  if (targetValid) targetValid.checked = elig.includes("target_valid");
  if (fullscreenOnly) fullscreenOnly.checked = elig.includes("fullscreen_only");
  updateUserRectHint(p.user_rect);
}

function updateScreenTargetUi(target) {
  const t = target || "front_window";
  const regionField = $("screen-regionField");
  const pickRow = $("screen-pickRegionRow");
  const hint = $("screen-captureHint");
  if (regionField) regionField.hidden = t !== "front_window";
  if (pickRow) pickRow.hidden = t !== "user_rect";
  if (hint && !hint.textContent.startsWith("当前取色")) {
    if (t === "display") hint.textContent = "整屏：跟随所选显示器画面";
    else if (t === "user_rect") hint.textContent = "自选区域：请先框选，再按该矩形取色";
    else hint.textContent = "前台窗口：跟随当前最前的应用窗口";
  }
}

function setCaptureTargetSeg(target) {
  document.querySelectorAll("#screen-captureTargetSeg .seg-btn").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.captureTarget === target);
  });
  updateScreenTargetUi(target);
  const segOn = !!sid("screen", "segmentColor")?.checked;
  applySegmentRegionHints("screen", segOn);
}

function updateUserRectHint(rect) {
  const hint = $("screen-userRectHint");
  if (!hint) return;
  if (rect && Number(rect.width) > 0 && Number(rect.height) > 0) {
    hint.textContent = `已保存区域 ${Math.round(rect.width)}×${Math.round(rect.height)} @ (${Math.round(rect.left)}, ${Math.round(rect.top)})`;
  } else {
    hint.textContent = "尚未框选区域";
  }
}

function readEligibility() {
  const flags = [];
  if ($("screen-elig-always")?.checked) flags.push("always");
  if ($("screen-elig-target_valid")?.checked) flags.push("target_valid");
  if ($("screen-elig-fullscreen_only")?.checked) flags.push("fullscreen_only");
  return flags.length ? flags : ["target_valid"];
}

function renderPriorityList(order) {
  const list = $("syncPriorityList");
  if (!list) return;
  const seq = Array.isArray(order) && order.length ? order : SOURCES;
  const items = [...list.querySelectorAll(".priority-item")];
  const bySrc = Object.fromEntries(items.map((el) => [el.dataset.source, el]));
  for (const src of seq) {
    if (bySrc[src]) list.appendChild(bySrc[src]);
  }
}

function readPriorityOrder() {
  return [...document.querySelectorAll("#syncPriorityList .priority-item")]
    .map((el) => el.dataset.source)
    .filter(Boolean);
}

async function patchPriority(order) {
  await api("/api/config", { method: "PATCH", body: { sync: { priority: order } } });
  await refreshSync();
}

async function refreshState(opts = {}) {
  const force = !!opts.force;
  if (force) {
    mainPowerLockUntil = 0;
    bgPowerLockUntil = 0;
  }
  const { data } = await api(`/api/state${force ? "?force=true" : ""}`);
  const pill = $("connPill");
  if (!data?.connected) {
    pill.textContent = "离线";
    pill.classList.remove("on");
    $("connText").textContent = data?.error
      ? `未连上灯：${data.error}`
      : "服务在线 · 灯未连接";
    if (data?.state) applyLampStateToUi(data.state, { force });
    return;
  }
  pill.textContent = "在线";
  pill.classList.add("on");
  $("connText").textContent = `已连接 ${data.host}`;
  applyLampStateToUi(data.state || {}, { force });
}

function applyLampStateToUi(s, { force = false } = {}) {
  if (!s || typeof s !== "object") return;
  const now = performance.now();
  const mainOn = parsePowerOn(s.main_power) ?? parsePowerOn(s.power);
  const bgOn = parsePowerOn(s.bg_power);
  // 空值不改 UI，避免切页/强刷把开着的灯显示成关
  if (mainOn !== null && (force || now > mainPowerLockUntil)) {
    setMainPowerUi(mainOn);
  }
  if (bgOn !== null && (force || now > bgPowerLockUntil)) {
    setBgPowerUi(bgOn);
  }
  if (s.bright != null && s.bright !== "" && !mainBrightDragging && !ctDragging) {
    if (!channelSyncing("main")) {
      $("mainBright").value = s.bright;
      $("mainBrightVal").textContent = s.bright;
    }
  }
  if (s.ct != null && s.ct !== "" && !ctDragging) {
    mainCt = Number(s.ct);
    drawCtWheel();
  }
  if (driveMode !== "music" && s.bg_bright != null && s.bg_bright !== "") {
    if (!channelSyncing("bg")) {
      $("bgBright").value = s.bg_bright;
      $("bgBrightVal").textContent = s.bg_bright;
    }
  }
  if (driveMode !== "music" && s.bg_rgb_hex) {
    bgHex = String(s.bg_rgb_hex).toUpperCase();
    drawRgbWheel();
  }
}

async function refreshSync() {
  const { data: sync } = await api("/api/sync/status");
  if (sync?.last_rgb_hex) $("swatch").style.background = sync.last_rgb_hex;
  if (sync?.error) $("syncLine").textContent = `出错`;
  else if (sync?.active) $("syncLine").textContent = "正在同步";
  else if (sync?.running) $("syncLine").textContent = "已开启 · 等待播放";
  else $("syncLine").textContent = "未开启";

  const active = sync?.source;
  _lastActiveSource = active || _lastActiveSource;
  if ($("activeSourceLine")) {
    if (sync?.active && SOURCE_LABELS[active]) {
      $("activeSourceLine").textContent = `当前：${SOURCE_LABELS[active]}`;
    } else {
      $("activeSourceLine").textContent = "当前：—";
    }
  }

  if (Array.isArray(sync?.priority)) renderPriorityList(sync.priority);

  for (const source of SOURCES) {
    const key = `${source}_enabled`;
    const on =
      typeof sync?.[key] === "boolean"
        ? sync[key]
        : typeof sync?.params?.[source]?.enabled === "boolean"
          ? sync.params[source].enabled
          : null;
    if (on !== null) setToggle($(`${source}Enable`), on);
    $(`${source}Col`)?.classList.toggle("is-active", !!sync?.active && active === source);
    document
      .querySelector(`#syncPriorityList .priority-item[data-source="${source}"]`)
      ?.classList.toggle("is-active", !!sync?.active && active === source);
  }

  const effectiveOn = (source) => {
    const key = `${source}_enabled`;
    if (typeof sync?.[key] === "boolean") return sync[key];
    return $(`${source}Enable`)?.getAttribute("aria-pressed") === "true";
  };

  const cap = sync?.capture;
  if (cap?.media_source === "screen" || (sync?.active && active === "screen")) {
    const hint = $("screen-captureHint");
    if (hint) {
      const target = cap?.capture_target || sync?.screen?.capture_target || "front_window";
      const label = CAPTURE_TARGET_LABELS[target] || target;
      const owner = cap?.window_owner ? ` · ${cap.window_owner}` : "";
      const degraded = /降级|整屏/.test(String(sync?.screen?.reason || ""))
        ? ` · ${sync.screen.reason}`
        : "";
      hint.textContent = `当前取色：${label}${owner}${degraded}`;
    }
  } else if (cap?.source && cap.source !== "none") {
    const map = {
      "window:window": "窗口中部",
      "window:top": "窗口顶部",
      "window:full": "窗口大范围",
      "display:top": "屏幕顶部",
      "display:full": "屏幕中央",
      "display:window": "屏幕中部",
    };
    const label = map[cap.source] || cap.source;
    const owner = cap.window_owner ? ` · ${cap.window_owner}` : "";
    const mediaSrc = cap.media_source || active;
    const hintId =
      mediaSrc === "browser"
        ? "browser-captureHint"
        : mediaSrc === "screen"
          ? "screen-captureHint"
          : "iina-captureHint";
    const hint = $(hintId);
    if (hint) {
      hint.textContent = `当前取色：${label}${owner}${
        cap.has_window === false ? "（未找到播放窗）" : ""
      }`;
    }
  }

  const iina = sync?.iina || {};
  if (sync?.error && active === "iina") {
    $("iinaLine").textContent = sync.error;
    $("iinaHint").textContent = "同步异常，请检查灯具或插件";
  } else if (!effectiveOn("iina")) {
    $("iinaLine").textContent = "已关闭";
    $("iinaHint").textContent = "打开开关后跟 IINA 画面";
  } else if (!iina?.running) {
    $("iinaLine").textContent = "IINA 未运行";
    $("iinaHint").textContent = "播放时跟色，暂停保持";
  } else if (iina.source === "plugin") {
    $("iinaLine").textContent = iina.playing ? "播放中" : "已暂停";
    $("iinaHint").textContent = "已通过插件接收状态";
  } else if (iina.source === "process-no-plugin" || iina.source === "process-no-ipc") {
    $("iinaLine").textContent = "未收到插件状态";
    $("iinaHint").textContent = "请安装插件后 Cmd+Q 重开 IINA";
  } else {
    $("iinaLine").textContent = iina.playing ? "播放中" : "空闲";
    $("iinaHint").textContent = "播放时跟色，暂停保持";
  }

  const m = sync?.media || {};
  if (sync?.error && active === "browser") {
    $("browserLine").textContent = sync.error;
    $("browserHint").textContent = "同步异常，请检查灯具或扩展";
  } else if (!effectiveOn("browser")) {
    $("browserLine").textContent = "已关闭";
    $("browserHint").textContent = "打开开关后跟浏览器画面";
  } else if (
    m.source === "none" ||
    (!m.running && m.source !== "window-only" && m.source !== "title-fallback")
  ) {
    $("browserLine").textContent = "未收到扩展状态";
    $("browserHint").textContent = "安装扩展并刷新 B 站页 · 请关弹幕";
  } else if (m.source === "title-fallback") {
    $("browserLine").textContent = m.playing ? "跟色中（标题兜底）" : "空闲";
    $("browserHint").textContent = "建议重新加载扩展 · 请关弹幕";
  } else if (m.playing) {
    $("browserLine").textContent = "播放中";
    $("browserHint").textContent = "扩展在线 · 请关闭弹幕";
  } else if (m.running) {
    $("browserLine").textContent = "已暂停";
    $("browserHint").textContent = "暂停保持灯效 · 请关弹幕";
  } else {
    $("browserLine").textContent = "空闲";
    $("browserHint").textContent = "打开 B 站视频播放即可";
  }

  const scr = sync?.screen || {};
  if (sync?.error && active === "screen") {
    $("screenLine").textContent = sync.error;
    $("screenHint").textContent = "同步异常，请检查灯具或屏幕录制权限";
  } else if (!effectiveOn("screen")) {
    $("screenLine").textContent = "已关闭";
    $("screenHint").textContent = "打开开关后跟屏幕取色";
  } else if (scr.locked) {
    $("screenLine").textContent = "已锁屏";
    $("screenHint").textContent = "锁屏 / 息屏时按退出策略收尾";
  } else if (scr.eligible) {
    $("screenLine").textContent = sync?.active && active === "screen" ? "跟色中" : "可跟";
    const degrade = String(sync?.capture?.degrade || "");
    $("screenHint").textContent =
      degrade === "fallback_display"
        ? "已降级为整屏"
        : degrade === "previous"
          ? "跟上一前台窗口"
          : CAPTURE_TARGET_LABELS[scr.capture_target] || "跟整屏、前台窗或自选区域";
  } else {
    const reasonMap = {
      locked: "已锁屏",
      capture_fail: "取色失败",
      target_invalid: "目标无效",
      not_fullscreen: "未全屏",
    };
    $("screenLine").textContent = reasonMap[scr.reason] || scr.reason || "待命";
    $("screenHint").textContent = "等待可跟条件满足";
  }

  if (sync?.params?.screen?.user_rect) {
    updateUserRectHint(sync.params.screen.user_rect);
  }

  for (const source of SOURCES) {
    const statusEl = $(`prio-${source}-status`);
    if (!statusEl) continue;
    const line = $(`${source}Line`);
    statusEl.textContent = line?.textContent || "—";
  }
}

const patchSync = debounce(async (patch) => {
  await api("/api/config", { method: "PATCH", body: { sync: patch } });
  await refreshSync();
}, 200);

function patchSource(source, patch) {
  return patchSync({ [source]: patch });
}

async function patchSourceImmediate(source, patch) {
  const { status, data } = await api("/api/config", {
    method: "PATCH",
    body: { sync: { [source]: patch } },
  });
  return { status, data };
}

function bindSourceEnable(source) {
  const btn = $(`${source}Enable`);
  const hint = $(`${source}Hint`);
  btn?.addEventListener("click", async (e) => {
    e.stopPropagation();
    const on = btn.getAttribute("aria-pressed") !== "true";
    setToggle(btn, on);
    try {
      const { status, data } = await patchSourceImmediate(source, { enabled: on });
      const saved = data?.sync?.[source]?.enabled;
      if (status >= 400 || (data?.sync?.[source] && typeof saved === "undefined" && on)) {
        setToggle(btn, !on);
        if (hint) {
          hint.textContent =
            source === "browser"
              ? "请完全退出并重开 App（需加载新版服务）"
              : "开关保存失败，请重开 App / 服务";
        }
        return;
      }
      if (typeof saved === "boolean") setToggle(btn, saved);
      await refreshSync();
    } catch (_) {
      setToggle(btn, !on);
      if (hint) hint.textContent = "服务无响应，请重开 App";
    }
  });
}

for (const source of SOURCES) bindSourceEnable(source);

function setRegionSeg(source, region) {
  document.querySelectorAll(`#${source}-regionSeg .seg-btn`).forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.region === region);
  });
}

// 各源控件绑定
for (const source of SOURCES) {
  document.querySelectorAll(`input[data-source="${source}"][data-ch]`).forEach((el) => {
    el.addEventListener("change", () => {
      syncOptionVisibility(source);
      patchSource(source, { channels: { [el.dataset.ch]: el.checked } });
    });
  });
  document.querySelectorAll(`input[data-source="${source}"][data-key]`).forEach((el) => {
    el.addEventListener(
      "input",
      debounce(() => {
        const key = el.dataset.key;
        let val = Number(el.value);
        if (key.includes("smooth")) {
          const label =
            key === "color_smooth"
              ? sid(source, "smoothVal")
              : key === "main_bright_smooth"
                ? sid(source, "mainBrightSmoothVal")
                : sid(source, "bgBrightSmoothVal");
          if (label) label.textContent = String(val);
          val = val / 100;
        } else if (key === "main_bright_range") {
          const label = sid(source, "mainBrightRangeVal");
          if (label) label.textContent = String(val);
        } else if (key === "ambient_bright_range") {
          const label = sid(source, "bgBrightRangeVal");
          if (label) label.textContent = String(val);
        }
        patchSource(source, { [key]: val });
      }, 120)
    );
  });
  document.querySelectorAll(`button.btn-reset[data-source="${source}"]`).forEach((btn) => {
    btn.addEventListener("click", () => {
      const key = btn.dataset.reset;
      let val;
      if (key === "color_smooth") val = (SMOOTH_DEFAULT[source] || 20) / 100;
      else if (key.endsWith("_smooth")) val = SMOOTH_DEFAULT.iina / 100;
      else if (key === "main_bright_range") val = RANGE_DEFAULT_MAIN;
      else val = RANGE_DEFAULT_BG;
      if (key === "color_smooth") {
        sid(source, "colorSmooth").value = String(Math.round(val * 100));
        sid(source, "smoothVal").textContent = String(Math.round(val * 100));
      } else if (key === "main_bright_smooth") {
        sid(source, "mainBrightSmooth").value = String(Math.round(val * 100));
        sid(source, "mainBrightSmoothVal").textContent = String(Math.round(val * 100));
      } else if (key === "ambient_bright_smooth") {
        sid(source, "bgBrightSmooth").value = String(Math.round(val * 100));
        sid(source, "bgBrightSmoothVal").textContent = String(Math.round(val * 100));
      } else if (key === "main_bright_range") {
        sid(source, "mainBrightRange").value = String(val);
        sid(source, "mainBrightRangeVal").textContent = String(val);
      } else if (key === "ambient_bright_range") {
        sid(source, "bgBrightRange").value = String(val);
        sid(source, "bgBrightRangeVal").textContent = String(val);
      }
      patchSource(source, { [key]: val });
    });
  });
  sid(source, "displaySelect")?.addEventListener("change", (e) => {
    const v = e.target.value ? Number(e.target.value) : null;
    patchSource(source, { display_id: v });
  });
  sid(source, "exitSelect")?.addEventListener("change", (e) => {
    patchSource(source, { on_exit: e.target.value, exit_mode: e.target.value });
  });
  document.querySelectorAll(`#${source}-regionSeg .seg-btn`).forEach((btn) => {
    btn.addEventListener("click", () => {
      const region = btn.dataset.region;
      setRegionSeg(source, region);
      patchSource(source, { region });
      const segOn = !!sid(source, "segmentColor")?.checked;
      applySegmentRegionHints(source, segOn);
    });
  });
  sid(source, "segmentColor")?.addEventListener("change", (e) => {
    const on = !!e.target.checked;
    const patch = { segment_color: on };
    if (on) {
      const upgraded = maybeUpgradeRegionForSegment(source);
      if (upgraded) patch.region = upgraded;
    }
    syncOptionVisibility(source);
    applySegmentRegionHints(source, on);
    patchSource(source, patch);
  });
}

// 屏幕：取色目标 / 可跟条件 / 框选
document.querySelectorAll("#screen-captureTargetSeg .seg-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    const target = btn.dataset.captureTarget;
    setCaptureTargetSeg(target);
    patchSource("screen", { capture_target: target });
  });
});

["screen-elig-always", "screen-elig-target_valid", "screen-elig-fullscreen_only"].forEach((id) => {
  $(id)?.addEventListener("change", () => {
    patchSource("screen", { eligibility: readEligibility() });
  });
});

$("screen-pickRegion")?.addEventListener("click", async () => {
  if (window.yeelight?.openRegionPicker) {
    try {
      const result = await window.yeelight.openRegionPicker();
      if (result?.ok && result.user_rect) {
        updateUserRectHint(result.user_rect);
        setCaptureTargetSeg("user_rect");
        $("screenHint").textContent = "已保存自选区域";
        await refreshSync();
      } else if (result && result.ok === false && result.cancelled) {
        $("screenHint").textContent = "已取消框选";
      }
    } catch (_) {
      $("screenHint").textContent = "框选失败，请重试";
    }
  } else {
    $("screenHint").textContent = "请在桌面 App 中框选区域";
  }
});

function syncExpandButton(col) {
  if (!col) return;
  const btn = col.querySelector(".source-expand-btn");
  if (!btn) return;
  const open = col.classList.contains("is-open");
  btn.setAttribute("aria-expanded", open ? "true" : "false");
  btn.setAttribute("aria-label", open ? "收起设置" : "展开设置");
  btn.title = open ? "收起设置" : "展开设置";
}

document.querySelectorAll(".source-expand-btn").forEach((btn) => {
  syncExpandButton(btn.closest(".source-accordion"));
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    const col = btn.closest(".source-accordion");
    if (!col) return;
    col.classList.toggle("is-open");
    syncExpandButton(col);
  });
});

// 优先级拖拽
(function bindPriorityDrag() {
  const list = $("syncPriorityList");
  if (!list) return;
  list.querySelectorAll(".priority-item").forEach((item) => {
    item.addEventListener("dragstart", (e) => {
      _priorityDragSrc = item;
      item.classList.add("is-dragging");
      e.dataTransfer.effectAllowed = "move";
      try {
        e.dataTransfer.setData("text/plain", item.dataset.source || "");
      } catch (_) {}
    });
    item.addEventListener("dragend", () => {
      item.classList.remove("is-dragging");
      _priorityDragSrc = null;
      list.querySelectorAll(".priority-item").forEach((el) => el.classList.remove("drag-over"));
    });
    item.addEventListener("dragover", (e) => {
      e.preventDefault();
      e.dataTransfer.dropEffect = "move";
      if (!_priorityDragSrc || _priorityDragSrc === item) return;
      item.classList.add("drag-over");
      const rect = item.getBoundingClientRect();
      const before = e.clientY < rect.top + rect.height / 2;
      if (before) list.insertBefore(_priorityDragSrc, item);
      else list.insertBefore(_priorityDragSrc, item.nextSibling);
    });
    item.addEventListener("dragleave", () => item.classList.remove("drag-over"));
    item.addEventListener("drop", async (e) => {
      e.preventDefault();
      item.classList.remove("drag-over");
      const order = readPriorityOrder();
      await patchPriority(order);
    });
  });
})();

$("btnConnect").addEventListener("click", async () => {
  const host = $("hostInput").value.trim();
  await api("/api/connect", { method: "POST", body: { host } });
  await refreshState({ force: true });
});


$("btnDiscover").addEventListener("click", async () => {
  $("connText").textContent = "正在发现设备…";
  const { data } = await api("/api/discover");
  const devices = data?.devices || [];
  if (!devices.length) {
    $("connText").textContent = "未发现设备";
    return;
  }
  $("hostInput").value = devices[0].host;
  $("connText").textContent = `已填入 ${devices[0].host}`;
});

$("mainPower").addEventListener("click", async () => {
  const on = $("mainPower").getAttribute("aria-pressed") !== "true";
  setMainPowerUi(on, { lock: true });
  try {
    const { status, data } = await api("/api/power", {
      method: "POST",
      body: { target: "main", on },
    });
    if (status >= 400) {
      setMainPowerUi(!on, { lock: true });
      $("connText").textContent = "开关失败";
      return;
    }
    if (data?.state) applyLampStateToUi(data.state, { force: true });
  } catch (_) {
    setMainPowerUi(!on, { lock: true });
  }
});

$("bgPower").addEventListener("click", async () => {
  const on = $("bgPower").getAttribute("aria-pressed") !== "true";
  setBgPowerUi(on, { lock: true });
  try {
    const { status, data } = await api("/api/power", {
      method: "POST",
      body: { target: "bg", on },
    });
    if (status >= 400) {
      setBgPowerUi(!on, { lock: true });
      $("connText").textContent = "开关失败";
      return;
    }
    if (data?.state) applyLampStateToUi(data.state, { force: true });
  } catch (_) {
    setBgPowerUi(!on, { lock: true });
  }
});

let _brightSeq = 0;
function pushBright(target, bright) {
  const seq = ++_brightSeq;
  // 调亮度会开对应灯，底栏与页面状态联动
  if (target === "main") setMainPowerUi(true, { lock: true });
  else setBgPowerUi(true, { lock: true });
  api("/api/bright", { method: "POST", body: { target, bright } }).then(({ status }) => {
    if (status >= 400 && seq === _brightSeq) {
      $("connText").textContent = "调光失败";
    }
  });
}

$("mainBright").addEventListener("pointerdown", () => {
  mainBrightDragging = true;
});
$("mainBright").addEventListener("pointerup", () => {
  mainBrightDragging = false;
});
$("mainBright").addEventListener("pointercancel", () => {
  mainBrightDragging = false;
});
$("mainBright").addEventListener(
  "input",
  debounce((e) => {
    const bright = Number(e.target.value);
    $("mainBrightVal").textContent = bright;
    pushBright("main", bright);
  }, 80)
);
$("mainBright").addEventListener("change", () => {
  mainBrightDragging = false;
});

$("bgBright").addEventListener(
  "input",
  debounce((e) => {
    const bright = Number(e.target.value);
    $("bgBrightVal").textContent = bright;
    if (driveMode === "music" || ambientFx.fx === "static") {
      pushBright("bg", bright);
    } else {
      pushAmbientFx({ bright });
    }
  }, 80)
);

$("syncEnable").addEventListener("click", async () => {
  const on = $("syncEnable").getAttribute("aria-pressed") !== "true";
  setToggle($("syncEnable"), on);
  await api(`/api/sync/enable?enabled=${on}`, { method: "POST" });
  await refreshSync();
});

$("btnInstallChromeExt")?.addEventListener("click", async () => {
  try {
    if (window.yeelight?.installChromeExtension) {
      const r = await window.yeelight.installChromeExtension();
      $("browserHint").textContent = r?.ok
        ? "已打开扩展目录 · Chrome 开发者模式 → 加载已解压的扩展"
        : r?.error || "无法安装扩展";
      return;
    }
    const paths = (await window.yeelight?.extensionPaths?.()) || {};
    if (paths.chromeDir && window.yeelight?.revealPath) {
      await window.yeelight.revealPath(paths.chromeDir);
      $("browserHint").textContent = "已打开 chromium 目录 · chrome://extensions 加载";
      return;
    }
    $("browserHint").textContent = "请加载 browser-extension/chromium";
  } catch (_) {
    $("browserHint").textContent = "无法打开扩展目录";
  }
});

$("btnInstallSafariExt")?.addEventListener("click", async () => {
  try {
    const paths = (await window.yeelight?.extensionPaths?.()) || {};
    if (paths.safariProject && window.yeelight?.openPath) {
      const r = await window.yeelight.openPath(paths.safariProject);
      if (r?.ok) {
        $("browserHint").textContent = "已打开 Xcode 工程 · Run 后在 Safari 启用扩展";
        return;
      }
    }
    if (paths.readme && window.yeelight?.openPath) {
      await window.yeelight.openPath(paths.readme);
    }
    $("browserHint").textContent = "需 Xcode：见 browser-extension/README.md";
  } catch (_) {
    $("browserHint").textContent = "无法打开 Safari 工程";
  }
});

$("launchAtLogin").addEventListener("change", async (e) => {
  const enabled = e.target.checked;
  if (window.yeelight?.setLaunchAtLogin) {
    await window.yeelight.setLaunchAtLogin(enabled);
  } else {
    await api("/api/config", {
      method: "PATCH",
      body: { launch_at_login: enabled },
    });
  }
});

$("btnInstallIina")?.addEventListener("click", async () => {
  const r = await window.yeelight?.installIinaPlugin?.();
  $("connText").textContent = r?.ok
    ? "IINA 插件已安装，请 Cmd+Q 重开 IINA"
    : r?.error || "IINA 插件安装失败";
});

let _onboardStep = 0;

function setOnboardVisible(on) {
  $("onboard").hidden = !on;
}

function renderOnboard() {
  const title = $("onboardTitle");
  const desc = $("onboardDesc");
  const body = $("onboardBody");
  const next = $("onboardNext");
  const step = $("onboardStepLabel");
  $("onboardMsg").textContent = "";
  step.textContent = `第 ${_onboardStep + 1} 步 · 共 3 步`;
  body.innerHTML = "";
  if (_onboardStep === 0) {
    title.textContent = "连接挂灯";
    desc.textContent = "易来 App 打开该灯的局域网控制后，点发现或手动填写 IP。";
    body.innerHTML = `
      <div class="onboard-body-field">
        <input id="onboardHost" type="text" spellcheck="false" placeholder="192.168.x.x" />
        <button type="button" class="btn ghost" id="onboardDiscover">发现设备</button>
      </div>`;
    next.textContent = "连接并继续";
    $("onboardHost").value = $("hostInput")?.value || "";
    $("onboardDiscover").addEventListener("click", async () => {
      $("onboardMsg").textContent = "正在发现…";
      const { data } = await api("/api/discover");
      const devices = data?.devices || [];
      if (!devices.length) {
        $("onboardMsg").textContent = "未发现设备，请确认同一网段";
        return;
      }
      $("onboardHost").value = devices[0].host;
      $("onboardMsg").textContent = `发现 ${devices[0].name || devices[0].model || devices[0].host}`;
    });
  } else if (_onboardStep === 1) {
    title.textContent = "屏幕录制权限";
    desc.textContent = "跟色需要截取屏幕颜色。请在系统设置中勾选「Yeelight 显示器挂灯」。";
    next.textContent = "我已授权，继续";
    body.innerHTML = `<button type="button" class="btn ghost" id="onboardPerm">打开屏幕录制设置</button>`;
    $("onboardPerm").addEventListener("click", async () => {
      // 只打开设置，不调用 CGRequest（会反复弹系统授权框）。
      await window.yeelight?.openScreenRecordingSettings?.();
      $("onboardMsg").textContent = "在列表里打开本应用后回到这里点继续";
    });
  } else {
    title.textContent = "安装同步配套";
    desc.textContent =
      "IINA 插件用于本地电影，点一次即可装好，然后完全退出并重开 IINA。Chrome 扩展用于哔哩哔哩：先开开发者模式，再加载弹出的文件夹。Safari 需用 Xcode 运行后再启用。 / The IINA plugin follows local films. The Chrome extension follows Bilibili. Safari is enabled after an Xcode run.";
    next.textContent = "完成";
    body.innerHTML = `
      <div class="btn-row">
        <button type="button" class="btn ghost" id="onboardIina">安装 IINA 插件</button>
        <button type="button" class="btn ghost" id="onboardChrome">安装 Chrome 扩展</button>
      </div>`;
    $("onboardIina").addEventListener("click", async () => {
      const r = await window.yeelight?.installIinaPlugin?.();
      $("onboardMsg").textContent = r?.ok ? "IINA 插件已复制，请完全退出并重开 IINA" : r?.error || "安装失败";
    });
    $("onboardChrome").addEventListener("click", async () => {
      const r = await window.yeelight?.installChromeExtension?.();
      $("onboardMsg").textContent = r?.ok
        ? "已打开扩展目录。Chrome → 开发者模式 → 加载已解压的扩展程序"
        : r?.error || "安装失败";
    });
  }
}

async function finishOnboard(complete) {
  if (complete) {
    await api("/api/config", { method: "PATCH", body: { onboarding: { complete: true } } });
  }
  setOnboardVisible(false);
  if ($("setupBanner")) $("setupBanner").hidden = !!complete;
}

$("onboardSkip")?.addEventListener("click", () => finishOnboard(false));
$("btnContinueSetup")?.addEventListener("click", () => {
  _onboardStep = 0;
  setOnboardVisible(true);
  renderOnboard();
});
$("onboardNext")?.addEventListener("click", async () => {
  if (_onboardStep === 0) {
    const host = ($("onboardHost")?.value || "").trim();
    if (!host) {
      $("onboardMsg").textContent = "请填写或发现灯的 IP";
      return;
    }
    $("hostInput").value = host;
    const { status, data } = await api("/api/connect", { method: "POST", body: { host } });
    if (status >= 400) {
      $("onboardMsg").textContent = (data && data.detail) || "连接失败";
      return;
    }
    _onboardStep = 1;
    renderOnboard();
    return;
  }
  if (_onboardStep === 1) {
    const { data } = await api("/api/permissions/screen");
    if (!data?.granted) {
      $("onboardMsg").textContent = "还没有录制权限。可先稍后，跟色前再授权。";
    }
    _onboardStep = 2;
    renderOnboard();
    return;
  }
  await finishOnboard(true);
});

async function connectLocal() {
  if ($("connText")) $("connText").textContent = "本地服务启动中";
  const started = Date.now();
  while (Date.now() - started < 90000) {
    try {
      await api("/api/health");
      if ($("connText")) $("connText").textContent = "本地服务已就绪";
      return true;
    } catch (_) {
      await new Promise((r) => setTimeout(r, 1000));
    }
  }
  if ($("connText")) $("connText").textContent = "本地服务未启动";
  return false;
}

async function fillAppVersion() {
  const el = $("appVersion");
  if (!el) return;
  try {
    const info = await window.yeelight?.appInfo?.();
    if (info?.version) el.textContent = `版本 ${info.version}`;
  } catch (_) {}
}

async function boot() {
  drawCtWheel();
  drawRgbWheel();
  fillAppVersion().catch(() => {});
  const ready = await connectLocal();
  if (ready) {
    try {
      const data = await refreshConfig();
      await loadModes();
      await refreshState({ force: true });
      await refreshSync();
      setAmbientFxUi(ambientFx.fx);
      const onboardDone = !!(data && data.onboarding && data.onboarding.complete);
      if ($("setupBanner")) $("setupBanner").hidden = onboardDone;
      if (!onboardDone) {
        _onboardStep = 0;
        setOnboardVisible(true);
        renderOnboard();
      }
    } catch (_) {
      if ($("connText")) $("connText").textContent = "本地服务未启动";
    }
  }
  // 同步状态：仅同步页可见时低频刷新；灯态仅启动/切页/回到前台
  setInterval(() => {
    const syncPanel = $("panel-sync");
    if (syncPanel && !syncPanel.hidden) refreshSync().catch(() => {});
    if (driveMode === "music" || (syncPanel && !syncPanel.hidden)) {
      refreshMusicStatus().catch(() => {});
    }
  }, 2500);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") {
      refreshState({ force: true }).catch(() => {});
      const syncPanel = $("panel-sync");
      if (syncPanel && !syncPanel.hidden) refreshSync().catch(() => {});
    }
  });
  // 托盘/菜单改灯后主进程广播，立刻对齐底栏电源等
  if (window.yeelight?.onStateChanged) {
    window.yeelight.onStateChanged((payload) => {
      if (payload?.state) {
        applyLampStateToUi(payload.state, { force: true });
      } else {
        refreshState({ force: true }).catch(() => {});
      }
      refreshConfig().catch(() => {});
      const syncPanel = $("panel-sync");
      if (syncPanel && !syncPanel.hidden) refreshSync().catch(() => {});
    });
  }
}

boot();

