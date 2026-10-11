# 独立 App 封装（产品化）— 设计文档

> 状态：**已落地（可双击 `.app`；默认本机架构）**  
> 日期：2026-09-06  
> 关联：[`交接文档.md`](../交接文档.md) · 浏览器跟色 · 屏幕取色同步  
> 图标草案：会话内已生成 App 图标 / 状态栏剪影（落地时转 icns + template PNG）

---

## 1. 一句话目标

把现在「仓库脚本启动的开发态 Electron + Python」收成一个可双击的 macOS 应用 **Yeelight 显示器挂灯**：Python 打进包，配置离开仓库，菜单栏常驻并可直接控灯，首次打开有引导。自己用或发给熟人侧载即可。

---

## 2. 已锁定决策

| # | 议题 | 决定 |
|---|---|---|
| D1 | 交付物 | 可双击的 `.app`（可拖进「应用程序」）。**不上架、不公证、不收费、无自动更新** |
| D2 | 对外名称 | **Yeelight 显示器挂灯**（窗口、托盘 tooltip、`.app` 文件名、引导页一致） |
| D3 | Bundle ID | `com.yeelightbar.control`（与 IINA 插件 `com.yeelightbar.sync` 同家族） |
| D4 | 架构 | **目标 Universal**。默认打本机架构（避免 electron-builder 把 600MB Python 拿去 lipo 卡死）。Python.org 框架本身是 universal2；Electron Universal：`YEELIGHT_UNIVERSAL=1 ./scripts/build-app.sh`（Python 在 builder 之后拷入） |
| D5 | 配套 | 包内带 **IINA 插件 + Chrome 扩展**，设置/引导里一键安装；**Safari 第一版不自动装** |
| D6 | 首次打开 | **简短引导**：连灯 → 屏幕录制权限 → 一键装 IINA/Chrome → 再进主界面 |
| D7 | 菜单栏交互 | **左键**迷你控制面板；**右键**快捷菜单；关窗与黄灯最小化都收到状态栏 |
| D8 | 迷你面板内容 | **全做**：主灯开/关+亮度、氛围开/关+亮度、色温、氛围色、同步总开/关、四模式快捷套用、「打开完整控制」 |
| D9 | 右键菜单 | 打开完整控制；主灯 / 氛围灯 / 同步 勾选；退出。亮度/色温只在迷你面板与大窗口 |
| D10 | Dock | **藏掉**：纯菜单栏 App；Cmd+Tab 不一定找得到 |
| D11 | 图标 | 新设计：App = 显示器+顶挂灯+氛围光；状态栏 = 同语义黑剪影模板图（浅色/深色菜单栏自动反色） |
| D12 | 配置位置 | `~/Library/Application Support/com.yeelightbar.control/config.json`（ASCII，不跟中文产品名） |
| D13 | 登录项 | 「登录时启动」**真正**调用 `app.setLoginItemSettings`（v1 欠账，这版一并做）；默认关 |
| D14 | 开发态 | `./scripts/start-app.sh` 仍可用；打包产物与源码跑两套路径，配置目录相同以免双份灯状态 |

---

## 3. 不做 / 边界

| 项 | 说明 |
|---|---|
| 不公证 / 不上架 | 熟人侧载可能看到「无法验证开发者」→ 引导：右键打开 / 系统设置 → 仍要打开 |
| 不收费 / 无许可证 | 本版只做可安装产品 |
| 无自动更新 | 换版本靠重新拖 `.app` |
| 不自动装 Safari 扩展 | 仍需 Xcode Run；引导里一句说明即可 |
| 不改控灯协议 | 热路径仍 UDP；TCP 配额策略不变 |
| 不把 API 绑到非 localhost | 仍 `127.0.0.1:8787` |
| 不做 Windows / 多灯组 / 音乐律动 / 场景商城 | 原后置项仍后置 |
| 迷你面板不做完整四 Tab | 完整设置只在大窗口 |

---

## 4. 用户故事与验收场景

### 4.1 主路径（发给熟人）

1. 把 `Yeelight 显示器挂灯.app` 拖进「应用程序」，双击打开  
2. 若 Gatekeeper 拦截：按引导右键打开一次  
3. **引导 1**：自动发现灯 → 选中 / 确认 IP → 连接成功  
4. **引导 2**：请求屏幕录制；跳转系统设置；授权后检测通过  
5. **引导 3**：一键安装 IINA 插件 + 复制 Chrome 扩展并打开扩展页（加载已解压的扩展）；提示 Cmd+Q 重开 IINA  
6. 进入主界面（大窗口从菜单栏弹出）。菜单栏出现新图标，**Dock 无图标**  
7. 关窗 / 点黄灯 → 窗口消失，菜单栏图标仍在，灯与同步继续  
8. **左键**菜单栏：迷你面板可开关主灯/氛围、调亮度/色温/氛围色、切四模式、开关同步  
9. **右键**：勾选主灯/氛围/同步，或打开完整控制，或退出（退出才停 Python）  
10. 设置里「登录时启动」勾上后，下次登录菜单栏自动出现  

### 4.2 验收清单（实现后勾）

- [x] 不依赖仓库路径：打包 Python 拉起 `server.main`，`api_rev=4`，无 `.venv`  
- [ ] Universal Electron：默认本机架构；`YEELIGHT_UNIVERSAL=1` 可选（Python 已是 universal2）  
- [x] 配置写在 Application Support，不写 `.app` 包内  
- [x] 开发态旧 `config.json` 可迁移一次到新目录  
- [x] 引导三步 UI 已接；每步可「稍后」  
- [ ] 一键装 IINA 插件后，重开 IINA 能上报 hook（真机点一次）  
- [ ] Chrome 扩展目录可「在 Finder 中显示」+ 打开 `chrome://extensions`（真机点一次）  
- [x] `LSUIElement=true`；accessory 藏 Dock  
- [x] 关窗、黄灯最小化都不退出（hide）  
- [x] 左键迷你面板：D8 所列控件已做  
- [x] 点面板外或再点图标：面板收起  
- [x] 右键三项勾选与退出  
- [x] 「打开完整控制」打开大窗  
- [x] 登录项接线 `setLoginItemSettings`（默认关；勾选后需重启登录再验一次）  
- [x] 托盘 template 图标已打进包  
- [ ] 退出菜单栏项会停 Python（请用右键「退出」验一次）  
- [ ] IINA / 浏览器 / 屏幕同步回归：从 `.app` 启动仍可跟色  

---

## 5. 架构

```
Yeelight 显示器挂灯.app
├── Contents/MacOS/          Electron（universal）
├── Contents/Resources/
│   ├── app.asar             UI（main / preload / renderer）
│   ├── icon.icns
│   ├── python/              内嵌 CPython universal2 + site-packages
│   ├── server/              FastAPI 源码（或等价打包入口）
│   ├── config.example.json
│   ├── iina-plugin/         一键复制源
│   └── browser-extension/chromium/
└── Info.plist               LSUIElement=true, CFBundleName, appId

菜单栏 Tray ──左键──► 迷你面板 BrowserWindow（无 Dock）
         └──右键──► NSMenu 快捷开关
大窗口 ← 「打开完整控制」；close/minimize → hide
              │ HTTP 127.0.0.1:8787
              ▼
     内嵌 Python ─ SyncEngine ─ 挂灯 LAN
```

原则：

- 打包后 **不再** `cwd` 到 git 仓库；`server` 与 Python 都从 `process.resourcesPath` 解析  
- 开发态：`app.isPackaged === false` 时仍用仓库 ROOT + `.venv`  
- 单实例锁保持；第二份打开只唤出大窗口或迷你面板  
- 已有健康 API 且 `api_rev` 足够则不重复拉 Python（与现网一致）  

---

## 6. 路径与运行时

### 6.1 配置

| 环境 | 配置文件 |
|---|---|
| 打包后 / 开发态统一目标 | `~/Library/Application Support/com.yeelightbar.control/config.json` |
| 模板 | 包内或仓库 `config.example.json` |
| 迁移 | 若新路径不存在，且仓库旁旧 `config.json` 存在 → 复制过去一次并打日志 |

`server/config.py` 的 `CONFIG_PATH` 改为：优先环境变量 `YEELIGHT_CONFIG_DIR`，否则 Application Support。Electron 拉起 Python 时注入该变量。

### 6.2 Python 进程

| 环境 | 解释器 | cwd / 模块 |
|---|---|---|
| 开发 | `YEELIGHT_PYTHON` 或 `.venv/bin/python` | 仓库 ROOT，`python -m server.main` |
| 打包 | `Resources/python/bin/python3` | `PYTHONPATH=Resources`，同样 `-m server.main` |

建议：**内嵌官方/standalone CPython universal2 + pip 安装 `requirements.txt`**，不要 PyInstaller 单文件（Quartz / numpy / mss 易碎）。在 Intel 机打 Universal 时：用 universal2 的 Python 与 wheels，避免「只编了 x86_64」。

### 6.3 端口与权限

- API 仍 `127.0.0.1:8787`  
- 屏幕录制：打包后权限绑的是 **`.app` bundle**，不是终端/Electron 开发版；引导必须针对本 App 勾选  
- 首次从「应用程序」开，系统会再问一次录制权限（即使开发态已经授过）  

---

## 7. 菜单栏与窗口

### 7.1 生命周期

- `Info.plist`：`LSUIElement` = true（或等价 `app.setActivationPolicy('accessory')`）  
- `app.dock.hide()` 作为双保险  
- 大窗口 `close`：preventDefault + hide（现网已有）  
- 大窗口 `minimize`：**拦截并 hide**，不要进 Dock（反正无 Dock）  
- 无「最小化到 Dock」；状态栏图标 = 唯一常驻入口  
- 点菜单栏左键：若迷你面板已开则关闭，否则打开并定位到图标下方  
- 迷你面板失焦（点外面）→ hide  
- 退出：仅右键「退出」或完整窗口里的退出（若有）；`before-quit` 停 Python  

### 7.2 迷你面板 UI（紧凑，不是第二个大窗）

建议尺寸约 **320×520** 内，分区：

1. **模式**：四按钮一排（办公 / 竞技 / 电影 / 休闲），点即 `POST /api/modes/{id}/apply`  
2. **主灯**：开关、亮度滑杆、色温（小色温条或滑杆，复用现有 ct 逻辑，不必整块色轮）  
3. **氛围**：开关、亮度、色点（预设色 + 当前色，完整色轮仍在大窗口）  
4. **同步**：总开关 + 一行状态（正在跟 / 待命）  
5. 底栏：**打开完整控制**  

实现：独立 `renderer/tray.html` + `tray.js`，经 preload 调同一套 API；不要复用整页四 Tab。

### 7.3 右键菜单

```
打开完整控制
────────────
主灯          ☑
氛围灯        ☑
同步          ☑
────────────
退出
```

勾选与 `/api/state`、`sync.enabled` 对齐；5s 刷新可保留，打开菜单时再拉一次更新。

### 7.4 图标落地

| 用途 | 规格 |
|---|---|
| App | 1024 → `icon.icns`（16…1024 多尺寸） |
| 菜单栏 | template PNG：16×16 @1x、32×32 @2x，**只有 alpha 的黑剪影**，`setTemplate(true)` |
| 剪影加粗 | 草案横条偏细，制作 template 时描边 ≥ 1.5px @16，保证视网膜上可辨 |

---

## 8. 首次引导

全屏或大窗口内步进，**未完成也可关**（关 = 收到菜单栏）；`config.onboarding.complete`（或同级字段）为 false 时每次打开大窗口先到引导。

| 步 | 内容 | 成功条件 |
|---|---|---|
| 1 连灯 | 调 `GET /api/discover`，列表选择；可手动 IP；`POST /api/connect` | `health` + 能读到 state 或明确已连接 |
| 2 录制权限 | 说明为何需要；按钮打开「屏幕录制」设置；轮询检测 | 取色 API / 系统权限 API 报已授权 |
| 3 配套安装 | 一键：复制 IINA 插件并尝试启用；复制 Chrome 扩展到 Application Support 并 `open chrome://extensions` + reveal 目录 | 文件已就位；扩展是否「已加载」不强制检测（Chrome 不能静默装） |
| 完成 | 写 `onboarding.complete=true`，打开完整控制窗 | |

「稍后」：不写 complete；大窗口顶栏保留「继续设置」入口（设置页同样提供一键安装，给引导跳过的人）。

Chrome 一键的诚实边界：只能 **打开扩展页 + 选中目录**，用户需开「开发者模式」并「加载已解压的扩展程序」。引导文案写清这两下。

---

## 9. 登录项

- UI 已有 `launch_at_login`：打包后 `app.setLoginItemSettings({ openAtLogin, openAsHidden: true })`  
- 隐藏启动：只出菜单栏，不弹大窗口、不弹引导（若 `onboarding.complete`；未完成则弹引导）  
- 开发态也可接线，方便本机验  

---

## 10. 代码改动地图

| 区域 | 改动 |
|---|---|
| `server/config.py` | Application Support 路径、`YEELIGHT_CONFIG_DIR`、onboarding 字段、旧配置迁移 |
| `app/main.js` | packaged Python 路径、accessory/Dock、minimize hide、迷你窗、登录项、引导权限、资源路径 |
| `app/preload.js` | tray / onboarding / install-plugin IPC |
| `app/renderer/tray.*` | **新建**迷你面板 |
| `app/renderer/*` | 引导页；设置里一键安装；产品名统一 |
| `app/package.json` | productName、build（electron-builder）、icons |
| `scripts/build-app.sh` | 新：prepare-runtime + electron-builder（默认本机架构）+ 事后拷 Python |
| `scripts/start-app.sh` | 开发态注入同一 `YEELIGHT_CONFIG_DIR` |
| `iina-plugin` / `browser-extension/chromium` | 作为 extraResources；安装逻辑从 shell 迁到 Electron（可复用现脚本思路） |
| `交接文档.md` / `README.md` | 启动方式改为安装 `.app`；开发脚本仍保留 |

---

## 11. 分期（同一 `.app` 发布前做全）

1. 路径：配置目录 + packaged Python 拉起 + 开发态兼容  
2. 打包脚本：universal Electron + 内嵌 Python + extraResources  
3. 菜单栏：藏 Dock、minimize/close、template 图标  
4. 迷你面板 + 右键三项勾选  
5. 引导三步 + 一键装 IINA/Chrome  
6. 登录项接线  
7. 从「应用程序」真机跑通控灯与 IINA 跟色  

开发期仍可用 `./scripts/start-app.sh` 调 UI；**验收必须以打出来的 `.app` 为准**。

---

## 12. 风险

| 风险 | 缓解 |
|---|---|
| Intel 机打出「假 Universal」（Python 只有 x86_64） | 用 universal2 CPython；构建后 `lipo -info` 检查 python3 与 Electron；文档写明检查命令 |
| Gatekeeper | 引导右键打开；不假装已公证 |
| 屏幕录制绑 bundle id | 引导针对本 `.app`；开发版与正式版权限分开 |
| Chrome 无法静默装扩展 | 文案 + 打开扩展页 + reveal；不承诺「点一下就装好」 |
| 迷你面板过长 | 紧凑分区、色温/氛围色用简化控件；完整色轮留大窗口 |
| 无 Dock 用户找不到 | 引导结束明确说「以后点菜单栏图标」；右键退出才是退出 |
| 双开 Python | 保持现网 health + api_rev；打包后不要再起仓库里的 `start-server.sh` 抢 8787 |

---

## 13. 开放问题（实现中可微调，不挡开工）

| # | 问题 | 倾向 |
|---|---|---|
| O1 | 交付是裸 `.app` 还是再打 zip | 先产出 `dist/Yeelight 显示器挂灯.app`，可选再 zip 方便隔空投送 |
| O2 | 迷你面板色温/氛围色交互 | 色温滑杆 + 氛围预设色点；不进迷你面板塞双色轮 |
| O3 | 引导能否跳过第 3 步 | 可以「稍后」；complete 仍可在仅连灯+权限后写入，扩展未装靠同步页提示 |
| O4 | 产品名含空格/中文作文件名 | `.app` 显示名用中文；磁盘 bundle 可用 `Yeelight Bar.app` 若 builder 踩坑再定 |

---

## 14. 文档维护

| 动作 | 何时 |
|---|---|
| 本文状态改为「已实现」 | 验收清单勾完 |
| 更新交接文档 §1/§3/§4 启动方式 | 合并封装时 |
| README 入口改为安装 `.app` | 同上 |

---

## 15. 摘要

**做：** 可双击 `.app`；内嵌 Python；配置进 Application Support；纯菜单栏（无 Dock）；左键迷你全控 + 右键快捷开关；首次引导连灯/权限/装 IINA+Chrome；登录项生效。  

**不做：** 公证、上架、收费、自动更新、Safari 一键装。  

**构建：** `./scripts/build-app.sh` → `app/dist/mac/Yeelight 显示器挂灯.app`。Universal Electron 用 `YEELIGHT_UNIVERSAL=1`。
