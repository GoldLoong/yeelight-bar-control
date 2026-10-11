import Cocoa

final class InstallViewController: NSViewController {
    override func loadView() {
        view = NSView(frame: NSRect(x: 0, y: 0, width: 480, height: 360))
        view.wantsLayer = true
        view.layer?.backgroundColor = NSColor.windowBackgroundColor.cgColor

        let title = NSTextField(labelWithString: "Yeelight Bar Sync")
        title.font = .systemFont(ofSize: 22, weight: .semibold)
        title.translatesAutoresizingMaskIntoConstraints = false

        let body = NSTextField(wrappingLabelWithString: """
        本 App 用于在 Safari 中启用「易来挂灯」浏览器扩展。

        1. 若扩展未出现：先在 Xcode 中对本工程点 Run 一次
        2. 打开 Safari → 设置 → 扩展
        3. 勾选 Yeelight Bar Sync (Web)
        4. 对该扩展允许访问 bilibili.com，以及 127.0.0.1（本地服务）
        5. 打开 B 站视频页，播放一次；易来控制窗应显示浏览器播放中

        跟色时请手动关闭弹幕。
        """)
        body.font = .systemFont(ofSize: 13)
        body.translatesAutoresizingMaskIntoConstraints = false

        let openBtn = NSButton(title: "打开 Safari 设置", target: self, action: #selector(openSafariPrefs))
        openBtn.bezelStyle = .rounded
        openBtn.translatesAutoresizingMaskIntoConstraints = false

        view.addSubview(title)
        view.addSubview(body)
        view.addSubview(openBtn)

        NSLayoutConstraint.activate([
            title.topAnchor.constraint(equalTo: view.topAnchor, constant: 28),
            title.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: 28),
            title.trailingAnchor.constraint(equalTo: view.trailingAnchor, constant: -28),

            body.topAnchor.constraint(equalTo: title.bottomAnchor, constant: 16),
            body.leadingAnchor.constraint(equalTo: title.leadingAnchor),
            body.trailingAnchor.constraint(equalTo: title.trailingAnchor),

            openBtn.topAnchor.constraint(equalTo: body.bottomAnchor, constant: 24),
            openBtn.leadingAnchor.constraint(equalTo: title.leadingAnchor),
        ])
    }

    @objc private func openSafariPrefs() {
        let candidates = [
            "x-apple.systempreferences:com.apple.Safari-Settings.extension",
            "x-apple.systempreferences:com.apple.Safari-Settings",
        ]
        for s in candidates {
            if let u = URL(string: s), NSWorkspace.shared.open(u) {
                return
            }
        }
        if let app = NSWorkspace.shared.urlForApplication(withBundleIdentifier: "com.apple.Safari") {
            NSWorkspace.shared.open(app)
        }
    }
}
