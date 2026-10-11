# Yeelight Bar Sync（IINA 插件）

向本地服务 `http://127.0.0.1:8787/api/iina/hook` 上报：

- 播放 / 暂停 / 空闲
- 进度、时长、倍速
- **seek / 换片**：让服务端立刻清空平滑、重采颜色（与切场同步）

热路径只用 `http.post`，不拉起 curl，避免拖慢播片。

## 安装

```bash
./scripts/install-iina-plugin.sh
```

然后 **完全退出 IINA（Cmd+Q）再打开**，播放任意视频。挂灯 App 应显示「插件」。
