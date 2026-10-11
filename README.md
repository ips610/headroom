# headroom

My Mac ran out of space, so I made this to see what was eating it and clear the junk safely.
headroom finds caches and leftover files, shows how much space each one takes, and deletes
only what you tick. It can also uninstall apps, find what's big or duplicated, show what starts
up with your Mac, run a few macOS fixes, and tell you when space runs low.

It runs as a small page in your browser at `headroom.localhost`. Nothing about your files leaves
your Mac, and nothing gets deleted until you confirm. The only thing it ever asks the internet is
whether there's a new version.

![headroom finding 79.3 GB to clear, in dark mode, with its eight sections and a Safe clean button](docs/screenshots/clean-dark.png)

## Getting started

You don't need Python or anything else, just macOS 12 or newer.

1. **Check which Mac you have.** Apple menu → About This Mac. If it says "Chip" (Apple M1, M2…)
   you have Apple Silicon. If it says "Processor" (Intel Core…) you have Intel.
2. **Download the zip** from the [latest release](https://github.com/ips610/headroom/releases/latest),
   under Assets:
   - `headroom-apple-silicon.zip` for Apple Silicon
   - `headroom-intel.zip` for Intel

   If you want to check the download first, get `SHA256SUMS` from the same place and run this
   in Terminal, in the folder with both files. It should say `OK`. (Safari unzips by itself and
   moves the zip to the Trash, so drag the zip back out first.)

   ```sh
   shasum -a 256 -c SHA256SUMS --ignore-missing
   ```
3. **Unzip it.** Safari does this by itself. Otherwise double-click the zip in Downloads. You get
   `headroom.app`.
4. **Move it to Applications.** Drag `headroom.app` onto Applications in the Finder sidebar. Don't
   run it from Downloads: the reminders and the `headroom.localhost` address are only set up
   when it runs from Applications.
5. **Open it the first time.** Double-click headroom in Applications. macOS says it can't check
   the app for malware, because I haven't paid Apple to notarise it. Click **Done** (not Move to
   Trash). Then open System Settings → Privacy & Security, scroll down to Security, and next to
   "headroom was blocked" click **Open Anyway**. Enter your password and click **Open Anyway**
   once more. You only do this once per download.
6. **Allow what it asks for.** headroom opens a page in your browser. macOS asks whether
   headroom may read Desktop, Documents and Downloads (that's where it finds build folders and
   old installers) and whether it may send notifications. Allow both. macOS may also say it
   added background items: those are the small helpers that make `headroom.localhost` and the
   reminders work. If you want headroom to clear the Trash, Safari's cache and the caches of apps
   like WhatsApp, also give it [Full Disk Access](#full-disk-access).
7. **Clean.** Wait for the scan, then press **Safe clean** for the easy wins, or tick what you
   want and press Delete. Nothing goes until you confirm.

After that, open it from the Dock, Launchpad or Spotlight, or just type `headroom.localhost` in
your browser.

### Updating

When you open headroom, it asks GitHub whether there's a newer version, at most once a day. If
there is, the top bar says "headroom 1.5 is out · Get it". Maintenance → Updates has a **Check
for updates** button too.

To update, quit headroom (right-click its Dock icon → Quit), download the new zip, unzip it and
drag headroom into Applications. Click **Replace** when Finder asks. A new download may need
Open Anyway once more. Your settings, history and "Never offered" list stay.

### Uninstalling

Quit headroom and drag it from Applications to the Trash. Then paste this into Terminal to
remove its helpers, settings and history:

```sh
for agent in local.headroom.door local.headroom.watch local.localhost.door; do
  launchctl bootout gui/$(id -u)/$agent 2>/dev/null
  rm -f ~/Library/LaunchAgents/$agent.plist
done
rm -rf ~/Library/Application\ Support/headroom ~/Library/Logs/headroom ~/Library/Logs/headroom.log
```

If you also use harbor, keep `local.localhost.door` (it serves `harbor.localhost` too): take it
out of the first line.

## Using it

Open headroom and it scans your Mac. The numbers from last time show straight away, greyed
out, and each section fills in again as it's measured. Tick a whole section or open it to
pick single items, then hit Delete. A progress bar shows how it's going and you can stop it
any time. Deleted files don't go to the Trash, so check what you tick.

![The App caches section opened in light mode, showing each cache with its size and where it lives](docs/screenshots/clean-light.png)

If you just want space back, press **Safe clean**. It picks only what can't hurt: app caches,
old logs and temp files, and installers you downloaded over a month ago. You still see the
list before anything goes.

headroom has a Dock icon while it runs. Click it to bring the page back, and right-click it →
Quit (or ⌘Q) to stop headroom. Closing the tab stops it too.

You can also just type `headroom.localhost` in your browser, even when headroom is closed, and
it starts. macOS keeps that address free for it, so nothing actually runs until you visit. (If
Safari searches for it instead, type `headroom.localhost/` once.) The page itself lives on port
8460; the plain name is a tiny redirect on port 80 that also knows `harbor.localhost`, my other
app. [Uninstalling](#uninstalling) says how to remove those helpers.

The first scan may ask for access to Desktop, Documents and Downloads. That's how it finds
`node_modules` and build folders in your projects.

If an app is open, its cache is greyed out until you quit it, because the app would just
write it back. Build folders you used this week stay unticked when you tick the whole
section; tick them one by one if you really mean it.

Don't want a row offered again? Hover it and press **Never offer**. It moves to "Never
offered" at the bottom of the list, where you can bring it back.

The page comes in Light and Dark. **Auto · Light · Dark** sits at the top right; Auto follows
your Mac.

### Reminders

headroom tells you when free space drops below 20 GB, and once a week, if Safe clean would
free 500 MB or more, it says how much. It checks every half hour in the background, never
repeats itself within six hours unless another 5 GB went, and never deletes anything on its
own. The first time you open headroom, macOS asks whether it may send notifications. Click one
and headroom opens. You can turn either reminder off, or change the 20 GB, under Maintenance.

### Full Disk Access

macOS keeps the Trash, Safari's and Chrome's caches, the caches inside sandboxed apps
(WhatsApp, Zoom, Teams…) and your iPhone and iPad backups behind Full Disk Access. If you want headroom to clear those, open
System Settings → Privacy & Security → Full Disk Access, switch on headroom (add it from
Applications with + if it isn't there), then quit and reopen headroom. Without it, headroom
just tells you what it couldn't see.

## What it cleans

| Section | What's in it |
| --- | --- |
| Trash and browser caches | the Trash, and the cache folders of Safari, Chrome, Firefox, Arc, Brave and Edge (never history, logins or cookies) |
| App caches | everything in `~/Library/Caches`, the cache folders of apps like Slack, VS Code and Notion, caches inside sandboxed apps, Spotify's and Adobe's media caches, qBittorrent's location database |
| Logs and temporary files | `~/Library/Logs`, qBittorrent's logs, and temp files nobody has touched for a day |
| Installer files | `.dmg`, `.pkg`, `.iso`, `.xip` and zipped apps in Downloads and on the Desktop, iPhone and iPad updates (`.ipsw`), and `.torrent` files you've already added to qBittorrent or that are over a month old |
| Developer tool caches | npm, Bun, Gradle, Maven, NuGet, gems, pub, Cargo, rustup, uv, Conda, Deno, Expo, Unity, Arduino, Go modules, Flutter, Xcode DerivedData, old simulators and the caches inside shut-down ones, Docker build cache, Homebrew's old versions and packages nothing needs, old VS Code / Cursor / Windsurf extension versions, `~/.cache` |
| Project build folders | `node_modules`, `.next`, `dist`, Rust `target`, Swift `.build`, Flutter `build` and `.dart_tool` |
| Leftovers of deleted apps | settings, caches, saved windows, containers and launch agents of apps that aren't on your Mac any more |
| Backups, emulators and big downloads | iPhone and iPad backups (these go to the Trash, since one may be your only copy), Android emulators and system images, iOS simulators, Ollama and LM Studio models, unused Docker images, stopped Docker containers and volumes no container uses, Xcode archives, unfinished downloads of torrents you removed from qBittorrent |

The last two aren't really cache. They won't come back on their own, so headroom keeps them
apart and warns you before deleting them.

## The other tabs

- **Uninstall** lists your apps with their size, when you last used them and what they keep
  in your Library. Uninstalling moves the app and those files to the Trash, so you can change
  your mind. Apple's apps aren't listed, and an app has to be closed first.
- **Disk** shows the biggest folders in your home folder and Applications (click one to look
  inside), your largest files, downloads you haven't touched in 90 days, and duplicate files in
  Downloads, Desktop and Documents. You can show anything in Finder or move it to the Trash. For
  duplicates it keeps the newest copy unless you pick another, and skips files that live only in
  iCloud so nothing downloads.
- **Startup** lists what starts with your Mac or when you log in: your own launch agents and the
  ones for every user. You can turn your own off and on again; the others are shown only.
- **Maintenance** refreshes Quick Look, flushes DNS, rebuilds the icon cache or the Spotlight
  index. The ones that need your password ask through macOS's own dialog; headroom never
  sees it. Rebuilding Spotlight takes hours, so it asks first.
- **Maintenance** also holds the two reminders.
- **History** shows a chart of free space over time, with a dot wherever headroom freed some,
  and lists everything it deleted or moved to the Trash, with when, what and where. It's kept
  in `~/Library/Logs/headroom/history.jsonl`.

## What it won't touch

Your actual app data (settings, chats, local storage) unless you uninstall the app or it's a
deleted app's leftovers, Docker volumes a container still uses, virtualenvs, Apple's own caches and apps, and any
build folder that doesn't sit next to the file that proves what it is (`package.json`,
`Cargo.toml`, `Package.swift`, `pubspec.yaml`). If git tracks files in a build folder, it's
left alone. A deleted app's leftovers only show up when headroom is sure the app is gone.
With qBittorrent, it never touches your list of torrents, its settings or anything it finished
downloading, only the half-downloaded pieces of torrents that aren't in the list any more, and
nothing of it while qBittorrent is open.

## Is it safe?

I was careful here because it deletes things. The page never sends file paths to the app, only
the ids of rows it was shown, and the app only deletes what its own scan found. On top of that
it refuses anything outside your home folder or reached through a symlink, and only moves apps
to the Trash, never deletes them.

It only listens on this Mac (`127.0.0.1` and `::1`), and the page only works in a browser
headroom has signed in, so no website can talk to it. Being signed in takes two things: a
cookie, and a key the page keeps in the browser's storage for its exact address. A cookie alone
isn't tied to a port on localhost, so another program on your Mac could be sent it. The key
never leaves headroom's own page.

The first time, headroom signs your browser in by opening a tab itself. If you typed the
address, you'll see a second tab open and the first one say you can close it. After that the
browser stays signed in for 30 days, so typing `headroom.localhost` just works. Only your default
browser gets signed in this way.

Found a security problem? [SECURITY.md](SECURITY.md) says how to tell me privately.

## Building it yourself

You need Python 3.

```sh
git clone https://github.com/ips610/headroom.git
cd headroom
python3 install.py
```

That puts `headroom.app` in `/Applications`. It's signed with my own certificate ("IPS Local Apps")
when that's in the keychain, so macOS keeps Full Disk Access and notifications across rebuilds.
Without it, the app gets a throwaway signature and macOS asks again after every rebuild. Set
`SIGN_ID` to use a different certificate. `./run.sh` starts it straight from the folder, and
`python3 test_cleaner.py` runs the tests. They use a fake home folder and stand in for Finder,
so nothing real gets deleted or moved.

`./package.sh` makes the two zips for the Releases page, and `SHA256SUMS` to upload beside them.
It needs the python.org Python 3.11, a clean git tree and my certificate in the keychain
(`ALLOW_ADHOC=1` builds without it). PyInstaller and what it needs are pinned in
`requirements-build.txt`, with hashes, and installed fresh for every build. I build Apple Silicon
and Intel separately on purpose: a universal app made macOS warn that it "will not open in macOS 28".
