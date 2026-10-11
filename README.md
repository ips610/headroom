# headroom

A small Mac cache cleaner that lists caches and leftover files that are safe to delete, and deletes
the ones you tick. It opens as a page in your browser, served only to this Mac.

## Use it

Open **headroom** from Applications, Spotlight or Launchpad. Or, from this folder:

```sh
./run.sh
```

Tick rows, press **Delete**, confirm. A progress bar follows the delete, and **Stop** halts it
after the item it is on. Deleted items do not go to the Trash. The app stops by itself when
you close the tab.

After changing `cleaner.py` or `ui.html`, run `python3 install.py` to rebuild the app. It
needs Python 3 (Homebrew's is used if present) and nothing else.

## Share it with someone

**No Python needed on their Mac.** `./package.sh` builds `dist/headroom.zip` (about 13 MB):
a `headroom.app` with its own Python inside, for both Apple Silicon and Intel Macs. It needs
the python.org Python 3.11 on the Mac that builds it, and downloads PyInstaller into
`.venv-build` the first time. Send the zip (AirDrop, Drive, a message). On their Mac:

1. Unzip it and drag `headroom.app` into Applications.
2. Open it. macOS blocks it the first time because it is not notarised by Apple: open
   System Settings → Privacy & Security, scroll down, and press **Open Anyway** next to
   headroom. (Or, in Terminal: `xattr -dr com.apple.quarantine /Applications/headroom.app`.)
3. It opens a page in their browser. The first scan may ask for access to Desktop,
   Documents and Downloads, which it needs to find project build folders.

**With GitHub access and Python 3:**

```sh
git clone https://github.com/ips610/headroom.git
cd headroom
python3 install.py
```

That builds `headroom.app` into `/Applications` (or `~/Applications` on an account that
cannot write there). Built on their own Mac, it opens without the Gatekeeper step.

A Mac with a full disk often cannot `fork` new processes. The app and the installer are
written to start programs without forking, so they still open in that state.

## What it lists

| Section | What is in it |
| --- | --- |
| App caches | `~/Library/Caches`, and the cache folders of Chromium-based apps (Slack, VS Code, Notion ...) in `~/Library/Application Support` |
| Logs and temporary files | `~/Library/Logs`, and temp files untouched for over a day |
| Developer tool caches | npm, Bun, Gradle, pub, Cargo, uv, Conda downloads, Flutter SDK downloads, older Claude Code versions, Xcode DerivedData and device support, simulators that no longer open, Docker build cache, `~/.cache` |
| Project build folders | `node_modules`, `.next`, Rust `target`, Flutter `build` and `.dart_tool` inside your projects |
| Emulators, models and other big downloads | Android emulators, system images and NDKs, iOS simulators that still work, Ollama and LM Studio models, Docker images no container uses, Xcode archives. Not caches: each one is gone until you download or make it again |

A row is greyed out while its app is running, because the app would write the cache back.
Only the app itself counts: helpers inside its bundle that keep running after you quit it
(WhatsApp's notification extension, draw.io's Quick Look preview) do not. Coming back to the
tab re-checks, so a row frees up as soon as you quit its app.

## What it never touches

- App data that is not cache: settings, chats, local storage, `~/.claude`.
- Docker volumes and containers, the Claude Code version you run, and any that a session is
  still running.
- Virtualenvs, and any `build` or `target` folder without the project file that proves what
  it is (`pubspec.yaml`, `Cargo.toml`).
- Apple's own caches (`com.apple.*`), and Chrome's and Safari's, which macOS keeps private.
  Clear those from the browser itself.

## How deleting is kept safe

- The page never sends a path. It sends the ids of rows from the last scan, and the server
  deletes only what that scan listed.
- A second check refuses anything outside the home folder or the per-user temp folder, or
  reached through a symlink.
- The server listens on `127.0.0.1` only, and every request needs a token made at launch,
  so no other website or program can ask it to delete anything.

## Tests

```sh
python3 test_cleaner.py
```

Builds a throwaway home folder, runs the scan and the delete against it, and checks the
guards above. Nothing real is touched.
