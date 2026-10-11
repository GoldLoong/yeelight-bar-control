(() => {
  /* global browser, chrome */
  const ext = typeof browser !== "undefined" ? browser : chrome;
  // 只在最可能有播放器的 frame 工作；顶层无 video 时 iframe 也会注入
  const MATCH_HOSTS = [
    "www.bilibili.com",
    "m.bilibili.com",
    "www.bilibili.tv",
    "bilibili.com",
    "bilibili.tv",
  ];

  let videoEl = null;
  let leaveSent = false;
  let lastSentAt = 0;

  function hostOk(hostname) {
    const h = (hostname || "").toLowerCase();
    return MATCH_HOSTS.some((m) => h === m || h.endsWith("." + m));
  }

  function isWatchPage() {
    try {
      const path = (window.top && window.top.location.pathname) || location.pathname || "";
      return (
        /\/video\//.test(path) ||
        /\/bangumi\//.test(path) ||
        /\/list\//.test(path) ||
        /\/watchlater/.test(path) ||
        /\/festival\//.test(path)
      );
    } catch (_) {
      // cross-origin iframe：本 frame 有 video 也当播放页
      return !!findVideo();
    }
  }

  function pageHost() {
    try {
      return (window.top && window.top.location.hostname) || location.hostname;
    } catch (_) {
      return location.hostname;
    }
  }

  function pageUrl() {
    try {
      return ((window.top && window.top.location.href) || location.href).split("?")[0];
    } catch (_) {
      return location.href.split("?")[0];
    }
  }

  function pageTitle() {
    try {
      return (window.top && window.top.document && window.top.document.title) || document.title || null;
    } catch (_) {
      return document.title || null;
    }
  }

  function findVideo() {
    return (
      document.querySelector("video.bpx-player-video-element") ||
      document.querySelector(".bpx-player-container video") ||
      document.querySelector("#bilibili-player video") ||
      document.querySelector("video")
    );
  }

  function post(payload) {
    const body = {
      provider: "browser",
      ts: Date.now() / 1000,
      url: pageUrl(),
      host: pageHost(),
      title: pageTitle(),
      fullscreen: !!(
        document.fullscreenElement ||
        document.webkitFullscreenElement ||
        (window.top && window.top.document && (window.top.document.fullscreenElement || window.top.document.webkitFullscreenElement))
      ),
      ...payload,
    };
    lastSentAt = Date.now();
    try {
      ext.runtime.sendMessage({ type: "media-hook", payload: body }, () => {
        void ext.runtime.lastError;
      });
    } catch (_) {
      /* extension context gone */
    }
  }

  function snapshot(event, extra = {}) {
    const v = videoEl || findVideo();
    videoEl = v;
    const host = pageHost();
    if (!hostOk(host)) {
      return;
    }
    if (!isWatchPage()) {
      if (window === window.top) {
        post({
          event: "exit",
          running: false,
          playing: false,
          pause: true,
          ...extra,
        });
      }
      return;
    }
    if (!v) {
      // 非 video 所在 frame（常见：顶层壳页）不要上报 idle，避免冲掉 iframe 播放态
      return;
    }
    const paused = !!v.paused || !!v.ended;
    const ended = !!v.ended;
    post({
      event: ended ? "ended" : event || (paused ? "pause" : "play"),
      running: !ended,
      playing: !paused && !ended,
      pause: paused,
      position: Number.isFinite(v.currentTime) ? v.currentTime : null,
      duration: Number.isFinite(v.duration) ? v.duration : null,
      seek: event === "seek",
      ...extra,
    });
  }

  function bindVideo(v) {
    if (!v || v === videoEl) return;
    if (videoEl) {
      videoEl.removeEventListener("play", onPlay);
      videoEl.removeEventListener("playing", onPlay);
      videoEl.removeEventListener("pause", onPause);
      videoEl.removeEventListener("ended", onEnded);
      videoEl.removeEventListener("seeking", onSeek);
      videoEl.removeEventListener("seeked", onSeek);
    }
    videoEl = v;
    v.addEventListener("play", onPlay);
    v.addEventListener("playing", onPlay);
    v.addEventListener("pause", onPause);
    v.addEventListener("ended", onEnded);
    v.addEventListener("seeking", onSeek);
    v.addEventListener("seeked", onSeek);
    snapshot(v.paused ? "pause" : "play");
  }

  function onPlay() {
    leaveSent = false;
    snapshot("play");
  }
  function onPause() {
    snapshot("pause");
  }
  function onEnded() {
    snapshot("ended");
  }
  function onSeek() {
    snapshot("seek");
  }

  function onVisibility() {
    // 仅本 frame 有 video 时才跟可见性，避免顶层误报 pause
    if (!findVideo() && !videoEl) return;
    if (document.visibilityState === "hidden") {
      snapshot("pause", { pause: true, playing: false });
    } else {
      snapshot("play");
    }
  }

  function sendExit() {
    if (leaveSent) return;
    if (window !== window.top) return;
    leaveSent = true;
    post({
      event: "exit",
      running: false,
      playing: false,
      pause: true,
    });
  }

  function tick() {
    if (!hostOk(pageHost())) return;
    const v = findVideo();
    if (v) bindVideo(v);
    if (videoEl && !videoEl.paused && !videoEl.ended) {
      snapshot("heartbeat");
    } else if (videoEl && window === window.top && isWatchPage() && Date.now() - lastSentAt > 2000) {
      // 仅本 frame 确有 video 时才做顶层兜底
      snapshot(videoEl.paused ? "pause" : "heartbeat");
    }
  }

  document.addEventListener("visibilitychange", onVisibility);
  window.addEventListener("pagehide", sendExit);
  window.addEventListener("beforeunload", sendExit);
  document.addEventListener("fullscreenchange", () => snapshot("heartbeat"));
  document.addEventListener("webkitfullscreenchange", () => snapshot("heartbeat"));

  const mo = new MutationObserver(() => {
    const v = findVideo();
    if (v) bindVideo(v);
  });
  mo.observe(document.documentElement, { childList: true, subtree: true });

  setInterval(tick, 800);
  tick();
})();
