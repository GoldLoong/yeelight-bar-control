/* global browser, chrome */
const ext = typeof browser !== "undefined" ? browser : chrome;
const API = "http://127.0.0.1:8787/api/media/hook";

async function postHook(body) {
  const res = await fetch(API, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    throw new Error(`HTTP ${res.status}`);
  }
  return res.json().catch(() => ({}));
}

ext.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (!msg || msg.type !== "media-hook") {
    return;
  }
  const body = msg.payload || {};
  postHook(body)
    .then((data) => sendResponse({ ok: true, data }))
    .catch((err) => sendResponse({ ok: false, error: String(err) }));
  return true;
});

// 保活：避免 MV3 service worker 睡死导致漏报（Safari 无 alarms 时忽略）
try {
  if (ext.alarms && ext.alarms.create) {
    ext.alarms.create("yeelight-keepalive", { periodInMinutes: 1 });
    ext.alarms.onAlarm.addListener(() => {});
  }
} catch (_) {
  /* Safari / older runtime */
}
