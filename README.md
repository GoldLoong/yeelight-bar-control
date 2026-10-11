# 显示器上方的那条灯

**Yeelight Bar** · macOS 菜单栏工具，让米家-Yeelight易来屏幕挂灯Pro跟着画面走。  
作者 / Author: [GoldLoong](https://github.com/GoldLoong)

这是一个**非官方**项目，与易来（Yeelight）及其关联公司无关。Yeelight 是其权利人的商标。灯具协议按局域网控制文档在本地使用，本仓库不提供云账号、不上传画面。

[English](#the-light-above-the-monitor) 在下方。

---

## 中文

我买了一盏米家-Yeelight易来屏幕挂灯Pro。灯很好用，官方也为游戏做了氛围灯：画面一变，背后的灯就跟着变。我用的是 Mac，平时不玩游戏，这套联动落不到我的场景里。

我想要的是另一条路。在 Mac 上播本地电影时，灯跟着片子走；看在线视频时，灯跟着网页里的画面走；没有在播放的时候，也可以让灯跟着整块屏幕的颜色走。官方没有把这件事做到 macOS 上，于是有了这个开源项目。

它只跑在这台 Mac 上，通过局域网和灯说话。菜单栏里一个图标，把本地影片、在线视频和屏幕色彩接到氛围灯上。

跟色这件事比开关更挑剔。易来的 TCP 通道大约每分钟几十次，拿来频繁推颜色会被配额拖死，拖色温时灯也不跟着走。所以开关、模式、氛围特效仍走 TCP，并在客户端用令牌桶限速；画面同步和已经开着的灯上的色温、主灯亮度，走 UDP。暂停时留下最后一种颜色，也留着这条 UDP 会话，方便接着用手拖。

画面从三条路来，同一时刻只跟其中一条：

- **IINA**：插件上报播放、暂停、拖动进度。拖动时立刻丢掉平滑、重新取色，跟切口对齐。
- **浏览器**：扩展只看哔哩哔哩的播放状态，只访问本机 `127.0.0.1`。跟色时请自己关掉弹幕。
- **屏幕**：整屏、前台窗口，或自己框一块区域。需要 macOS 的屏幕录制权限。

三条路各自有一套参数。谁先说话，由一条可以拖动的优先级决定，默认是 IINA，然后浏览器，然后屏幕。

### 它能做什么

- 主灯：开关、亮度、色温（大约 1700–6500K，色温盘左暖右冷）
- 氛围灯：开关、亮度、静态颜色；呼吸、循环、燃彩
- 模式预设，以及同步时的基础亮度加画面增量
- 菜单栏图标：左键迷你面板，右键快捷开关。关掉窗口只是隐藏，右键退出才会停掉本机服务

### 开始之前

1. 在易来 App 里打开这盏灯的**局域网控制**
2. Mac 和灯在同一网段
3. 取色需要系统设置里的**屏幕录制**权限（打包后的 `.app` 和开发时的 Electron 是两条权限）
4. macOS，Python 3，以及 Node.js（只在跑界面或打包时需要）

把灯的地址写进配置。模板在 `config.example.json`，运行时用的是：

`~/Library/Application Support/com.yeelightbar.control/config.json`

仓库里的 `config.json` 不会提交。

### 安装

文件在 [Releases](https://github.com/GoldLoong/yeelight-bar-control/releases) 的 **0.2.0**。主应用里已经带上 IINA 插件、Chrome 扩展和 Safari 工程，也可以下载单独的压缩包。

#### 主应用

1. 下载 `YeelightBar-0.2.0-mac.zip`，解压。
2. 把 **Yeelight 显示器挂灯** 拖进「应用程序」。
3. 第一次双击时，系统会说：Apple 无法验证「Yeelight 显示器挂灯」是否包含可能危害 Mac 安全或泄漏隐私的恶意软件。这是因为这份应用没有经过 Apple 公证。它只在你的电脑上控制局域网里的灯，不会把画面上传到网上。
4. 在提示里点 **完成**。不要点「移到废纸篓」。
5. 打开 **系统设置 → 隐私与安全性**，拉到页面下方。
6. 点 **仍要打开**，再点 **打开**。系统若要求输入开机密码，输入后确认。
7. 这一次确认之后，以后从启动台或「应用程序」打开，不会再出现这句提示。

也可以在 Finder 里按住 Control 点按这个应用，选择 **打开**，再在新对话框里点 **打开**。

菜单栏会出现图标。左键是迷你面板，右键可以退出。关掉窗口只是隐藏。

接着在易来 App 里打开这盏灯的局域网控制，并让 Mac 和灯处在同一网段。跟色前，在 **系统设置 → 隐私与安全性 → 屏幕录制** 里允许「Yeelight 显示器挂灯」。

#### IINA 插件

用来跟本地电影。

1. 打开挂灯应用，在「设备」里点 **安装 IINA 插件**。
2. 完全退出 IINA（Cmd+Q），再打开。
3. 确认插件 **Yeelight Bar Sync** 已启用，然后播放一段视频。

从源码安装时，也可以运行 `./scripts/install-iina-plugin.sh`，之后同样要重开 IINA。单独的压缩包是 `YeelightBar-iina-plugin-0.2.0.zip`。

#### Chrome / Edge 扩展

用来跟哔哩哔哩。跟色时请关掉弹幕。

1. 在挂灯应用的「浏览器」里点 **安装 Chrome 扩展**，Finder 会打开扩展文件夹。也可以改用 `YeelightBar-chrome-extension-0.2.0.zip`，解压后使用里面的文件夹。
2. 打开 `chrome://extensions`（Edge 是 `edge://extensions`），打开右上角的 **开发者模式**。
3. 点 **加载已解压的扩展程序**，选中刚才的文件夹。
4. 打开任意哔哩哔哩视频并播放。挂灯窗口应显示浏览器正在播放。
5. 在同步页打开 **浏览器** 开关。

若显示「未收到扩展状态」：在扩展页点重新加载，然后刷新哔哩哔哩标签页再播。

#### Safari 扩展

同样用来跟哔哩哔哩。Safari 不能直接加载文件夹。

1. 安装完整的 Xcode。只装命令行工具不够。
2. 解压 `YeelightBar-safari-extension-0.2.0.zip`，或使用仓库里的 `browser-extension/safari`。
3. 用 Xcode 打开 `YeelightBarSync.xcodeproj`。顶部选 **My Mac**，点 **Run** 一次。签名时选自己的 Apple ID 即可。
4. 打开 **Safari → 设置 → 扩展**，勾选 **Yeelight Bar Sync (Web)**。
5. 允许它访问 **bilibili.com** 和 **127.0.0.1**。
6. 若 Safari 不列出未签名扩展：菜单栏 **开发 → 允许未签名的扩展**。
7. 打开哔哩哔哩播放视频，并在挂灯应用里打开浏览器同步。

### 从源码运行

```bash
chmod +x scripts/*.sh
./scripts/start-app.sh
```

这会准备 Python 虚拟环境、装依赖，并打开菜单栏界面。本机 API 在 `http://127.0.0.1:8787`。

只要服务、不要窗口：

```bash
./scripts/start-server.sh
```

IINA 同步：

```bash
./scripts/install-iina-plugin.sh
```

然后完全退出 IINA（Cmd+Q）再打开，确认插件 **Yeelight Bar Sync** 已启用。说明见 [`iina-plugin/README.md`](iina-plugin/README.md)。

浏览器扩展见 [`browser-extension/README.md`](browser-extension/README.md)。Chrome 系可以加载 `browser-extension/chromium`；Safari 需要用 Xcode 跑一遍容器 App。

打一个可以双击的菜单栏 App：

```bash
./scripts/build-app.sh
```

产物在 `app/dist/mac/`。默认打当前机器的架构。Universal 包：`YEELIGHT_UNIVERSAL=1 ./scripts/build-app.sh`。第一次打开时的系统拦截，见上面的「主应用」。

### 仓库里有什么

| 目录 | 内容 |
|---|---|
| `server/` | 发现设备、TCP/UDP、取色、同步、HTTP API |
| `app/` | Electron 菜单栏与控制窗 |
| `iina-plugin/` | IINA 播放状态 |
| `browser-extension/` | Chromium 扩展与 Safari 工程 |
| `scripts/` | 启动、打包、安装插件 |
| `docs/` | 设计笔记：屏幕取色、浏览器跟色、独立 App |

音乐律动、多灯编组、场景商店、自动更新和上架公证，都不在这个仓库里。

### 许可

[MIT](LICENSE)。代码可以拿走用。灯、商标和官方 App 不是这份许可能给你的东西。

---

## The light above the monitor

I bought a Mijia-Yeelight screen light bar Pro: 米家-Yeelight易来屏幕挂灯Pro. The lamp is excellent, and the official app already follows games: when the picture changes, the light behind the screen changes with it. I use a Mac, and I don't play games, so that mode never reached the way I actually watch things.

I wanted a different path. While a local film plays on the Mac, the lamp follows the movie. While an online video plays, it follows the picture in the page. When nothing is playing, it can still follow the colors on the screen. The official app does not do this on macOS, so I made this project and released it as open source.

It runs only on this Mac and talks to the lamp over the local network. One menu-bar icon connects local films, online video, and the colors on screen to the ambient light.

Color is pickier than a power switch. The lamp's TCP channel only accepts a few dozen commands a minute. Pushing a new color that often stalls on the quota, and dragging the color temperature stops tracking. Switches, modes, and ambient effects still go over TCP, with a token bucket on the client. Picture sync, and color temperature or main brightness while the lamp is already on, go over UDP. A pause keeps the last color and keeps that UDP session, so the sliders still work.

Three sources can feed the picture. Only one of them drives the lamp at a time:

- **IINA.** A plugin reports play, pause, and seeks. A seek drops the smoothing and samples again, so a cut and the light arrive together.
- **Browser.** The extension watches Bilibili playback only, and talks only to `127.0.0.1` on this machine. Turn danmaku off yourself if you want the color of the frame.
- **Screen.** The whole display, the frontmost window, or a rectangle you draw. macOS screen recording permission is required.

Each source has its own settings. A draggable priority list decides who speaks. The default order is IINA, then the browser, then the screen.

### What it does

- Main light: power, brightness, color temperature (about 1700–6500K; warm on the left, cool on the right)
- Ambient light: power, brightness, a static color; breathing, cycling, and flow
- Mode presets, and a base brightness plus a delta taken from the picture
- A menu-bar icon: left click for the mini panel, right click for quick toggles. Closing the window only hides it. Quit from the menu stops the local service

### Before you start

1. Turn on **LAN control** for this lamp in the Yeelight app
2. Keep the Mac and the lamp on the same network
3. Grant **Screen Recording** if you want color sampling (the packaged `.app` and the dev Electron process are separate permissions)
4. macOS, Python 3, and Node.js (Node is only for the UI and for packaging)

Set the lamp address in config. The template is `config.example.json`. The file the app actually reads is:

`~/Library/Application Support/com.yeelightbar.control/config.json`

The `config.json` in this repository is not committed.

### Install

The files are on the **0.2.0** [release](https://github.com/GoldLoong/yeelight-bar-control/releases). The main app already includes the IINA plugin, the Chrome extension, and the Safari project. Each is also attached on its own.

#### Main app

1. Download `YeelightBar-0.2.0-mac.zip` and unzip it.
2. Move **Yeelight 显示器挂灯** into Applications.
3. The first double-click says Apple cannot verify whether Yeelight 显示器挂灯 contains malware that may harm your Mac or compromise your privacy. The app is not notarized by Apple. It controls the lamp on your local network and does not upload the picture.
4. Click **Done** in the alert. Leave the app where it is.
5. Open **System Settings → Privacy & Security** and scroll to the bottom.
6. Click **Open Anyway**, then **Open**. Enter your login password if macOS asks.
7. After this confirmation, launching it from the Applications folder no longer shows the alert.

You can also Control-click the app in Finder, choose **Open**, then click **Open** again.

A menu-bar icon appears. Left click opens the mini panel. Right click can quit. Closing the window only hides it.

Then turn on LAN control for this lamp in the Yeelight app, and keep the Mac on the same network. Before color sampling, allow **Yeelight 显示器挂灯** under **System Settings → Privacy & Security → Screen Recording**.

#### IINA plugin

This follows local films.

1. Open the lamp app and click **安装 IINA 插件** in the Device section.
2. Quit IINA completely (Cmd+Q), then open it again.
3. Confirm the **Yeelight Bar Sync** plugin is enabled, and play a video.

From a source checkout you can also run `./scripts/install-iina-plugin.sh`, then reopen IINA the same way. The separate archive is `YeelightBar-iina-plugin-0.2.0.zip`.

#### Chrome / Edge extension

This follows Bilibili. Turn danmaku off while sampling color.

1. In the lamp app, open the Browser section and click **安装 Chrome 扩展**. Finder opens the extension folder. Or unzip `YeelightBar-chrome-extension-0.2.0.zip` and use that folder.
2. Open `chrome://extensions` (Edge: `edge://extensions`) and turn on **Developer mode**.
3. Click **Load unpacked** and select that folder.
4. Open any Bilibili video and press play. The lamp window should show the browser playing.
5. Turn on the **Browser** switch on the sync page.

If it says no extension status has arrived, reload the extension, refresh the Bilibili tab, and play again.

#### Safari extension

This also follows Bilibili. Safari cannot load a plain folder.

1. Install the full Xcode app. The command-line tools alone are not enough.
2. Unzip `YeelightBar-safari-extension-0.2.0.zip`, or use `browser-extension/safari` from the repository.
3. Open `YeelightBarSync.xcodeproj` in Xcode. Choose **My Mac** at the top and click **Run** once. When signing, your own Apple ID is enough.
4. Open **Safari → Settings → Extensions** and enable **Yeelight Bar Sync (Web)**.
5. Allow it on **bilibili.com** and **127.0.0.1**.
6. If Safari hides unsigned extensions, use **Develop → Allow Unsigned Extensions**.
7. Play a Bilibili video and turn on browser sync in the lamp app.

### Run from source

```bash
chmod +x scripts/*.sh
./scripts/start-app.sh
```

That creates a Python virtual environment, installs dependencies, and opens the menu-bar UI. The local API is `http://127.0.0.1:8787`.

Service only:

```bash
./scripts/start-server.sh
```

IINA sync:

```bash
./scripts/install-iina-plugin.sh
```

Quit IINA completely (Cmd+Q), open it again, and confirm the **Yeelight Bar Sync** plugin is enabled. See [`iina-plugin/README.md`](iina-plugin/README.md).

The browser extension is documented in [`browser-extension/README.md`](browser-extension/README.md). Chromium-based browsers can load `browser-extension/chromium`. Safari needs one Xcode run of the container app.

Build a double-clickable menu-bar app:

```bash
./scripts/build-app.sh
```

The result is under `app/dist/mac/`. The default build matches this machine. For a universal build: `YEELIGHT_UNIVERSAL=1 ./scripts/build-app.sh`. The first-launch system warning is covered under Main app above.

### What is in the repo

| Path | What it is |
|---|---|
| `server/` | Discovery, TCP/UDP, sampling, sync, HTTP API |
| `app/` | Electron menu bar and control window |
| `iina-plugin/` | IINA playback state |
| `browser-extension/` | Chromium extension and the Safari project |
| `scripts/` | Start, package, install the plugin |
| `docs/` | Design notes: screen sampling, browser sync, the standalone app |

Music-reactive modes, lamp groups, a scene store, auto-update, and notarized distribution are not here.

### License

[MIT](LICENSE). You can take the code. The lamp, the trademarks, and the official app are not part of that grant.
