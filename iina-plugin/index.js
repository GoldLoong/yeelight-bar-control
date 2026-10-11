/**
 * IINA 播放器插件：
 * - 高频上报播放态 / 进度 / 倍速
 * - 检测 seek，让服务端立刻重置颜色管线（“预告”场景切换）
 * - 热路径零外部进程，不影响播片
 */
const { core, event, mpv, console } = iina;
const { postJson } = require("./lib.js");

let lastPos = -1;
let lastPlayingKey = "";
let lastSeekAt = 0;

function nowSec() {
  return Date.now() / 1000;
}

function readNumber(getter, fallback) {
  try {
    const v = getter();
    if (v === null || v === undefined || Number.isNaN(Number(v))) return fallback;
    return Number(v);
  } catch (_) {
    return fallback;
  }
}

function snapshot(extra) {
  const paused = !!core.status.paused;
  const idle = !!core.status.idle;
  const playing = !paused && !idle;
  const position = readNumber(function () {
    return core.status.position;
  }, null);
  const duration = readNumber(function () {
    return core.status.duration;
  }, null);
  const speed = readNumber(function () {
    return mpv.getNumber("speed");
  }, 1.0);

  return {
    running: true,
    playing: playing,
    pause: paused,
    idle: idle,
    path: core.status.url || null,
    title: core.status.title || null,
    position: position,
    duration: duration,
    speed: speed,
    event: (extra && extra.event) || "update",
    seek: !!(extra && extra.seek),
    ts: nowSec(),
  };
}

async function report(extra) {
  const body = snapshot(extra || {});

  // seek 检测：进度突变（且不在刚 seek 的防抖窗内）
  if (
    body.playing &&
    body.position != null &&
    lastPos >= 0 &&
    Math.abs(body.position - lastPos) > 1.25 &&
    nowSec() - lastSeekAt > 0.35
  ) {
    body.seek = true;
    body.event = "seek";
    lastSeekAt = nowSec();
  }
  if (body.position != null) lastPos = body.position;

  const key = [
    body.playing,
    body.pause,
    body.idle,
    body.path,
    body.event,
    body.seek ? 1 : 0,
    body.position != null ? Math.floor(body.position * 2) : -1,
  ].join("|");

  // poll 时若关键状态未变则跳过，减负载
  if (body.event === "poll" && !body.seek && key === lastPlayingKey) return;
  lastPlayingKey = key;

  await postJson(body);
}

function bind() {
  event.on("iina.file-loaded", function () {
    lastPos = -1;
    report({ event: "file-loaded", seek: true });
  });
  event.on("iina.file-started", function () {
    report({ event: "file-started", seek: true });
  });
  event.on("mpv.pause.changed", function () {
    report({ event: "pause" });
  });
  event.on("mpv.idle-active.changed", function () {
    report({ event: "idle" });
  });
  event.on("mpv.speed.changed", function () {
    report({ event: "speed" });
  });
  event.on("iina.window-loaded", function () {
    report({ event: "window-loaded" });
  });
  event.on("iina.window-will-close", function () {
    // 关窗不等于退 App：running 可仍为 true，但必须立刻停跟色
    lastPlayingKey = "";
    postJson({
      running: true,
      playing: false,
      pause: true,
      idle: true,
      event: "window-close",
      ts: nowSec(),
    });
  });
  event.on("iina.will-quit", function () {
    postJson({
      running: false,
      playing: false,
      pause: true,
      idle: true,
      event: "will-quit",
      ts: nowSec(),
    });
  });
  // 部分版本无 will-quit，用 mpv shutdown 兜底
  try {
    event.on("mpv.shutdown", function () {
      postJson({
        running: false,
        playing: false,
        pause: true,
        idle: true,
        event: "shutdown",
        ts: nowSec(),
      });
    });
  } catch (_) {}

  // 播放中 100ms 上报；由 poll 节流避免重复
  setInterval(function () {
    report({ event: "poll" });
  }, 100);

  report({ event: "plugin-start" });
  try {
    core.osd("Yeelight Sync 就绪");
  } catch (e) {
    console.log("osd failed: " + e);
  }
}

bind();
