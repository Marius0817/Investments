// stopguard für macOS – natives Fenster um die Weboberfläche.
//
// Die App startet `stopguard web` als Kindprozess (nur 127.0.0.1, frei gewählter Port,
// zufälliger Zugangsschlüssel je Start), meldet sich an und zeigt die Oberfläche in einem
// WKWebView. Beim Beenden – oder falls die App abstürzt – beendet sich auch der Server.
// Orders werden ausschließlich über die Oberfläche bestätigt (Vorschau → Häkchen → Platzieren);
// die App selbst löst nie eine Order aus.
//
// Bauen: deploy/macos/build-app.sh  (swiftc aus den Xcode Command Line Tools)

import AppKit
import Security
import WebKit

// MARK: - Pfade

enum Paths {
    static let home = FileManager.default.homeDirectoryForCurrentUser
    static let support = home.appendingPathComponent("Library/Application Support/stopguard", isDirectory: true)
    static let demoDir = support.appendingPathComponent("demo", isDirectory: true)
    static let log = home.appendingPathComponent("Library/Logs/stopguard-app.log")

    /// Ausführbare Datei von stopguard: Einstellung, Umgebung, dann die Orte von install.sh / build-app.sh.
    static func stopguardExecutable() -> URL? {
        var candidates: [String] = []
        if let custom = UserDefaults.standard.string(forKey: "StopguardExecutable") { candidates.append(custom) }
        if let env = ProcessInfo.processInfo.environment["STOPGUARD_BIN"] { candidates.append(env) }
        candidates += [
            home.appendingPathComponent(".local/share/stopguard/venv/bin/stopguard").path,
            home.appendingPathComponent(".local/bin/stopguard").path,
            "/opt/homebrew/bin/stopguard",
            "/usr/local/bin/stopguard",
        ]
        return candidates
            .map { URL(fileURLWithPath: ($0 as NSString).expandingTildeInPath) }
            .first { FileManager.default.isExecutableFile(atPath: $0.path) }
    }

    /// Konfiguration wie in der CLI: STOPGUARD_CONFIG, sonst ~/.config/stopguard/config.toml.
    static func defaultConfigExists() -> Bool {
        if let env = ProcessInfo.processInfo.environment["STOPGUARD_CONFIG"], FileManager.default.fileExists(atPath: env) { return true }
        let xdg = ProcessInfo.processInfo.environment["XDG_CONFIG_HOME"].map { URL(fileURLWithPath: $0) } ?? home.appendingPathComponent(".config")
        return FileManager.default.fileExists(atPath: xdg.appendingPathComponent("stopguard/config.toml").path)
    }

    static var searchPath: String {
        [home.appendingPathComponent(".local/bin").path, "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
            .joined(separator: ":")
    }
}

// MARK: - Serverprozess

final class ServerProcess {
    let token: String
    private let process = Process()
    private let pipe = Pipe()
    private var buffer = Data()
    private var logHandle: FileHandle?
    private(set) var output = ""
    var onURL: ((URL) -> Void)?
    var onExit: ((Int32, String) -> Void)?

    init(executable: URL, config: String?) {
        var bytes = [UInt8](repeating: 0, count: 24)
        if SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes) != errSecSuccess {
            bytes = (0..<24).map { _ in UInt8.random(in: 0...255) }
        }
        token = bytes.map { String(format: "%02x", $0) }.joined()
        process.executableURL = executable
        var args: [String] = []
        if let config { args += ["-c", config] }
        args += ["web", "--host", "127.0.0.1", "--port", "0", "--announce", "--exit-with-parent"]
        process.arguments = args
        var env = ProcessInfo.processInfo.environment
        env["STOPGUARD_WEB_TOKEN"] = token
        env["STOPGUARD_PARENT_PID"] = String(ProcessInfo.processInfo.processIdentifier)
        env["PATH"] = Paths.searchPath
        env["PYTHONUNBUFFERED"] = "1"
        env["NO_COLOR"] = "1"
        process.environment = env
        process.currentDirectoryURL = Paths.home
        process.standardOutput = pipe
        process.standardError = pipe
    }

    func start() throws {
        try? FileManager.default.createDirectory(at: Paths.log.deletingLastPathComponent(), withIntermediateDirectories: true)
        if !FileManager.default.fileExists(atPath: Paths.log.path) {
            FileManager.default.createFile(atPath: Paths.log.path, contents: nil)
        }
        logHandle = try? FileHandle(forWritingTo: Paths.log)
        logHandle?.seekToEndOfFile()
        pipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty else { return }
            DispatchQueue.main.async { self?.consume(data) }
        }
        process.terminationHandler = { [weak self] proc in
            let status = proc.terminationStatus
            DispatchQueue.main.async {
                guard let self else { return }
                self.pipe.fileHandleForReading.readabilityHandler = nil
                self.onExit?(status, self.output)
            }
        }
        try process.run()
    }

    private func consume(_ data: Data) {
        logHandle?.write(data)
        buffer.append(data)
        while let newline = buffer.firstIndex(of: 0x0A) {
            let line = String(decoding: buffer[buffer.startIndex..<newline], as: UTF8.self)
            buffer.removeSubrange(buffer.startIndex...newline)
            output += line + "\n"
            if output.count > 20_000 { output = String(output.suffix(8_000)) }
            let prefix = "STOPGUARD_URL="
            if line.hasPrefix(prefix), let url = URL(string: line.dropFirst(prefix.count).trimmingCharacters(in: .whitespaces)) {
                onURL?(url)
            }
        }
    }

    var isRunning: Bool { process.isRunning }

    func stop() {
        onExit = nil
        if process.isRunning { process.terminate() }
        try? logHandle?.close()
    }
}

/// Führt stopguard mit Argumenten aus und wartet auf das Ende (für das Anlegen des Demo-Depots).
func runQuietly(_ exe: URL, _ args: [String]) -> Int32 {
    let p = Process()
    p.executableURL = exe
    p.arguments = args
    var env = ProcessInfo.processInfo.environment
    env["PATH"] = Paths.searchPath
    p.environment = env
    p.standardOutput = FileHandle.nullDevice
    p.standardError = FileHandle.nullDevice
    do { try p.run() } catch { return -1 }
    p.waitUntilExit()
    return p.terminationStatus
}

/// Meldet sich mit dem Zugangsschlüssel an und liefert das Sitzungs-Cookie.
func login(base: URL, token: String, session: URLSession = URLSession(configuration: .ephemeral),
           completion: @escaping (Result<[HTTPCookie], Error>) -> Void) {
    var request = URLRequest(url: base.appendingPathComponent("api/v1/session"))
    request.httpMethod = "POST"
    request.setValue("application/json", forHTTPHeaderField: "Content-Type")
    request.setValue("1", forHTTPHeaderField: "X-Stopguard")
    request.httpBody = try? JSONSerialization.data(withJSONObject: ["token": token])
    session.dataTask(with: request) { _, response, error in
        if let error { completion(.failure(error)); return }
        guard let http = response as? HTTPURLResponse, http.statusCode == 200 else {
            let code = (response as? HTTPURLResponse)?.statusCode ?? -1
            completion(.failure(NSError(domain: "stopguard", code: code, userInfo: [NSLocalizedDescriptionKey: "Anmeldung fehlgeschlagen (HTTP \(code))"])))
            return
        }
        let header = http.value(forHTTPHeaderField: "Set-Cookie") ?? ""
        completion(.success(HTTPCookie.cookies(withResponseHeaderFields: ["Set-Cookie": header], for: base)))
    }.resume()
}

// MARK: - App

final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate, WKNavigationDelegate, WKUIDelegate, WKDownloadDelegate, WKScriptMessageHandler {
    private var window: NSWindow!
    private var webView: WKWebView!
    private var server: ServerProcess?
    private var baseURL: URL?
    private var configPath: String?
    private var executable: URL?
    private var retriedLogin = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        buildMenu()
        buildWindow()
        showMessage("stopguard startet …")
        NSApp.activate(ignoringOtherApps: true)
        guard let exe = Paths.stopguardExecutable() else {
            fatalAlert(
                "stopguard ist nicht installiert",
                "Führe im Terminal im stopguard-Ordner aus:\n\n./deploy/macos/build-app.sh\n\nDas installiert stopguard (Python ≥ 3.11) und baut diese App."
            )
            return
        }
        executable = exe
        if Paths.defaultConfigExists() {
            startServer(config: nil)
        } else {
            firstRun()
        }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    func applicationWillTerminate(_ notification: Notification) {
        server?.stop()
    }

    // MARK: Fenster

    private func buildWindow() {
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .default()
        config.userContentController.add(self, name: "stopguard")
        webView = WKWebView(frame: .zero, configuration: config)
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.allowsMagnification = true

        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1280, height: 860),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.title = "stopguard"
        window.minSize = NSSize(width: 760, height: 520)
        window.tabbingMode = .disallowed
        window.contentView = webView
        window.delegate = self
        window.center()
        window.setFrameAutosaveName("stopguard.main")
        window.makeKeyAndOrderFront(nil)
    }

    private func showMessage(_ text: String, detail: String = "") {
        let html = """
        <!doctype html><meta charset="utf-8"><style>
        :root { color-scheme: light dark; }
        body { margin: 0; height: 100vh; display: grid; place-items: center; font: 15px -apple-system, system-ui, sans-serif; background: #f0eee6; color: #191817; }
        @media (prefers-color-scheme: dark) { body { background: #1c1b18; color: #eeece4; } }
        div { text-align: center; } p { opacity: .7; font-size: 13px; max-width: 46ch; }
        .spin { width: 22px; height: 22px; margin: 0 auto 14px; border: 2px solid currentColor; border-right-color: transparent; border-radius: 50%; animation: s 1s linear infinite; opacity: .6; }
        @keyframes s { to { transform: rotate(360deg); } }
        </style><div><div class="spin"></div><b>\(text)</b><p>\(detail)</p></div>
        """
        webView.loadHTMLString(html, baseURL: nil)
    }

    // MARK: Erster Start

    private func firstRun() {
        let alert = NSAlert()
        alert.messageText = "stopguard ist noch nicht eingerichtet"
        alert.informativeText = "Es gibt noch keine Konfiguration (~/.config/stopguard/config.toml).\n\nProbiere stopguard zuerst mit einem simulierten Demo-Depot aus – es werden keine echten Orders erzeugt – oder richte es im Terminal ein (Konfiguration anlegen, bei Scalable anmelden)."
        alert.addButton(withTitle: "Demo ansehen")
        alert.addButton(withTitle: "Im Terminal einrichten")
        alert.addButton(withTitle: "Beenden")
        alert.beginSheetModal(for: window) { [weak self] response in
            guard let self else { return }
            switch response {
            case .alertFirstButtonReturn: self.startDemo()
            case .alertSecondButtonReturn:
                self.runInTerminal(name: "einrichten", commands: ["\"$SG\" init", "\"$SG\" login", "echo", "echo 'Fertig. Öffne stopguard jetzt erneut.'"])
                NSApp.terminate(nil)
            default: NSApp.terminate(nil)
            }
        }
    }

    private func startDemo() {
        guard let exe = executable else { return }
        let config = Paths.demoDir.appendingPathComponent("stopguard.toml").path
        showMessage("Demo-Depot wird vorbereitet …", detail: "Ein simuliertes Depot mit Beispieldaten – keine Verbindung zu Scalable.")
        window.title = "stopguard – Demo"
        DispatchQueue.global(qos: .userInitiated).async {
            let ok = FileManager.default.fileExists(atPath: config)
                || (runQuietly(exe, ["demo", "init", Paths.demoDir.path]) == 0
                    && runQuietly(exe, ["-c", config, "portfolio", "sync"]) == 0)
            DispatchQueue.main.async {
                if ok { self.startServer(config: config) } else { self.fatalAlert("Demo konnte nicht angelegt werden", "Details stehen im Log: \(Paths.log.path)") }
            }
        }
    }

    // MARK: Server

    private func startServer(config: String?) {
        guard let exe = executable else { return }
        configPath = config
        retriedLogin = false
        let srv = ServerProcess(executable: exe, config: config)
        srv.onURL = { [weak self] url in self?.serverReady(url) }
        srv.onExit = { [weak self] status, output in self?.serverExited(status, output) }
        do {
            try srv.start()
            server = srv
        } catch {
            fatalAlert("stopguard konnte nicht gestartet werden", error.localizedDescription)
        }
    }

    private func serverReady(_ url: URL) {
        guard let srv = server else { return }
        baseURL = url
        login(base: url, token: srv.token) { [weak self] result in
            DispatchQueue.main.async {
                guard let self else { return }
                switch result {
                case .success(let cookies):
                    self.setCookies(cookies) { self.webView.load(URLRequest(url: url)) }
                case .failure:
                    // Fallback: die Seite meldet sich selbst an (siehe userContentController)
                    self.webView.load(URLRequest(url: url))
                }
            }
        }
    }

    private func setCookies(_ cookies: [HTTPCookie], then done: @escaping () -> Void) {
        let store = webView.configuration.websiteDataStore.httpCookieStore
        let group = DispatchGroup()
        for cookie in cookies {
            group.enter()
            store.setCookie(cookie) { group.leave() }
        }
        group.notify(queue: .main, execute: done)
    }

    private func serverExited(_ status: Int32, _ output: String) {
        server = nil
        let tail = output.split(separator: "\n").suffix(12).joined(separator: "\n")
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = baseURL == nil ? "stopguard konnte nicht starten" : "stopguard wurde unerwartet beendet"
        alert.informativeText = (tail.isEmpty ? "Exit-Code \(status)." : tail) + "\n\nVollständiges Log: \(Paths.log.path)"
        alert.addButton(withTitle: "Neu starten")
        alert.addButton(withTitle: "Log öffnen")
        alert.addButton(withTitle: "Beenden")
        baseURL = nil
        showMessage("stopguard ist nicht aktiv")
        alert.beginSheetModal(for: window) { [weak self] response in
            guard let self else { return }
            switch response {
            case .alertFirstButtonReturn: self.startServer(config: self.configPath)
            case .alertSecondButtonReturn: NSWorkspace.shared.open(Paths.log); NSApp.terminate(nil)
            default: NSApp.terminate(nil)
            }
        }
    }

    private func fatalAlert(_ title: String, _ text: String) {
        let alert = NSAlert()
        alert.alertStyle = .critical
        alert.messageText = title
        alert.informativeText = text
        alert.addButton(withTitle: "Beenden")
        alert.beginSheetModal(for: window) { _ in NSApp.terminate(nil) }
    }

    // MARK: Nachrichten der Seite

    func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {
        guard let body = message.body as? String else { return }
        if body == "login_required", let srv = server, !retriedLogin {
            // Cookie wurde nicht übernommen: Anmeldung direkt in der Seite, danach neu laden.
            retriedLogin = true
            let js = """
            const r = await fetch("api/v1/session", { method: "POST", headers: { "Content-Type": "application/json", "X-Stopguard": "1" }, body: JSON.stringify({ token }) });
            return r.status;
            """
            webView.callAsyncJavaScript(js, arguments: ["token": srv.token], in: nil, in: .page) { [weak self] result in
                if case .success(let status) = result, (status as? Int) == 200 || (status as? NSNumber)?.intValue == 200 {
                    self?.webView.reload()
                }
            }
        }
    }

    // MARK: Navigation, Downloads, Dialoge

    private func isInternal(_ url: URL) -> Bool {
        if ["about", "blob", "data"].contains(url.scheme ?? "") { return true }
        guard let base = baseURL else { return false }
        return url.scheme == base.scheme && url.host == base.host && url.port == base.port
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if navigationAction.shouldPerformDownload {
            decisionHandler(.download)
            return
        }
        if let url = navigationAction.request.url, !isInternal(url) {
            NSWorkspace.shared.open(url)  // externe Links im Standardbrowser
            decisionHandler(.cancel)
            return
        }
        decisionHandler(.allow)
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationResponse: WKNavigationResponse, decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
        if let http = navigationResponse.response as? HTTPURLResponse,
           let disposition = http.value(forHTTPHeaderField: "Content-Disposition"),
           disposition.lowercased().hasPrefix("attachment") {
            decisionHandler(.download)
            return
        }
        decisionHandler(navigationResponse.canShowMIMEType ? .allow : .download)
    }

    func webView(_ webView: WKWebView, navigationAction: WKNavigationAction, didBecome download: WKDownload) {
        download.delegate = self
    }

    func webView(_ webView: WKWebView, navigationResponse: WKNavigationResponse, didBecome download: WKDownload) {
        download.delegate = self
    }

    func download(_ download: WKDownload, decideDestinationUsing response: URLResponse, suggestedFilename: String, completionHandler: @escaping (URL?) -> Void) {
        let panel = NSSavePanel()
        panel.nameFieldStringValue = suggestedFilename
        panel.canCreateDirectories = true
        panel.beginSheetModal(for: window) { result in
            completionHandler(result == .OK ? panel.url : nil)
        }
    }

    func downloadDidFinish(_ download: WKDownload) {}

    func download(_ download: WKDownload, didFailWithError error: Error, resumeData: Data?) {
        let alert = NSAlert(error: error)
        alert.beginSheetModal(for: window, completionHandler: nil)
    }

    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration, for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = navigationAction.request.url {
            if isInternal(url) { webView.load(navigationAction.request) } else { NSWorkspace.shared.open(url) }
        }
        return nil
    }

    func webView(_ webView: WKWebView, runOpenPanelWith parameters: WKOpenPanelParameters, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping ([URL]?) -> Void) {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = parameters.allowsMultipleSelection
        panel.beginSheetModal(for: window) { result in
            completionHandler(result == .OK ? panel.urls : nil)
        }
    }

    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let alert = NSAlert()
        alert.messageText = message
        alert.beginSheetModal(for: window) { _ in completionHandler() }
    }

    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let alert = NSAlert()
        alert.messageText = message
        alert.addButton(withTitle: "OK")
        alert.addButton(withTitle: "Abbrechen")
        alert.beginSheetModal(for: window) { completionHandler($0 == .alertFirstButtonReturn) }
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        webView.reload()
    }

    // MARK: Menü

    private func buildMenu() {
        let main = NSMenu()

        let appMenu = submenu(main, "stopguard")
        add(appMenu, "Über stopguard", #selector(NSApplication.orderFrontStandardAboutPanel(_:)), "", target: NSApp)
        appMenu.addItem(.separator())
        add(appMenu, "Einstellungen …", #selector(openSettings), ",")
        appMenu.addItem(.separator())
        add(appMenu, "stopguard ausblenden", #selector(NSApplication.hide(_:)), "h", target: NSApp)
        add(appMenu, "Andere ausblenden", #selector(NSApplication.hideOtherApplications(_:)), "h", mods: [.command, .option], target: NSApp)
        add(appMenu, "Alle einblenden", #selector(NSApplication.unhideAllApplications(_:)), "", target: NSApp)
        appMenu.addItem(.separator())
        add(appMenu, "stopguard beenden", #selector(NSApplication.terminate(_:)), "q", target: NSApp)

        let edit = submenu(main, "Bearbeiten")
        add(edit, "Widerrufen", Selector(("undo:")), "z", target: nil)
        add(edit, "Wiederholen", Selector(("redo:")), "z", mods: [.command, .shift], target: nil)
        edit.addItem(.separator())
        add(edit, "Ausschneiden", #selector(NSText.cut(_:)), "x", target: nil)
        add(edit, "Kopieren", #selector(NSText.copy(_:)), "c", target: nil)
        add(edit, "Einsetzen", #selector(NSText.paste(_:)), "v", target: nil)
        add(edit, "Alles auswählen", #selector(NSText.selectAll(_:)), "a", target: nil)

        let view = submenu(main, "Ansicht")
        add(view, "Neu laden", #selector(reloadPage), "r")
        view.addItem(.separator())
        add(view, "Originalgröße", #selector(zoomReset), "0")
        add(view, "Vergrößern", #selector(zoomIn), "+")
        add(view, "Verkleinern", #selector(zoomOut), "-")
        view.addItem(.separator())
        add(view, "Beträge ein-/ausblenden", #selector(togglePrivacy), "b", mods: [.command, .shift])

        let go = submenu(main, "Gehe zu")
        let pages: [(String, String)] = [
            ("Übersicht", "#/"), ("Positionen", "#/positionen"), ("Trades", "#/trades"),
            ("Stops", "#/stops"), ("Auswertung", "#/auswertung"), ("Watchlist", "#/watchlist"),
        ]
        for (index, page) in pages.enumerated() {
            let item = add(go, page.0, #selector(navigate(_:)), String(index + 1))
            item.representedObject = page.1
        }
        go.addItem(.separator())
        let data = add(go, "Daten & Einstellungen", #selector(navigate(_:)), "")
        data.representedObject = "#/daten"

        let depot = submenu(main, "Depot")
        add(depot, "Bei Scalable anmelden …", #selector(scalableLogin), "")
        add(depot, "Stop-Anpassungen im Terminal bestätigen …", #selector(applyInTerminal), "")
        depot.addItem(.separator())
        add(depot, "Log anzeigen", #selector(showLog), "")

        let windowMenu = submenu(main, "Fenster")
        add(windowMenu, "Im Dock ablegen", #selector(NSWindow.performMiniaturize(_:)), "m", target: nil)
        add(windowMenu, "Zoomen", #selector(NSWindow.performZoom(_:)), "", target: nil)
        NSApp.windowsMenu = windowMenu

        NSApp.mainMenu = main
    }

    private func submenu(_ main: NSMenu, _ title: String) -> NSMenu {
        let item = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        let menu = NSMenu(title: title)
        item.submenu = menu
        main.addItem(item)
        return menu
    }

    @discardableResult
    private func add(_ menu: NSMenu, _ title: String, _ action: Selector?, _ key: String,
                     mods: NSEvent.ModifierFlags = [.command], target: AnyObject? = nil) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: action, keyEquivalent: key)
        item.keyEquivalentModifierMask = mods
        if let target {
            item.target = target
        } else if let action, responds(to: action) {
            item.target = self  // eigene Aktionen; sonst nil = Responder-Kette (Bearbeiten, Fenster)
        }
        menu.addItem(item)
        return item
    }

    private func js(_ code: String) {
        guard baseURL != nil else { return }
        webView.evaluateJavaScript(code, completionHandler: nil)
    }

    @objc private func navigate(_ sender: NSMenuItem) {
        guard let hash = sender.representedObject as? String else { return }
        js("location.hash = \(String(reflecting: hash))")
    }

    @objc private func openSettings() { js("location.hash = '#/daten'") }
    @objc private func togglePrivacy() { js("document.getElementById('privacy-toggle')?.click()") }
    @objc private func reloadPage() { if baseURL != nil { webView.reload() } }
    @objc private func zoomIn() { webView.pageZoom = min(2.0, webView.pageZoom + 0.1) }
    @objc private func zoomOut() { webView.pageZoom = max(0.6, webView.pageZoom - 0.1) }
    @objc private func zoomReset() { webView.pageZoom = 1.0 }
    @objc private func showLog() { NSWorkspace.shared.open(Paths.log) }
    @objc private func scalableLogin() { runInTerminal(name: "login", commands: ["\"$SG\" login"]) }
    @objc private func applyInTerminal() {
        let config = configPath.map { "-c \(String(reflecting: $0)) " } ?? ""
        runInTerminal(name: "apply", commands: ["\"$SG\" \(config)apply"])
    }

    /// Öffnet ein Terminal-Fenster mit einem kurzen Skript (.command) – ohne Apple-Events-Berechtigung.
    private func runInTerminal(name: String, commands: [String]) {
        guard let exe = executable else { return }
        let script = Paths.support.appendingPathComponent("\(name).command")
        let body = """
        #!/bin/zsh
        export PATH="\(Paths.searchPath):$PATH"
        SG=\(String(reflecting: exe.path))
        clear
        \(commands.joined(separator: "\n"))
        echo
        read -k 1 "?Fertig – beliebige Taste zum Schließen …"
        """
        do {
            try FileManager.default.createDirectory(at: Paths.support, withIntermediateDirectories: true)
            try body.write(to: script, atomically: true, encoding: .utf8)
            try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: script.path)
            NSWorkspace.shared.open(script)
        } catch {
            NSAlert(error: error).runModal()
        }
    }
}

// MARK: - Selbsttest (für CI): Server starten, anmelden, API abfragen – ohne Fenster

func runSelfTest() -> Never {
    let args = CommandLine.arguments
    let config = args.firstIndex(of: "--config").flatMap { args.indices.contains($0 + 1) ? args[$0 + 1] : nil }
    guard let exe = Paths.stopguardExecutable() else {
        print("SELFTEST FAIL: stopguard nicht gefunden (STOPGUARD_BIN setzen)")
        exit(3)
    }
    print("SELFTEST stopguard: \(exe.path)")
    let server = ServerProcess(executable: exe, config: config)
    let session = URLSession(configuration: .ephemeral)
    server.onExit = { status, output in
        print("SELFTEST FAIL: Server beendet (\(status))\n\(output)")
        exit(4)
    }
    server.onURL = { url in
        print("SELFTEST URL: \(url)")
        login(base: url, token: server.token, session: session) { result in
            guard case .success(let cookies) = result, !cookies.isEmpty else {
                print("SELFTEST FAIL: Anmeldung \(result)")
                exit(5)
            }
            print("SELFTEST Cookie: \(cookies.map(\.name).joined(separator: ","))")
            session.dataTask(with: url.appendingPathComponent("api/v1/app")) { data, response, _ in
                let code = (response as? HTTPURLResponse)?.statusCode ?? -1
                let text = data.map { String(decoding: $0, as: UTF8.self) } ?? ""
                print("SELFTEST GET /api/v1/app → \(code)")
                DispatchQueue.main.async {
                    server.stop()
                    if code == 200 && text.contains("\"version\"") {
                        print("SELFTEST OK")
                        exit(0)
                    }
                    print("SELFTEST FAIL: \(text.prefix(300))")
                    exit(6)
                }
            }.resume()
        }
    }
    do { try server.start() } catch {
        print("SELFTEST FAIL: \(error)")
        exit(7)
    }
    DispatchQueue.main.asyncAfter(deadline: .now() + 90) {
        print("SELFTEST FAIL: Zeitüberschreitung\n\(server.output)")
        server.stop()
        exit(8)
    }
    dispatchMain()
}

// MARK: - Start

if CommandLine.arguments.contains("--self-test") {
    runSelfTest()
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
