/**
 * 轻量上报：热路径只用 http.post，禁止每帧 curl（会严重拖慢 IINA/系统）。
 */
const { http, console } = iina;

const API = "http://127.0.0.1:8787/api/iina/hook";

async function postJson(body) {
  try {
    await http.post(API, {
      headers: { "Content-Type": "application/json" },
      data: body,
    });
    return true;
  } catch (err) {
    try {
      console.log("yeelight hook failed: " + err);
    } catch (_) {}
    return false;
  }
}

module.exports = {
  API: API,
  postJson: postJson,
};
