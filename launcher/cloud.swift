import Foundation
import SystemConfiguration

// headroom-cloud list <least bytes>
// headroom-cloud evict [--] path…
//
// iCloud Drive keeps a copy on this Mac of every file opened here. This tool finds the copies
// that could go, and removes one when asked: the file itself stays in iCloud and downloads again
// when it is opened. Nothing is ever deleted, and nothing is done with `rm` or `brctl`: the
// questions are asked through URLResourceValues, and the copy is removed with
// FileManager.evictUbiquitousItem(at:), which is how macOS means it to be done.
//
// A file is only named, and only touched, when all of this is true of it: it is a plain file (not
// a folder, not a package, not a link), it is an iCloud item, the copy here is the newest version,
// it has been uploaded, macOS says it is not on its way up or down, it has no conflict waiting to
// be settled and no upload error, its syncing is not paused, and neither its contents nor anything
// else about it changed in the last hour. `evict` asks all of it again for each file, right
// before the call.
//
// The last hour is there because "uploaded" only means iCloud has data for the file, not that it
// has the newest edit: a file changed a moment ago, with syncing behind, would pass without it.
// "macOS says" is meant strictly: where macOS gives no answer, the copy stays.
//
// Where it looks: ~/Library/Mobile Documents/com~apple~CloudDocs (iCloud Drive), and ~/Desktop and
// ~/Documents when macOS says those folders are iCloud items (the "Desktop & Documents Folders"
// setting). `evict` refuses any path that isn't inside one of those, or is reached through a link.
//
// Answers are JSON on standard output:
//   list  -> {"drive": true, "online": true, "account": true, "roots": [...], "files": [{"path", "bytes", "used"}]}
//            ("account" is whether macOS says anyone is signed in to iCloud Drive: only for saying so)
//   evict -> [{"path", "ok", "why", "whys"}]   ("whys" is every reason the copy stays, "why" the first)

let fm = FileManager.default

func real(_ path: String) -> String? {
    guard let resolved = realpath(path, nil) else { return nil }
    defer { free(resolved) }
    return String(cString: resolved)
}

// The home folder as headroom itself takes it: from HOME.
let home = real(ProcessInfo.processInfo.environment["HOME"] ?? NSHomeDirectory()) ?? NSHomeDirectory()
let drive = home + "/Library/Mobile Documents/com~apple~CloudDocs"

// A folder that is itself where it says it is: there, a folder, and no link on the way to it.
func plainFolder(_ path: String) -> Bool {
    var info = stat()
    return lstat(path, &info) == 0 && info.st_mode & S_IFMT == S_IFDIR && real(path) == path
}

// The folders whose files are iCloud's: iCloud Drive, and Desktop and Documents when they are in it.
func roots() -> [String] {
    var found = plainFolder(drive) ? [drive] : []
    for name in ["Desktop", "Documents"] {
        let folder = home + "/" + name
        let values = try? URL(fileURLWithPath: folder).resourceValues(forKeys: [.isUbiquitousItemKey])
        if !found.isEmpty && plainFolder(folder) && values?.isUbiquitousItem == true { found.append(folder) }
    }
    return found
}

// Whether anything can be reached at all: is there a route out of this Mac. No name is looked
// up and nothing is sent.
func online() -> Bool {
    var zero = sockaddr_in()
    zero.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
    zero.sin_family = sa_family_t(AF_INET)
    let target = withUnsafePointer(to: &zero) {
        $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { SCNetworkReachabilityCreateWithAddress(nil, $0) }
    }
    var flags = SCNetworkReachabilityFlags()
    guard let target = target, SCNetworkReachabilityGetFlags(target, &flags) else { return false }
    return flags.contains(.reachable) && !flags.contains(.connectionRequired)
}

// "Syncing is paused for this item": macOS 26 and later. Asked for by its name, so this also
// builds with an older SDK; an older macOS simply has no answer for it.
let syncPaused = URLResourceKey(rawValue: "NSURLUbiquitousItemIsSyncPausedKey")
let asked: Set<URLResourceKey> = [
    .isRegularFileKey, .isSymbolicLinkKey, .isUbiquitousItemKey, .ubiquitousItemDownloadingStatusKey,
    .ubiquitousItemIsUploadedKey, .ubiquitousItemIsUploadingKey, .ubiquitousItemIsDownloadingKey,
    .ubiquitousItemHasUnresolvedConflictsKey, .ubiquitousItemUploadingErrorKey, syncPaused,
    .totalFileAllocatedSizeKey, .fileAllocatedSizeKey,
    .contentModificationDateKey, .attributeModificationDateKey, .contentAccessDateKey,
]
let quiet: TimeInterval = 60 * 60       // a file changed more recently than this keeps its copy

// Every reason a file's local copy must stay: none when it may go. Asked afresh every time, never
// from memory. All of them, not just the first, so each rule can be seen to hold on its own.
func reasons(_ url: URL) -> (whys: [String], bytes: Int64, used: Double) {
    var fresh = url
    fresh.removeAllCachedResourceValues()
    guard let values = try? fresh.resourceValues(forKeys: asked) else { return (["It could not be looked at."], 0, 0) }
    let bytes = Int64(values.totalFileAllocatedSize ?? values.fileAllocatedSize ?? 0)
    let used = max(values.contentModificationDate?.timeIntervalSince1970 ?? 0, values.contentAccessDate?.timeIntervalSince1970 ?? 0)
    let now = Date()
    // A date macOS does not give, or one in the future, is not "over an hour ago".
    func settled(_ date: Date?) -> Bool { date.map { now.timeIntervalSince($0) > quiet } ?? false }
    var whys: [String] = []
    if values.isSymbolicLink == true || values.isRegularFile != true { whys.append("It is not a plain file.") }
    if values.isUbiquitousItem != true { whys.append("It is not in iCloud Drive.") }
    if values.ubiquitousItemDownloadingStatus != .current { whys.append("The copy on this Mac is not the newest version, or is not here.") }
    if values.ubiquitousItemIsUploaded != true { whys.append("It has not been uploaded to iCloud yet.") }
    if values.ubiquitousItemIsUploading != false { whys.append("It is being uploaded, or macOS does not say.") }
    if values.ubiquitousItemIsDownloading != false { whys.append("It is being downloaded, or macOS does not say.") }
    if values.ubiquitousItemHasUnresolvedConflicts != false { whys.append("It has a conflict that needs settling first, or macOS does not say.") }
    if values.ubiquitousItemUploadingError != nil { whys.append("macOS reports an error uploading it.") }
    if values.allValues[syncPaused] as? Bool == true { whys.append("Syncing is paused for it.") }
    if !settled(values.contentModificationDate) { whys.append("It was changed in the last hour.") }
    if !settled(values.attributeModificationDate) { whys.append("Something about it changed in the last hour.") }
    return (whys, bytes, used)
}

func say(_ answer: Any) {
    let data = (try? JSONSerialization.data(withJSONObject: answer, options: [])) ?? Data("null".utf8)
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data("\n".utf8))
}

func list(least: Int64) {
    let places = roots()
    let reachable = online()
    var files: [[String: Any]] = []
    // Offline, a copy that goes can't come back until the Mac is online again: nothing is offered.
    for root in reachable ? places : [] {
        // Not into packages (a Pages document is a folder that must stay whole), not into hidden
        // folders (iCloud Drive keeps its own Trash in one), and links are not followed.
        guard let walk = fm.enumerator(at: URL(fileURLWithPath: root), includingPropertiesForKeys: Array(asked),
                                       options: [.skipsPackageDescendants, .skipsHiddenFiles], errorHandler: { _, _ in true }) else { continue }
        for case let url as URL in walk {
            guard (try? url.resourceValues(forKeys: [.isRegularFileKey]))?.isRegularFile == true else { continue }
            let (whys, bytes, used) = reasons(url)
            if whys.isEmpty && bytes >= least { files.append(["path": url.path, "bytes": bytes, "used": used]) }
        }
    }
    say(["drive": !places.isEmpty, "online": reachable, "account": fm.ubiquityIdentityToken != nil, "roots": places, "files": files])
}

// The one place a local copy is removed. Every path is looked at on its own, again, right here.
func evict(_ paths: [String]) {
    let places = roots()
    let reachable = online()
    var answers: [[String: Any]] = []
    for path in paths {
        var why = ""
        var whys: [String] = []
        let inside = places.first { path.hasPrefix($0 + "/") }
        if !reachable {
            why = "This Mac is offline, so the copy stays: it could not come back."
        } else if real(path) != path {
            why = "It is not there, or is reached through a link."
        } else if inside == nil {
            why = "It is outside iCloud Drive."
        } else {
            // Nothing hidden, and nothing inside a package: every folder between iCloud Drive and the file.
            var folder = (path as NSString).deletingLastPathComponent
            var names = [(path as NSString).lastPathComponent]
            while folder.count > inside!.count {
                names.append((folder as NSString).lastPathComponent)
                if (try? URL(fileURLWithPath: folder).resourceValues(forKeys: [.isPackageKey]))?.isPackage == true {
                    why = "It is inside a package, which must stay whole."
                }
                folder = (folder as NSString).deletingLastPathComponent
            }
            if why.isEmpty && names.contains(where: { $0.hasPrefix(".") }) { why = "It is hidden, or in a hidden folder." }
            var info = stat()
            if why.isEmpty && (lstat(path, &info) != 0 || info.st_mode & S_IFMT != S_IFREG) { why = "It is not a plain file." }
            if why.isEmpty {
                whys = reasons(URL(fileURLWithPath: path)).whys
                why = whys.first ?? ""
            }
        }
        if why.isEmpty {
            do {
                try fm.evictUbiquitousItem(at: URL(fileURLWithPath: path))
            } catch {
                why = "macOS would not remove the copy: \(error.localizedDescription)"       // left as it is
            }
        }
        answers.append(["path": path, "ok": why.isEmpty, "why": why, "whys": whys.isEmpty && !why.isEmpty ? [why] : whys])
    }
    say(answers)
}

var arguments = Array(CommandLine.arguments.dropFirst())
let verb = arguments.isEmpty ? "" : arguments.removeFirst()
if verb == "list", arguments.count == 1, let least = Int64(arguments[0]), least >= 0 {
    list(least: least)
} else if verb == "evict" {
    if arguments.first == "--" { arguments.removeFirst() }
    evict(arguments)
} else {
    FileHandle.standardError.write(Data("usage: headroom-cloud list <least bytes> | evict [--] path ...\n".utf8))
    exit(64)
}
