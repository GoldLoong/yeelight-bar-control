# 显示器上方的那条灯

**Yeelight Bar** · macOS 菜单栏工具，让易来显示器挂灯跟着画面走。  
作者 / Author: [GoldLoong](https://github.com/GoldLoong)

这是一个**非官方**项目，与易来（Yeelight）及其关联公司无关。Yeelight 是其权利人的商标。灯具协议按局域网控制文档在本地使用，本仓库不提供云账号、不上传画面。

[English](#the-light-above-the-monitor) 在下方。

---

## 中文

我有一条易来显示器挂灯 Pro。官方 App 能开灯、调色温，在 Windows 上也能跟着游戏走。我的片子不在那里。

电影在 IINA 里，视频在浏览器里，有时只是桌面上随便一个窗口亮着。我想让屏幕背后那圈氛围灯跟着这些画面变色，主灯该亮的时候仍然亮着，而且不要在 Dock 里多占一个图标。

于是留下了这个只住在菜单栏里的小工具。它在这台 Mac 和灯之间说话，不经过云。

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

### 跑起来

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

产物在 `app/dist/mac/`。默认打当前机器的架构。Universal 包：`YEELIGHT_UNIVERSAL=1 ./scripts/build-app.sh`。第一次打开若被系统拦住，在 Finder 里右键，选择「打开」。

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

I have a Yeelight monitor light bar Pro. The official app can turn it on, set the color temperature, and on Windows it can follow games. My films are not there.

They play in IINA. Videos play in the browser. Sometimes it is just whichever window happens to be bright on the desktop. I wanted the ambient strip behind the screen to follow those pictures, the main light to stay a reading light when it should, and no extra icon in the Dock.

So this menu-bar tool stayed. It talks between this Mac and the lamp. Nothing goes to a cloud.

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

### Run it

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

The result is under `app/dist/mac/`. The default build matches this machine. For a universal build: `YEELIGHT_UNIVERSAL=1 ./scripts/build-app.sh`. If macOS blocks the first launch, right-click the app in Finder and choose Open.

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
