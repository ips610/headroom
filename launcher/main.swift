// headroom's app shell: a Dock icon, and a way to quit, for a cleaner that lives in a browser tab.
//
// It starts the server (cleaner.py with Python, or the frozen server inside a packaged app),
// reads the page's address from the server's first line, and opens that page again whenever the
// Dock icon is clicked. Quitting the app stops the server; the server stopping on its own (the
// tab was closed) quits the app, so the Dock icon is there exactly while headroom is running.
//
// Typing headroom.localhost:8460 into a browser also opens headroom. A LaunchAgent asks launchd
// to hold that port while headroom is closed; the first connection makes launchd start this
// program with --launchd, and the listening sockets are handed to the server, which answers the
// browser that is already waiting. Nothing runs until then.
//
// Plain headroom.localhost (port 80) works too: a second agent holds port 80 and starts this
// program with --door, which only redirects by name (headroom.localhost → :8460, harbor.localhost
// → :8470) and exits when idle. harbor carries the same door, so either app can serve it.
import AppKit

let label = "local.headroom.door"
let port = "8460"

@_silgen_name("launch_activate_socket")
func launch_activate_socket(_ name: UnsafePointer<CChar>,
                            _ fds: UnsafeMutablePointer<UnsafeMutablePointer<Int32>?>,
                            _ count: UnsafeMutablePointer<Int>) -> Int32

final class Shell: NSObject, NSApplicationDelegate {
    private var server: pid_t = 0
    private var exitWatch: DispatchSourceProcess?
    private var reader: FileHandle?     // kept, or the pipe closes and the server's first line fails
    private var page: URL?

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.mainMenu = mainMenu()
        let byLaunchd = CommandLine.arguments.contains("--launchd")
        if !byLaunchd, Door.install(), Door.handOver() {
            // launchd now starts the copy that owns the port; it opens the page.
            return NSApp.terminate(nil)
        }
        let sockets = byLaunchd ? Door.sockets() : []
        // Started by a browser connection: that tab is already waiting, so don't open another.
        let openTab = !byLaunchd || Door.takePageRequest()
        guard start(sockets: sockets, openTab: openTab) else {
            return fail("headroom needs Python 3. Install it with: brew install python")
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
        if server > 0 {
            kill(server, SIGTERM)
        }
    }

    @objc func openPage() {
        if let page {
            NSWorkspace.shared.open(page)
        }
    }

    private func start(sockets: [Int32], openTab: Bool) -> Bool {
        guard let (program, arguments) = serverCommand() else { return false }
        var output: [Int32] = [0, 0]
        guard pipe(&output) == 0 else { return false }
        let log = open(NSHomeDirectory() + "/Library/Logs/headroom.log", O_WRONLY | O_CREAT | O_TRUNC, 0o600)
        var environment = ProcessInfo.processInfo.environment
        if !sockets.isEmpty {
            environment["HEADROOM_LISTEN_FDS"] = sockets.indices.map { String(3 + $0) }.joined(separator: ",")
        }
        let pid = Spawn.run(program, arguments + (openTab ? [] : ["--no-open"]), environment: environment,
                            stdout: output[1], stderr: log >= 0 ? log : 2, extra: sockets)
        close(output[1])
        if log >= 0 { close(log) }
        guard let pid else { return false }
        server = pid
        let reader = FileHandle(fileDescriptor: output[0], closeOnDealloc: true)
        self.reader = reader
        reader.readabilityHandler = { [weak self] handle in
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
        let watch = DispatchSource.makeProcessSource(identifier: pid, eventMask: .exit, queue: .main)
        watch.setEventHandler { [weak self] in
            var status: Int32 = 0
            waitpid(pid, &status, 0)
            self?.server = 0
            NSApp.terminate(nil)
        }
        watch.resume()
        exitWatch = watch
        return true
    }

    // The packaged app carries a frozen server beside this program; a source install runs
    // cleaner.py from Resources with the first real Python it finds. /usr/bin/python3 comes last:
    // it is an Xcode shim that can stall for minutes.
    private func serverCommand() -> (String, [String])? {
        let bundle = Bundle.main
        let frozen = bundle.bundleURL.appendingPathComponent("Contents/MacOS/headroom").path
        if FileManager.default.isExecutableFile(atPath: frozen) {
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
        return (python, [script.path])
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

// The LaunchAgent that keeps headroom.localhost:8460 answerable while headroom is closed.
enum Door {
    static let plist = NSHomeDirectory() + "/Library/LaunchAgents/\(label).plist"
    static let request = NSHomeDirectory() + "/Library/Application Support/headroom/open-page"
    static var domain: String { "gui/\(getuid())" }

    // Writes and loads the agent for wherever this app now lives. Skipped for copies macOS runs
    // from a temporary place (App Translocation, a mounted disk image), whose path will not last.
    static func install() -> Bool {
        let program = Bundle.main.executablePath ?? CommandLine.arguments[0]
        if program.contains("/AppTranslocation/") || program.hasPrefix("/Volumes/") {
            return false
        }
        PortDoor.install(program: program)
        let wanted: [String: Any] = [
            "Label": label,
            "ProgramArguments": [program, "--launchd"],
            "ProcessType": "Interactive",
            "Sockets": [
                "v4": ["SockNodeName": "127.0.0.1", "SockServiceName": port, "SockFamily": "IPv4"],
                "v6": ["SockNodeName": "::1", "SockServiceName": port, "SockFamily": "IPv6"],
            ],
        ]
        let current = NSDictionary(contentsOfFile: plist) as? [String: Any]
        let loaded = launchctl(["print", "\(domain)/\(label)"])
        if loaded, let current, NSDictionary(dictionary: current).isEqual(to: wanted) {
            return true
        }
        if loaded {
            _ = launchctl(["bootout", "\(domain)/\(label)"])
        }
        try? FileManager.default.createDirectory(atPath: (plist as NSString).deletingLastPathComponent,
                                                 withIntermediateDirectories: true)
        guard (wanted as NSDictionary).write(toFile: plist, atomically: true) else { return false }
        return launchctl(["bootstrap", domain, plist])
    }

    // Opened from Finder or Spotlight: ask launchd to start the copy that owns the port, and tell
    // it to open the page.
    static func handOver() -> Bool {
        try? FileManager.default.createDirectory(atPath: (request as NSString).deletingLastPathComponent,
                                                 withIntermediateDirectories: true)
        FileManager.default.createFile(atPath: request, contents: nil)
        if launchctl(["kickstart", "\(domain)/\(label)"]) {
            return true
        }
        try? FileManager.default.removeItem(atPath: request)
        return false
    }

    // True when the copy that opened the app asked for the page in the last half minute.
    static func takePageRequest() -> Bool {
        guard let made = (try? FileManager.default.attributesOfItem(atPath: request))?[.modificationDate] as? Date
        else { return false }
        try? FileManager.default.removeItem(atPath: request)
        return Date().timeIntervalSince(made) < 30
    }

    // The listening sockets launchd holds for us, moved above 10 so they can be placed at 3, 4.
    static func sockets() -> [Int32] {
        var all: [Int32] = []
        for name in ["v4", "v6"] {
            var fds: UnsafeMutablePointer<Int32>?
            var count = 0
            guard launch_activate_socket(name, &fds, &count) == 0, let fds else { continue }
            for index in 0..<count {
                let moved = fcntl(fds[index], F_DUPFD_CLOEXEC, 10)
                if moved >= 0 { all.append(moved) }
                close(fds[index])
            }
            free(fds)
        }
        return all
    }

    static func launchctl(_ arguments: [String]) -> Bool {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/launchctl")
        process.arguments = arguments
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        do {
            try process.run()
            process.waitUntilExit()
            return process.terminationStatus == 0
        } catch {
            return false
        }
    }
}

// The port-80 door: lets plain headroom.localhost and harbor.localhost open their apps.
enum PortDoor {
    static let label = "local.localhost.door"
    static let routes = ["headroom.localhost": 8460, "harbor.localhost": 8470]
    static var keep: [DispatchSourceProtocol] = []

    // Loads the shared agent unless a working copy is already there (harbor may have set it up).
    static func install(program: String) {
        let plist = NSHomeDirectory() + "/Library/LaunchAgents/\(label).plist"
        if Door.launchctl(["print", "\(Door.domain)/\(label)"]),
           let current = NSDictionary(contentsOfFile: plist),
           let owner = (current["ProgramArguments"] as? [String])?.first,
           FileManager.default.isExecutableFile(atPath: owner) {
            return
        }
        _ = Door.launchctl(["bootout", "\(Door.domain)/\(label)"])
        let agent: NSDictionary = [
            "Label": label,
            "ProgramArguments": [program, "--door"],
            "Sockets": [
                "v4": ["SockNodeName": "127.0.0.1", "SockServiceName": "80", "SockFamily": "IPv4"],
                "v6": ["SockNodeName": "::1", "SockServiceName": "80", "SockFamily": "IPv6"],
            ],
        ]
        if agent.write(toFile: plist, atomically: true) {
            _ = Door.launchctl(["bootstrap", Door.domain, plist])
        }
    }

    // Started by launchd on a connection to port 80: answer every waiting browser with a redirect
    // by name, then leave once nobody has knocked for ten seconds.
    static func serve() -> Never {
        let listeners = Door.sockets()
        guard !listeners.isEmpty else { exit(1) }
        var last = Date()
        for listener in listeners {
            let source = DispatchSource.makeReadSource(fileDescriptor: listener, queue: .main)
            source.setEventHandler {
                let client = accept(listener, nil, nil)
                guard client >= 0 else { return }
                last = Date()
                answer(client)
            }
            source.resume()
            keep.append(source)
        }
        let timer = DispatchSource.makeTimerSource(queue: .main)
        timer.schedule(deadline: .now() + 2, repeating: 2)
        timer.setEventHandler {
            if Date().timeIntervalSince(last) > 10 { exit(0) }
        }
        timer.resume()
        keep.append(timer)
        dispatchMain()
    }

    private static func answer(_ client: Int32) {
        var wait = timeval(tv_sec: 2, tv_usec: 0)
        setsockopt(client, SOL_SOCKET, SO_RCVTIMEO, &wait, socklen_t(MemoryLayout<timeval>.size))
        var head = [UInt8]()
        var chunk = [UInt8](repeating: 0, count: 4096)
        while head.count < 16384 {
            let got = read(client, &chunk, chunk.count)
            if got <= 0 { break }
            head += chunk[0..<got]
            if String(decoding: head, as: UTF8.self).contains("\r\n\r\n") { break }
        }
        var host = ""
        for line in String(decoding: head, as: UTF8.self).components(separatedBy: "\r\n")
        where line.lowercased().hasPrefix("host:") {
            host = line.dropFirst(5).trimmingCharacters(in: .whitespaces).lowercased()
            if let colon = host.lastIndex(of: ":"), !host.hasSuffix("]") {
                host = String(host[..<colon])
            }
        }
        let reply: String
        if let port = routes[host] {
            // 302, never 301: browsers keep a 301 forever, and the port may change one day.
            reply = "HTTP/1.1 302 Found\r\nLocation: http://\(host):\(port)/\r\nCache-Control: no-store\r\n"
                + "Content-Length: 0\r\nConnection: close\r\n\r\n"
        } else {
            let body = "Nothing lives here. Try headroom.localhost or harbor.localhost.\n"
            reply = "HTTP/1.1 404 Not Found\r\nContent-Type: text/plain; charset=utf-8\r\nCache-Control: no-store\r\n"
                + "Content-Length: \(body.utf8.count)\r\nConnection: close\r\n\r\n" + body
        }
        _ = reply.withCString { write(client, $0, strlen($0)) }
        close(client)
    }
}

// posix_spawn, so the listening sockets can be handed to the server at fixed numbers (3, 4, …)
// and everything else stays closed.
enum Spawn {
    static func run(_ program: String, _ arguments: [String], environment: [String: String],
                    stdout: Int32, stderr: Int32, extra: [Int32]) -> pid_t? {
        var actions: posix_spawn_file_actions_t?
        posix_spawn_file_actions_init(&actions)
        defer { posix_spawn_file_actions_destroy(&actions) }
        posix_spawn_file_actions_addopen(&actions, 0, "/dev/null", O_RDONLY, 0)
        posix_spawn_file_actions_adddup2(&actions, stdout, 1)
        posix_spawn_file_actions_adddup2(&actions, stderr, 2)
        for (index, fd) in extra.enumerated() {
            posix_spawn_file_actions_adddup2(&actions, fd, Int32(3 + index))
        }
        var attributes: posix_spawnattr_t?
        posix_spawnattr_init(&attributes)
        defer { posix_spawnattr_destroy(&attributes) }
        posix_spawnattr_setflags(&attributes, Int16(POSIX_SPAWN_CLOEXEC_DEFAULT))
        let argv = ([program] + arguments).map { strdup($0) } + [nil]
        let envp = environment.map { strdup("\($0.key)=\($0.value)") } + [nil]
        defer {
            argv.forEach { free($0) }
            envp.forEach { free($0) }
        }
        var pid: pid_t = 0
        return posix_spawn(&pid, program, &actions, &attributes, argv, envp) == 0 ? pid : nil
    }
}

if CommandLine.arguments.contains("--door") {
    PortDoor.serve()      // no Dock icon, no app: just the redirect
}

let app = NSApplication.shared
let shell = Shell()
app.delegate = shell
app.setActivationPolicy(.regular)
app.run()
