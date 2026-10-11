# headroom

A small Mac cache cleaner that lists caches and leftover files that are safe to delete, and deletes
the ones you tick. It opens as a page in your browser, served only to this Mac.

## Use it

Open **headroom** from Spotlight or Launchpad (installed in `~/Applications`). Or, from
this folder:

```sh
./run.sh
```

Tick rows, press **Delete**, confirm. A progress bar follows the delete, and **Stop** halts it
after the item it is on. Deleted items do not go to the Trash. The app stops by itself when
you close the tab.

After changing `cleaner.py` or `ui.html`, run `python3 install.py` to rebuild the app. It
needs Python 3 (Homebrew's is used if present) and nothing else.

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
