# headroom

My Mac ran out of space, so I made this to see what was eating it and clear the junk safely.
headroom finds caches and leftover files, shows how much space each one takes, and deletes
only what you tick.

It runs as a small page in your browser. Nothing leaves your Mac, and nothing gets deleted
until you confirm.

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

Open headroom and it scans your Mac. Tick a whole section or open it to pick single items,
then hit Delete. A progress bar shows how it's going and you can stop it any time. Deleted
files don't go to the Trash, so check what you tick.

The first scan may ask for access to Desktop, Documents and Downloads. That's how it finds
`node_modules` and build folders in your projects.

If an app is open, its cache is greyed out until you quit it, because the app would just
write it back.

## What it cleans

| Section | What's in it |
| --- | --- |
| App caches | everything in `~/Library/Caches`, plus the cache folders of apps like Slack, VS Code and Notion |
| Logs and temporary files | `~/Library/Logs` and temp files nobody has touched for a day |
| Developer tool caches | npm, Bun, Gradle, pub, Cargo, uv, Conda, Flutter, Xcode DerivedData, old simulators, Docker build cache, `~/.cache` |
| Project build folders | `node_modules`, `.next`, Rust `target`, Flutter `build` and `.dart_tool` |
| Emulators, models and big downloads | Android emulators and system images, iOS simulators, Ollama and LM Studio models, unused Docker images, Xcode archives |

The last one isn't really cache. Those things won't come back on their own, so headroom keeps
them in their own section and warns you before deleting them.

## What it won't touch

Your actual app data (settings, chats, local storage), Docker volumes, virtualenvs, Apple's own
caches, and any `build` or `target` folder that doesn't sit next to a `pubspec.yaml` or
`Cargo.toml`. macOS locks Safari's and Chrome's caches, so clear those from the browser.

## Is it safe?

I was careful here because it deletes things. The page never sends file paths to the app, only
the ids of rows it was shown, and the app only deletes what its own scan found. On top of that
it refuses anything outside your home folder or reached through a symlink. It only listens on
`127.0.0.1` and needs a random token made at launch, so no website can talk to it.

## Building it yourself

You need Python 3.

```sh
git clone https://github.com/ips610/headroom.git
cd headroom
python3 install.py
```

That puts `headroom.app` in `/Applications`. `./run.sh` starts it straight from the folder, and
`python3 test_cleaner.py` runs the tests. They use a fake home folder, so nothing real gets
deleted.

`./package.sh` makes the two zips for the Releases page. It needs the python.org Python 3.11
and downloads PyInstaller the first time. I build Apple Silicon and Intel separately on purpose:
a universal app made macOS warn that it "will not open in macOS 28".
