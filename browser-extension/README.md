# Yeelight Bar Sync（浏览器扩展）

向本机 `http://127.0.0.1:8787/api/media/hook` 上报 B 站播放 / 暂停 / 结束 / 退出。

配合控制窗打开 **浏览器** 同步开关。跟色时请 **手动关闭弹幕**。

## Chrome / Edge / Chromium

1. 打开 `chrome://extensions`（Edge：`edge://extensions`）
2. 开启「开发者模式」
3. 「加载已解压的扩展程序」→ 选择：

```text
browser-extension/chromium
```

4. 打开任意 B 站视频页，播放一次；控制窗应显示「浏览器播放中」

若显示「未收到扩展状态」：在扩展页点 **重新加载**，然后 **刷新 B 站标签页** 再播。

改扩展代码后必须重新加载扩展。

## Safari（macOS）

Safari 扩展必须包在原生 App 里，不能像 Chrome 那样直接「加载文件夹」。

### 推荐步骤

1. 安装完整 **Xcode**（App Store；仅 Command Line Tools 不够）
2. 同步脚本并打开工程：

```bash
chmod +x scripts/build-safari-extension.sh
./scripts/build-safari-extension.sh
open browser-extension/safari/YeelightBarSync/YeelightBarSync.xcodeproj
```

3. 在 Xcode 顶部选 **My Mac**，点 **Run**（▶）一次  
   - 若提示签名：选自己的 Apple ID（Personal Team）即可本地调试
4. 打开 **Safari → 设置 → 扩展**
5. 勾选 **Yeelight Bar Sync (Web)**
6. 允许扩展访问 **bilibili.com**，以及 **127.0.0.1**（本地挂灯服务）
7. 若使用未签名开发扩展：Safari 菜单 **开发 → 允许未签名的扩展**
8. 打开 B 站视频播放；易来控制窗应显示浏览器播放中

仓库已含手写 Xcode 工程与 `safari/Resources`（与 Chromium 同源脚本）。若本机有 `safari-web-extension-converter`，构建脚本也可重新生成工程。

### 目录

```text
browser-extension/safari/
  Resources/                 # Web 扩展（manifest + JS + 图标）
  YeelightBarSync/           # macOS 容器 App + Appex 工程
```

## 权限说明

- 仅匹配 bilibili 域名
- 仅请求本机 `127.0.0.1:8787`
- 不上报 cookie / 登录态
