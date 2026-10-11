/**
 * 全局入口：低频保活即可，不要抢播放器实例的状态。
 */
const { console } = iina;
const { postJson } = require("./lib.js");

postJson({
  running: true,
  playing: false,
  event: "global-start",
  ts: Date.now() / 1000,
});

setInterval(function () {
  postJson({
    running: true,
    event: "global-heartbeat",
    ts: Date.now() / 1000,
  });
}, 5000);

console.log("yeelight sync global ready");
