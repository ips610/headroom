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
// browser that is already waiting. Nothing runs until then. If another program already has 8460,
// headroom takes the next free port instead and stays on it (see AppPort).
//
// Plain headroom.localhost (port 80) works too: a second agent holds port 80 and starts this
// program with --door, which only redirects by name (headroom.localhost and harbor.localhost, to
// the ports the two apps are on) and exits when idle. harbor carries a door too, and when the
// door is harbor's it stays harbor's: that one also opens harbor's projects by name
// (<name>.localhost).
//
// Reminders: an agent the server sets up (local.headroom.watch) starts this program with --watch
// every half hour. It asks the server program what is worth saying (space running low, or what Safe
// clean would free once a week), posts that as a notification, and exits. No Dock icon. Nothing
// is deleted here, and the server program deletes nothing in this check either unless "Run Safe
// clean by itself once a week" was switched on. Clicking a notification opens headroom's page.
import AppKit
import UserNotifications

let label = "local.headroom.door"

// The ports the two apps answer on. Each has a usual one (headroom 8460, harbor 8470) and may move
// up to 19 past it. launchd doesn't refuse an agent whose port another program already holds on
// 127.0.0.1 or ::1: it loads the agent and hands over a socket that isn't bound, and that address
// keeps going to the other program. So when the port is taken, the app moves to the next free one
// and stays there, and writes it to a small file. The port-80 door (this one, or harbor's) reads
// the two files, so the names land on the apps wherever they are. harbor does the same.
struct AppPort {
    let usual: Int
    let file: String
    static let headroom = AppPort(usual: 8460, file: NSHomeDirectory() + "/Library/Application Support/headroom/port")
    static let harbor = AppPort(usual: 8470, file: NSHomeDirectory() + "/Library/Application Support/local.harbor/port")
    var range: ClosedRange<Int> { usual...(usual + 19) }

    // What the file says: plain digits, inside the app's range. No file, or anything else, means
    // the usual port.
    var reserved: Int {
        guard let text = try? String(contentsOfFile: file, encoding: .utf8) else { return usual }
        let digits = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard (1...5).contains(digits.utf8.count), digits.utf8.allSatisfy({ (48...57).contains($0) }),
              let port = Int(digits), range.contains(port) else { return usual }
        return port
    }

    // Plain digits, 0600, put in place in one step. Nothing is written while the file (or its
    // absence) already says so.
    func record(_ port: Int) {
        guard range.contains(port), reserved != port else { return }
        try? FileManager.default.createDirectory(atPath: (file as NSString).deletingLastPathComponent,
                                                 withIntermediateDirectories: true)
        let new = file + ".new"
        unlink(new)
        let fd = open(new, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0o600)
        guard fd >= 0 else { return }
        let digits = Array(String(port).utf8)
        let written = write(fd, digits, digits.count) == digits.count
        close(fd)
        if written { rename(new, file) } else { unlink(new) }
    }

    // Binds a loopback address on `port`, or (answers) connects to whatever listens there.
    private static func loopback(_ port: Int, v6: Bool, answers: Bool) -> Bool {
        let fd = socket(v6 ? AF_INET6 : AF_INET, SOCK_STREAM, 0)
        guard fd >= 0, let number = UInt16(exactly: port) else { return false }
        defer { close(fd) }
        var one: Int32 = 1
        setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, socklen_t(MemoryLayout<Int32>.size))
        if answers { _ = fcntl(fd, F_SETFL, fcntl(fd, F_GETFL) | O_NONBLOCK) }
        func reach(_ address: UnsafePointer<sockaddr>, _ size: Int) -> Bool {
            if !answers { return Darwin.bind(fd, address, socklen_t(size)) == 0 }
            if Darwin.connect(fd, address, socklen_t(size)) == 0 { return true }
            // Not at once: a second for it, so a stuck listener can't hold the launch up.
            var waiting = pollfd(fd: fd, events: Int16(POLLOUT), revents: 0)
            var failure: Int32 = 0
            var length = socklen_t(MemoryLayout<Int32>.size)
            return errno == EINPROGRESS && poll(&waiting, 1, 1000) > 0
                && getsockopt(fd, SOL_SOCKET, SO_ERROR, &failure, &length) == 0 && failure == 0
        }
        if v6 {
            setsockopt(fd, IPPROTO_IPV6, IPV6_V6ONLY, &one, socklen_t(MemoryLayout<Int32>.size))
            var address = sockaddr_in6()
            address.sin6_len = UInt8(MemoryLayout<sockaddr_in6>.size)
            address.sin6_family = sa_family_t(AF_INET6)
            address.sin6_port = number.bigEndian
            address.sin6_addr = in6addr_loopback
            return withUnsafePointer(to: &address) {
                $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { reach($0, MemoryLayout<sockaddr_in6>.size) }
            }
        }
        var address = sockaddr_in()
        address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        address.sin_family = sa_family_t(AF_INET)
        address.sin_port = number.bigEndian
        address.sin_addr.s_addr = INADDR_LOOPBACK.bigEndian
        return withUnsafePointer(to: &address) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { reach($0, MemoryLayout<sockaddr_in>.size) }
        }
    }

    // Can 127.0.0.1 and ::1 both be bound on this port right now? A program listening on all
    // interfaces doesn't count: launchd binds the two loopback addresses beside it.
    static func free(_ port: Int) -> Bool {
        loopback(port, v6: false, answers: false) && loopback(port, v6: true, answers: false)
    }

    // Does another listener really hold it? Not being able to bind it isn't enough: for half a
    // minute after an agent is unloaded, the connections it just closed keep the port from being
    // bound here, while launchd can bind it again. Taken means it can't be bound and something
    // answers on it.
    static func taken(_ port: Int) -> Bool {
        !free(port) && (loopback(port, v6: false, answers: true) || loopback(port, v6: true, answers: true))
    }
}

@_silgen_name("launch_activate_socket")
func launch_activate_socket(_ name: UnsafePointer<CChar>,
                            _ fds: UnsafeMutablePointer<UnsafeMutablePointer<Int32>?>,
                            _ count: UnsafeMutablePointer<Int>) -> Int32

final class Shell: NSObject, NSApplicationDelegate, UNUserNotificationCenterDelegate {
    private var server: pid_t = 0
    private var exitWatch: DispatchSourceProcess?
    private var reader: FileHandle?     // kept, or the pipe closes and the server's first line fails
    private var page: URL?

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.mainMenu = mainMenu()
        let center = UNUserNotificationCenter.current()
        center.delegate = self
        // macOS asks only the first time; after that this just reads the answer.
        center.requestAuthorization(options: [.alert, .sound]) { _, _ in }
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
        if let page = page ?? URL(string: "http://headroom.localhost:\(AppPort.headroom.reserved)/") {
            NSWorkspace.shared.open(page)
        }
    }

    // A reminder was clicked: show the page. (If headroom wasn't running, opening it already does.)
    func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse,
                                withCompletionHandler done: @escaping () -> Void) {
        openPage()
        done()
    }

    private func start(sockets: [Int32], openTab: Bool) -> Bool {
        guard let (program, arguments) = Shell.serverCommand() else { return false }
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
                // launchd had no usable socket for us (another program holds the port), so the
                // server found its own: that is headroom's port from now on. Only the copy the
                // agent starts says so; one run from a build folder leaves the file alone.
                if sockets.isEmpty, CommandLine.arguments.contains("--launchd"), let moved = address?.port {
                    AppPort.headroom.record(moved)
                }
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
    static func serverCommand() -> (String, [String])? {
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

// The LaunchAgent that keeps headroom.localhost:8460 (or the port headroom moved to) answerable
// while headroom is closed.
enum Door {
    static let plist = NSHomeDirectory() + "/Library/LaunchAgents/\(label).plist"
    static let request = NSHomeDirectory() + "/Library/Application Support/headroom/open-page"
    static var domain: String { "gui/\(getuid())" }

    // Writes and loads the agent for wherever this app now lives, but only for a copy installed
    // in Applications. A copy run from Downloads, a build folder, a disk image or App
    // Translocation must not point the agents at a path that won't last.
    //
    // Installed means the app itself sits in an Applications folder, not somewhere below one.
    // "Update now" sets the old app aside in a folder beside the new one (/Applications/
    // .headroom-update-…/previous/headroom.app) when it can't be moved to the Trash: opened from
    // there, it must not point the agents at itself.
    static func installed(_ program: String) -> Bool {
        let real = (program as NSString).resolvingSymlinksInPath
        // …/headroom.app/Contents/MacOS/headroom-app: three up is the app, one more the folder it is in.
        var folder = real as NSString
        for _ in 0..<4 { folder = folder.deletingLastPathComponent as NSString }
        let homes = (NSHomeDirectory() + "/Applications" as NSString).resolvingSymlinksInPath
        return (folder as String == "/Applications" || folder as String == homes) && !real.contains("/AppTranslocation/")
    }

    static func install() -> Bool {
        let program = (Bundle.main.executablePath ?? CommandLine.arguments[0] as NSString as String)
        if !installed(program) {
            return false
        }
        PortDoor.install(program: program)
        func agent(_ port: Int) -> [String: Any] {
            [
                "Label": label,
                "ProgramArguments": [program, "--launchd"],
                "ProcessType": "Interactive",
                "Sockets": [
                    "v4": ["SockNodeName": "127.0.0.1", "SockServiceName": String(port), "SockFamily": "IPv4"],
                    "v6": ["SockNodeName": "::1", "SockServiceName": String(port), "SockFamily": "IPv6"],
                ],
            ]
        }
        let reserved = AppPort.headroom.reserved
        let current = NSDictionary(contentsOfFile: plist) as? [String: Any]
        let loaded = launchctl(["print", "\(domain)/\(label)"])
        // Loaded with this port: launchd holds it for headroom already.
        if loaded, let current, NSDictionary(dictionary: current).isEqual(to: agent(reserved)) {
            return true
        }
        if loaded {
            _ = launchctl(["bootout", "\(domain)/\(label)"])
        }
        // Whenever the agent has to be written, the port is checked first. If something else
        // answers on it, wait a few seconds (it may only be launchd letting go of ours after the
        // bootout), then move to the next free port in the range, and stay on that one.
        var port = reserved
        var waited = 0
        while AppPort.taken(port), waited < 15 {
            usleep(200_000)
            waited += 1
        }
        if AppPort.taken(port), let next = AppPort.headroom.range.first(where: AppPort.free) {
            port = next
        }
        AppPort.headroom.record(port)
        try? FileManager.default.createDirectory(atPath: (plist as NSString).deletingLastPathComponent,
                                                 withIntermediateDirectories: true)
        guard (agent(port) as NSDictionary).write(toFile: plist, atomically: true) else { return false }
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
    // One whose address another program already held comes back unbound (port 0): it is left out.
    static func sockets() -> [Int32] {
        var all: [Int32] = []
        for name in ["v4", "v6"] {
            var fds: UnsafeMutablePointer<Int32>?
            var count = 0
            guard launch_activate_socket(name, &fds, &count) == 0, let fds else { continue }
            for index in 0..<count {
                var address = sockaddr_storage()
                var length = socklen_t(MemoryLayout<sockaddr_storage>.size)
                // sin_port and sin6_port sit at the same offset.
                let bound = withUnsafeMutablePointer(to: &address) { pointer in
                    pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) { getsockname(fds[index], $0, &length) } == 0
                        && pointer.withMemoryRebound(to: sockaddr_in.self, capacity: 1) { $0.pointee.sin_port } != 0
                }
                let moved = bound ? fcntl(fds[index], F_DUPFD_CLOEXEC, 10) : -1
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
    // Where the two apps are right now: read from their port files on every request.
    static var routes: [String: Int] {
        ["headroom.localhost": AppPort.headroom.reserved, "harbor.localhost": AppPort.harbor.reserved]
    }
    static var keep: [DispatchSourceProtocol] = []

    // Loads the shared agent unless a working copy is already there (harbor may have set it up).
    // A door of harbor's is never taken over: harbor's also carries its projects' names
    // (<name>.localhost), which this one can't. If harbor's isn't loaded, it is loaded as it is.
    static func install(program: String) {
        let plist = NSHomeDirectory() + "/Library/LaunchAgents/\(label).plist"
        let owner = (NSDictionary(contentsOfFile: plist)?["ProgramArguments"] as? [String])?.first
        let works = owner.map { FileManager.default.isExecutableFile(atPath: $0) } ?? false
        if works, Door.launchctl(["print", "\(Door.domain)/\(label)"]) {
            return
        }
        if works, owner?.hasSuffix("/Contents/MacOS/Harbor") == true,
           Door.launchctl(["bootstrap", Door.domain, plist]) || Door.launchctl(["print", "\(Door.domain)/\(label)"]) {
            return      // loaded now, by this or by harbor itself in the meantime
        }
        // No door at all would be worse than headroom's: if harbor's can't be loaded, this one goes in.
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

// The reminder agent's half-hourly check.
enum Watch {
    static func run() -> Never {
        guard let (program, arguments) = Shell.serverCommand() else { exit(1) }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: program)
        process.arguments = arguments + ["--watch"]
        let output = Pipe()
        process.standardOutput = output
        process.standardError = FileHandle.nullDevice
        guard (try? process.run()) != nil else { exit(1) }
        let said = output.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        let center = UNUserNotificationCenter.current()
        let posted = DispatchGroup()
        // Each line the server prints is "notify<TAB>title<TAB>text".
        for line in String(decoding: said, as: UTF8.self).split(separator: "\n") {
            let parts = line.split(separator: "\t", omittingEmptySubsequences: false)
            guard parts.count == 3, parts[0] == "notify" else { continue }
            let content = UNMutableNotificationContent()
            content.title = String(parts[1])
            content.body = String(parts[2])
            posted.enter()
            center.add(UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil)) { _ in
                posted.leave()
            }
        }
        _ = posted.wait(timeout: .now() + 15)
        exit(0)
    }
}

if CommandLine.arguments.contains("--watch") {
    Watch.run()           // no Dock icon: check, maybe notify, exit
}
if CommandLine.arguments.contains("--door") {
    PortDoor.serve()      // no Dock icon, no app: just the redirect
}

let app = NSApplication.shared
let shell = Shell()
app.delegate = shell
app.setActivationPolicy(.regular)
app.run()
