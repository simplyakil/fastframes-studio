// FastFrames Studio — native macOS shell: a window with the Studio UI, a Dock icon and a menu bar.
// Starts the local Python server if it isn't running, and stops it on Quit (freeing the model's memory).
// Built by tools/make_app.sh; the repo folder is read from the FFSRepoPath key in Info.plist.
import AppKit
import WebKit

let port = ProcessInfo.processInfo.environment["FFS_PORT"] ?? "7870"
let base = URL(string: "http://127.0.0.1:\(port)")!

final class AppDelegate: NSObject, NSApplicationDelegate, WKUIDelegate, WKNavigationDelegate, NSWindowDelegate, WKScriptMessageHandler {
    var window: NSWindow!
    var web: WKWebView!
    var server: Process?
    var quitting = false

    func applicationDidFinishLaunching(_ note: Notification) {
        buildMenu()
        let cfg = WKWebViewConfiguration()
        cfg.mediaTypesRequiringUserActionForPlayback = []
        cfg.preferences.setValue(true, forKey: "developerExtrasEnabled")
        cfg.userContentController.add(self, name: "app")   // lets the page ask the app to quit
        web = WKWebView(frame: .zero, configuration: cfg)
        web.uiDelegate = self
        web.navigationDelegate = self
        web.setValue(false, forKey: "drawsBackground")

        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1240, height: 820),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.title = "FastFrames Studio"
        window.appearance = NSAppearance(named: .darkAqua)
        window.backgroundColor = NSColor(calibratedWhite: 0.118, alpha: 1)
        window.minSize = NSSize(width: 640, height: 520)
        window.contentView = web
        window.delegate = self
        window.setFrameAutosaveName("FastFramesStudioMain")
        if !window.setFrameUsingName("FastFramesStudioMain") { window.center() }
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)

        showStatus("Starting FastFrames Studio…")
        DispatchQueue.global().async { self.startServerIfNeeded() }
    }

    // MARK: server
    func serverUp() -> Bool {
        var req = URLRequest(url: base.appendingPathComponent("api/stats")); req.timeoutInterval = 1
        let sem = DispatchSemaphore(value: 0); var ok = false
        URLSession.shared.dataTask(with: req) { _, r, _ in ok = (r as? HTTPURLResponse)?.statusCode == 200; sem.signal() }.resume()
        _ = sem.wait(timeout: .now() + 2); return ok
    }

    func startServerIfNeeded() {
        if !serverUp() {
            let repo = (Bundle.main.object(forInfoDictionaryKey: "FFSRepoPath") as? String) ?? ""
            let py = ProcessInfo.processInfo.environment["FFS_PYTHON"] ?? "\(repo)/.venv/bin/python"
            guard FileManager.default.isExecutableFile(atPath: py) else {
                DispatchQueue.main.async { self.showStatus("Python environment not found at \(py).<br>See the README: create .venv in the app folder.") }
                return
            }
            let p = Process()
            p.executableURL = URL(fileURLWithPath: py)
            p.arguments = ["-s", "-E", "app.py"]
            p.currentDirectoryURL = URL(fileURLWithPath: repo)
            let logURL = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Logs/FastFrames Studio.log")
            FileManager.default.createFile(atPath: logURL.path, contents: nil)
            if let h = try? FileHandle(forWritingTo: logURL) { p.standardOutput = h; p.standardError = h }
            do { try p.run(); server = p } catch {
                DispatchQueue.main.async { self.showStatus("Could not start the server: \(error.localizedDescription)") }
                return
            }
            for _ in 0..<120 { if serverUp() { break }; Thread.sleep(forTimeInterval: 0.25) }
        }
        DispatchQueue.main.async { self.web.load(URLRequest(url: base)) }
    }

    func showStatus(_ html: String) {
        web.loadHTMLString("""
        <html><body style="margin:0;height:100vh;display:grid;place-items:center;background:#1e1e1e;color:#9a9a9a;
        font:14px -apple-system,sans-serif;text-align:center">\(html)</body></html>
        """, baseURL: nil)
    }

    func jobRunning() -> Bool {
        var req = URLRequest(url: base.appendingPathComponent("api/status")); req.timeoutInterval = 1
        let sem = DispatchSemaphore(value: 0); var running = false
        URLSession.shared.dataTask(with: req) { d, _, _ in
            if let d = d, let j = try? JSONSerialization.jsonObject(with: d) as? [String: Any] { running = (j["state"] as? String) == "running" }
            sem.signal()
        }.resume()
        _ = sem.wait(timeout: .now() + 2); return running
    }

    func stopServer() {
        var req = URLRequest(url: base.appendingPathComponent("api/quit"))
        req.httpMethod = "POST"; req.httpBody = "{}".data(using: .utf8); req.timeoutInterval = 2
        let sem = DispatchSemaphore(value: 0)
        URLSession.shared.dataTask(with: req) { _, _, _ in sem.signal() }.resume()
        _ = sem.wait(timeout: .now() + 3)
        if let p = server, p.isRunning { Thread.sleep(forTimeInterval: 1); if p.isRunning { p.terminate() } }
    }

    // MARK: app lifecycle
    func applicationShouldTerminateAfterLastWindowClosed(_ app: NSApplication) -> Bool { true }

    func applicationShouldTerminate(_ app: NSApplication) -> NSApplication.TerminateReply {
        if jobRunning() {
            let a = NSAlert()
            a.messageText = "A video is still generating"
            a.informativeText = "Quitting now cancels it."
            a.addButton(withTitle: "Keep Generating"); a.addButton(withTitle: "Quit Anyway")
            if a.runModal() == .alertFirstButtonReturn { return .terminateCancel }
            var req = URLRequest(url: base.appendingPathComponent("api/cancel")); req.httpMethod = "POST"; req.httpBody = "{}".data(using: .utf8)
            URLSession.shared.dataTask(with: req).resume(); Thread.sleep(forTimeInterval: 1.5)
        }
        stopServer()
        return .terminateNow
    }

    func applicationShouldHandleReopen(_ app: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        if !flag { window.makeKeyAndOrderFront(nil) }
        return true
    }

    // MARK: JavaScript dialogs (confirm/alert used by the UI)
    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let a = NSAlert(); a.messageText = message; a.addButton(withTitle: "OK")
        a.beginSheetModal(for: window) { _ in completionHandler() }
    }

    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let a = NSAlert(); a.messageText = message
        a.addButton(withTitle: "OK"); a.addButton(withTitle: "Cancel")
        a.beginSheetModal(for: window) { completionHandler($0 == .alertFirstButtonReturn) }
    }

    // Open external links in the default browser.
    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if let u = action.request.url, u.host != nil, u.host != "127.0.0.1", u.host != "localhost" {
            NSWorkspace.shared.open(u); decisionHandler(.cancel); return
        }
        decisionHandler(.allow)
    }

    // The page's Settings → Quit button quits the whole app.
    func userContentController(_ c: WKUserContentController, didReceive message: WKScriptMessage) {
        if (message.body as? String) == "quit" { NSApp.terminate(nil) }
    }

    // MARK: menu
    func buildMenu() {
        let main = NSMenu()
        let appItem = NSMenuItem(); main.addItem(appItem)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "About FastFrames Studio", action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Hide FastFrames Studio", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Quit FastFrames Studio", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu

        let editItem = NSMenuItem(); main.addItem(editItem)
        let edit = NSMenu(title: "Edit")
        edit.addItem(withTitle: "Undo", action: Selector(("undo:")), keyEquivalent: "z")
        edit.addItem(withTitle: "Redo", action: Selector(("redo:")), keyEquivalent: "Z")
        edit.addItem(.separator())
        edit.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = edit

        let viewItem = NSMenuItem(); main.addItem(viewItem)
        let view = NSMenu(title: "View")
        view.addItem(withTitle: "Reload", action: #selector(reload), keyEquivalent: "r")
        view.addItem(withTitle: "Video Generation", action: #selector(goVideo), keyEquivalent: "1")
        view.addItem(withTitle: "Prompt Generator", action: #selector(goGen), keyEquivalent: "2")
        view.addItem(withTitle: "Clips", action: #selector(goLib), keyEquivalent: "3")
        view.addItem(withTitle: "Settings", action: #selector(goSet), keyEquivalent: ",")
        view.addItem(.separator())
        view.addItem(withTitle: "Enter Full Screen", action: #selector(NSWindow.toggleFullScreen(_:)), keyEquivalent: "f")
        viewItem.submenu = view

        let winItem = NSMenuItem(); main.addItem(winItem)
        let win = NSMenu(title: "Window")
        win.addItem(withTitle: "Minimize", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        win.addItem(withTitle: "Close", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        winItem.submenu = win
        NSApp.windowsMenu = win
        NSApp.mainMenu = main
    }

    @objc func reload() { web.reload() }
    func go(_ page: String) { web.evaluateJavaScript("location.hash='\(page)'") }
    @objc func goVideo() { go("video") }
    @objc func goGen() { go("gen") }
    @objc func goLib() { go("lib") }
    @objc func goSet() { go("set") }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
