import Foundation

// headroom-size [--] path…
//
// Kilobytes on disk for each path, one "<kb>\t<path>" line each, in the order given: the numbers
// `du -sk -- path…` prints. It reads a whole folder's sizes in one call (getattrlistbulk) and walks
// subfolders on several threads, where du asks about one file at a time on one thread.
//
// As du does: links are never followed; a file with several names (hard links) counts once for
// each path given; a folder it may not read is left out and named on stderr; a disk mounted
// inside a folder is gone into, so the number is never less than what `rm -rf` would remove.
//
// Unlike du, it never prints a number it isn't sure of. A path it could not measure in full (a
// name too long to open, no file descriptors left, a disk that can't answer: any error but "not
// allowed" and "gone meanwhile") gets no line at all, and the caller measures that one with du.

struct FileID: Hashable {
    let dev: Int32
    let id: UInt64
}

final class Tally {
    private let lock = NSLock()
    private var bytes: Int64 = 0
    private var seen = Set<FileID>()
    private var broken = false
    func add(_ n: Int64) { lock.lock(); bytes += n; lock.unlock() }
    func first(_ file: FileID) -> Bool { lock.lock(); defer { lock.unlock() }; return seen.insert(file).inserted }
    func fail() { lock.lock(); broken = true; lock.unlock() }
    var failed: Bool { lock.lock(); defer { lock.unlock() }; return broken }
    var kb: Int64 { lock.lock(); defer { lock.unlock() }; return (bytes / 512 + 1) / 2 }     // du's rounding
}

enum Trouble { case denied, gone, other }

func trouble(_ code: Int32) -> Trouble {
    switch code {
    case EACCES, EPERM: return .denied              // du leaves these out too, and says so
    case ENOENT, ENOTDIR, ELOOP: return .gone       // deleted or replaced while we were looking
    default: return .other
    }
}

// "headroom-size: <path>: Permission denied", one write so lines from two threads never mix.
func complain(_ path: [CChar], _ code: Int32) {
    var line = Array("headroom-size: ".utf8)
    line += path.dropLast().map { UInt8(bitPattern: $0) }
    line += Array(": \(String(cString: strerror(code)))\n".utf8)
    _ = line.withUnsafeBufferPointer { write(2, $0.baseAddress, $0.count) }
}

// A few folders at a time, however many there are: each open folder is a file descriptor, and
// headroom asks about hundreds of paths in one call.
let workers = OperationQueue()
workers.maxConcurrentOperationCount = max(2, min(ProcessInfo.processInfo.activeProcessorCount, 8))
let backlog = workers.maxConcurrentOperationCount * 4      // more folders waiting than this are walked in place
let pending = DispatchGroup()                              // entered before a walk is queued, so wait() can't return early
let bufferSize = 128 * 1024

// Names are kept as the bytes the disk gave, never as text: a name that isn't valid UTF-8 would
// come back changed, the folder would not open, and its size would silently go missing.
func submit(_ path: [CChar], _ tally: Tally, root: Bool) {
    pending.enter()
    workers.addOperation {
        let buffer = UnsafeMutableRawPointer.allocate(byteCount: bufferSize, alignment: 8)
        walk(path, tally, root: root, buffer)
        buffer.deallocate()
        pending.leave()
    }
}

func walk(_ path: [CChar], _ tally: Tally, root: Bool, _ buffer: UnsafeMutableRawPointer) {
    if tally.failed { return }                             // no line will be printed for it anyway
    var folders: [[CChar]] = []
    list(path, tally, root: root, buffer, &folders)        // the folder is closed again before going deeper
    for folder in folders {
        if workers.operationCount < backlog {
            submit(folder, tally, root: false)
        } else {
            walk(folder, tally, root: false, buffer)
        }
    }
}

// Adds what one folder holds to the tally, and names its subfolders.
func list(_ path: [CChar], _ tally: Tally, root: Bool, _ buffer: UnsafeMutableRawPointer, _ folders: inout [[CChar]]) {
    let fd = open(path, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
    guard fd >= 0 else {
        let code = errno
        switch trouble(code) {
        case .denied:
            complain(path, code)
            if root { tally.fail() }                       // du prints no line for a path it may not read
        case .gone:
            if root { tally.fail() }
        case .other:
            tally.fail()                                   // ENAMETOOLONG, EMFILE, EIO…: not sure, so no number
        }
        return
    }
    defer { close(fd) }
    var info = stat()
    guard fstat(fd, &info) == 0 else { tally.fail(); return }
    var total = Int64(info.st_blocks) * 512                // the folder itself, as du counts it

    var want = attrlist()
    want.bitmapcount = UInt16(ATTR_BIT_MAP_COUNT)
    want.commonattr = attrgroup_t(ATTR_CMN_RETURNED_ATTRS) | attrgroup_t(ATTR_CMN_ERROR) | attrgroup_t(ATTR_CMN_NAME)
        | attrgroup_t(ATTR_CMN_DEVID) | attrgroup_t(ATTR_CMN_OBJTYPE) | attrgroup_t(ATTR_CMN_FILEID)
    want.fileattr = attrgroup_t(ATTR_FILE_LINKCOUNT) | attrgroup_t(ATTR_FILE_ALLOCSIZE)
    let needed = attrgroup_t(ATTR_CMN_DEVID) | attrgroup_t(ATTR_CMN_OBJTYPE) | attrgroup_t(ATTR_CMN_FILEID)
    let sizes = attrgroup_t(ATTR_FILE_LINKCOUNT) | attrgroup_t(ATTR_FILE_ALLOCSIZE)
    let head = 4 + MemoryLayout<attribute_set_t>.size

    listing: while true {
        let count = getattrlistbulk(fd, &want, buffer, bufferSize, 0)
        if count == 0 { break }
        if count < 0 {
            let code = errno
            if code == EINTR { continue }
            if trouble(code) == .denied {
                complain(path, code)
                if root { tally.fail() }
            } else {
                tally.fail()
            }
            break
        }
        var entry = buffer
        let end = buffer + bufferSize
        for _ in 0..<count {
            guard entry + head <= end else { tally.fail(); break listing }
            let length = Int(entry.loadUnaligned(as: UInt32.self))
            guard length >= head, entry + length <= end else { tally.fail(); break listing }
            let next = entry + length
            var at = entry + 4
            let returned = at.loadUnaligned(as: attribute_set_t.self)
            at += MemoryLayout<attribute_set_t>.size
            var failure: UInt32 = 0
            if returned.commonattr & attrgroup_t(ATTR_CMN_ERROR) != 0 {
                failure = at.loadUnaligned(as: UInt32.self)
                at += 4
            }
            // Without a name there is nothing to open or to complain about: not sure, so no number.
            guard returned.commonattr & attrgroup_t(ATTR_CMN_NAME) != 0, at + MemoryLayout<attrreference_t>.size <= next else {
                tally.fail(); break listing
            }
            let nameRef = at.loadUnaligned(as: attrreference_t.self)
            let nameAt = at + Int(nameRef.attr_dataoffset)
            let nameLength = Int(nameRef.attr_length)                  // counts the 0 that ends it
            guard nameLength >= 1, nameAt >= entry, nameAt + nameLength <= next else { tally.fail(); break listing }
            at += MemoryLayout<attrreference_t>.size
            var child = path
            child.removeLast()
            child.append(CChar(UInt8(ascii: "/")))
            child.append(contentsOf: UnsafeBufferPointer(start: nameAt.assumingMemoryBound(to: CChar.self), count: nameLength - 1))
            child.append(0)
            if failure != 0 {
                switch trouble(Int32(bitPattern: failure)) {
                case .denied: complain(child, Int32(bitPattern: failure))
                case .gone: break
                case .other: tally.fail(); break listing
                }
                entry = next
                continue
            }
            guard returned.commonattr & needed == needed, at + 16 <= next else { tally.fail(); break listing }
            let dev = at.loadUnaligned(as: Int32.self); at += 4
            let type = at.loadUnaligned(as: UInt32.self); at += 4
            let id = at.loadUnaligned(as: UInt64.self); at += 8
            if type == VDIR.rawValue {
                folders.append(child)
            } else {
                // A disk that won't say how much a file takes: counting it as nothing would be a lie.
                guard returned.fileattr & sizes == sizes, at + 12 <= next else { tally.fail(); break listing }
                let links = at.loadUnaligned(as: UInt32.self); at += 4
                let allocated = at.loadUnaligned(as: Int64.self)
                if links <= 1 || tally.first(FileID(dev: dev, id: id)) { total += allocated }
            }
            entry = next
        }
    }
    tally.add(total)
}

// Arguments as the bytes they came in. "--" ends the options (there are none), so a path may
// start with "-" after it; before it, anything starting with "-" is a mistake.
var roots: [[CChar]] = []
var options = true
for index in 1..<Int(CommandLine.argc) {
    guard let raw = CommandLine.unsafeArgv[index] else { continue }
    let arg = Array(UnsafeBufferPointer(start: raw, count: strlen(raw) + 1))
    if options && strcmp(raw, "--") == 0 {
        options = false
    } else if options && arg.first == CChar(UInt8(ascii: "-")) {
        fputs("usage: headroom-size [--] path ...\n", stderr)
        exit(64)
    } else {
        roots.append(arg)
    }
}

var results: [([CChar], Tally)] = []
for root in roots {
    let tally = Tally()
    var info = stat()
    if lstat(root, &info) != 0 {
        tally.fail()                                       // not there: no line, as with du
    } else if info.st_mode & S_IFMT == S_IFDIR {
        submit(root, tally, root: true)
    } else {
        tally.add(Int64(info.st_blocks) * 512)             // a file, or a link itself: never what it points at
    }
    results.append((root, tally))
}
pending.wait()

var out = [UInt8]()
var unsure = false
for (root, tally) in results {
    // A line break in the path would read as two lines: leave that one to du as well.
    if tally.failed || root.contains(10) || root.contains(13) {
        unsure = true
        continue
    }
    out += Array("\(tally.kb)\t".utf8)
    out += root.dropLast().map { UInt8(bitPattern: $0) }
    out.append(10)
}
var written = 0
while written < out.count {
    let n = out.withUnsafeBufferPointer { write(1, $0.baseAddress! + written, $0.count - written) }
    if n < 0 {
        if errno == EINTR { continue }
        exit(1)
    }
    written += n
}
exit(unsure ? 1 : 0)
