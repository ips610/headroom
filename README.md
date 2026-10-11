# headroom

My Mac ran out of space, so I made this to see what was eating it and clear the junk safely.
headroom finds caches and leftover files, shows how much space each one takes, and deletes
only what you tick. It can also uninstall apps, show you what's big, and run a few macOS fixes.

It runs as a small page in your browser at `headroom.localhost:8460`. Nothing leaves your Mac,
and nothing gets deleted until you confirm.

## Download

Get the zip for your Mac from the [Releases page](https://github.com/ips610/headroom/releases/latest):

- `headroom-apple-silicon.zip` for M1 and newer
- `headroom-intel.zip` for Intel Macs

Not sure which one you have? Apple menu → About This Mac. If it says "Chip", get Apple
Silicon. If it says "Processor", get Intel.

Unzip it, drag headroom into Applications and open it. The first time, macOS won't open it
because the app isn't notarised by Apple. Go to System Settings → Privacy & Security, scroll
down and click Open Anyway. You only have to do that once.

You don't need Python or anything else installed.

## Using it

Open headroom and it scans your Mac. The numbers from last time show straight away, greyed
out, and each section fills in again as it's measured. Tick a whole section or open it to
pick single items, then hit Delete. A progress bar shows how it's going and you can stop it
any time. Deleted files don't go to the Trash, so check what you tick.

If you just want space back, press **Safe clean**. It picks only what can't hurt: app caches,
old logs and temp files, and installers you downloaded over a month ago. You still see the
list before anything goes.

headroom has a Dock icon while it runs. Click it to bring the page back, and right-click it →
Quit (or ⌘Q) to stop headroom. Closing the tab stops it too.

You can also just type `headroom.localhost:8460` in your browser, even when headroom is closed,
and it starts. macOS keeps that address free for it, so nothing actually runs until you visit.
If you ever want that gone:

```sh
launchctl bootout gui/$(id -u)/local.headroom.door
rm ~/Library/LaunchAgents/local.headroom.door.plist
```

The first scan may ask for access to Desktop, Documents and Downloads. That's how it finds
`node_modules` and build folders in your projects.

If an app is open, its cache is greyed out until you quit it, because the app would just
write it back. Build folders you used this week stay unticked when you tick the whole
section; tick them one by one if you really mean it.

Don't want a row offered again? Hover it and press **Never offer**. It moves to "Never
offered" at the bottom of the list, where you can bring it back.

### Full Disk Access

macOS keeps the Trash, Safari's and Chrome's caches and the caches inside sandboxed apps
(WhatsApp, Zoom, Teams…) behind Full Disk Access. If you want headroom to clear those, open
System Settings → Privacy & Security → Full Disk Access, switch on headroom (add it from
Applications with + if it isn't there), then quit and reopen headroom. Without it, headroom
just tells you what it couldn't see.

## What it cleans

| Section | What's in it |
| --- | --- |
| Trash and browser caches | the Trash, and the cache folders of Safari, Chrome, Firefox, Arc, Brave and Edge (never history, logins or cookies) |
| App caches | everything in `~/Library/Caches`, the cache folders of apps like Slack, VS Code and Notion, caches inside sandboxed apps, Spotify's and Adobe's media caches |
| Logs and temporary files | `~/Library/Logs` and temp files nobody has touched for a day |
| Installer files | `.dmg`, `.pkg`, `.iso`, `.xip` and zipped apps in Downloads and on the Desktop |
| Developer tool caches | npm, Bun, Gradle, Maven, NuGet, gems, pub, Cargo, rustup, uv, Conda, Deno, Expo, Unity, Arduino, Go modules, Flutter, Xcode DerivedData, old simulators and the caches inside shut-down ones, Docker build cache, old VS Code / Cursor / Windsurf extension versions, `~/.cache` |
| Project build folders | `node_modules`, `.next`, `dist`, Rust `target`, Swift `.build`, Flutter `build` and `.dart_tool` |
| Leftovers of deleted apps | settings, caches, saved windows, containers and launch agents of apps that aren't on your Mac any more |
| Emulators, models and big downloads | Android emulators and system images, iOS simulators, Ollama and LM Studio models, unused Docker images, Xcode archives |

The last two aren't really cache. They won't come back on their own, so headroom keeps them
apart and warns you before deleting them.

## The other tabs

- **Uninstall** lists your apps with their size, when you last used them and what they keep
  in your Library. Uninstalling moves the app and those files to the Trash, so you can change
  your mind. Apple's apps aren't listed, and an app has to be closed first.
- **Disk** shows the biggest folders in your home folder and Applications (click one to look
  inside), your largest files, and downloads you haven't touched in 90 days. You can show
  anything in Finder or move it to the Trash.
- **Maintenance** refreshes Quick Look, flushes DNS, rebuilds the icon cache or the Spotlight
  index. The ones that need your password ask through macOS's own dialog; headroom never
  sees it. Rebuilding Spotlight takes hours, so it asks first.
- **History** lists everything headroom deleted or moved to the Trash, with when, what and
  where. It's kept in `~/Library/Logs/headroom/history.jsonl`.

## What it won't touch

Your actual app data (settings, chats, local storage) unless you uninstall the app or it's a
deleted app's leftovers, Docker volumes, virtualenvs, Apple's own caches and apps, and any
build folder that doesn't sit next to the file that proves what it is (`package.json`,
`Cargo.toml`, `Package.swift`, `pubspec.yaml`). If git tracks files in a build folder, it's
left alone. A deleted app's leftovers only show up when headroom is sure the app is gone.

## Is it safe?

I was careful here because it deletes things. The page never sends file paths to the app, only
the ids of rows it was shown, and the app only deletes what its own scan found. On top of that
it refuses anything outside your home folder or reached through a symlink, and only moves apps
to the Trash, never deletes them. It only listens on this Mac (`127.0.0.1` and `::1`), and the
page needs a cookie made at launch, so no website can talk to it.

## Building it yourself

You need Python 3.

```sh
git clone https://github.com/ips610/headroom.git
cd headroom
python3 install.py
```

That puts `headroom.app` in `/Applications`. `./run.sh` starts it straight from the folder, and
`python3 test_cleaner.py` runs the tests. They use a fake home folder and stand in for Finder,
so nothing real gets deleted or moved.

`./package.sh` makes the two zips for the Releases page. It needs the python.org Python 3.11
and downloads PyInstaller the first time. I build Apple Silicon and Intel separately on purpose:
a universal app made macOS warn that it "will not open in macOS 28".
