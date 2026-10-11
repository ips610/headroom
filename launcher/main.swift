// headroom's app shell: a Dock icon, and a way to quit, for a cleaner that lives in a browser tab.
//
// It starts the server (cleaner.py with Python, or the frozen server inside a packaged app),
// reads the page's address from the server's first line, and opens that page again whenever the
// Dock icon is clicked. Quitting the app stops the server; the server stopping on its own (the
// tab was closed) quits the app, so the Dock icon is there exactly while headroom is running.
import AppKit

final class Shell: NSObject, NSApplicationDelegate {
    private var server: Process?
    private var page: URL?

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.mainMenu = mainMenu()
        guard let (program, arguments) = serverCommand() else {
            return fail("headroom needs Python 3. Install it with: brew install python")
        }
        let process = Process()
        process.executableURL = program
        process.arguments = arguments
        let output = Pipe()
        process.standardOutput = output
        process.standardError = log()
        output.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty, let text = String(data: data, encoding: .utf8) else {
                handle.readabilityHandler = nil
                return
            }
            // The server's first line is "headroom is open at <address>".
            if let found = text.range(of: #"(?<=open at )http://\S+"#, options: .regularExpression) {
                let address = URL(string: String(text[found]))
                DispatchQueue.main.async { self?.page = address }
            }
        }
        process.terminationHandler = { _ in
            DispatchQueue.main.async { NSApp.terminate(nil) }
        }
        do {
            try process.run()
            server = process
        } catch {
            fail("headroom could not start: \(error.localizedDescription)")
        }
    }

    // Clicking the Dock icon, or opening the app again from Finder, brings the page back.
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        openPage()
        return false
    }

    func applicationDockMenu(_ sender: NSApplication) -> NSMenu? {
        let menu = NSMenu()
        menu.addItem(NSMenuItem(title: "Open headroom", action: #selector(openPage), keyEquivalent: ""))
        return menu
    }

    func applicationWillTerminate(_ notification: Notification) {
        if let server, server.isRunning {
            server.terminate()
        }
    }

    @objc func openPage() {
        if let page {
            NSWorkspace.shared.open(page)
        }
    }

    // The packaged app carries a frozen server beside this program; a source install runs
    // cleaner.py from Resources with the first real Python it finds. /usr/bin/python3 comes last:
    // it is an Xcode shim that can stall for minutes.
    private func serverCommand() -> (URL, [String])? {
        let bundle = Bundle.main
        let frozen = bundle.bundleURL.appendingPathComponent("Contents/MacOS/headroom")
        if FileManager.default.isExecutableFile(atPath: frozen.path) {
            return (frozen, ["--serve"])
        }
        guard let script = bundle.url(forResource: "cleaner", withExtension: "py") else { return nil }
        let pythons = [
            "/opt/homebrew/bin/python3", "/usr/local/bin/python3",
            "/Library/Frameworks/Python.framework/Versions/Current/bin/python3", "/usr/bin/python3",
        ]
        guard let python = pythons.first(where: { FileManager.default.isExecutableFile(atPath: $0) }) else {
            return nil
        }
        return (URL(fileURLWithPath: python), [script.path])
    }

    private func log() -> FileHandle {
        let path = NSHomeDirectory() + "/Library/Logs/headroom.log"
        FileManager.default.createFile(atPath: path, contents: nil)
        return FileHandle(forWritingAtPath: path) ?? FileHandle.nullDevice
    }

    private func mainMenu() -> NSMenu {
        let app = NSMenu()
        app.addItem(NSMenuItem(title: "Open headroom", action: #selector(openPage), keyEquivalent: "o"))
        app.addItem(.separator())
        app.addItem(NSMenuItem(title: "Quit headroom", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q"))
        let item = NSMenuItem()
        item.submenu = app
        let bar = NSMenu()
        bar.addItem(item)
        return bar
    }

    private func fail(_ message: String) {
        let alert = NSAlert()
        alert.messageText = message
        alert.runModal()
        NSApp.terminate(nil)
    }
}

let app = NSApplication.shared
let shell = Shell()
app.delegate = shell
app.setActivationPolicy(.regular)
app.run()
