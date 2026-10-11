# Security

headroom deletes files, so I'd like to hear about anything that could make it delete the wrong
thing, or let something other than you tell it what to do.

## Reporting a problem

Please don't open a public issue. On GitHub, go to this repository's **Security** tab and click
**Report a vulnerability**. That reaches only me. I reply within 7 days.

Tell me what you did, what happened, and which version you have (Maintenance → Updates shows it).

## Which versions get fixes

Only the latest release. If you're on an older one, update first: the fix will be there.

## What headroom does to stay safe

- It listens only on this Mac (`127.0.0.1` and `::1`) and answers only to its own address, so a
  website can't reach it by pointing a name at your Mac.
- The page does nothing until headroom itself has signed the browser in, which it does by opening
  a tab with a one-time code. Asking isn't enough, whoever asks. The code is never put on a
  command line, where another account on the Mac could read it.
- Being signed in takes two things: a cookie, and a key the page keeps in the browser's storage
  for its exact address, port included. A cookie alone isn't tied to a port on localhost, so
  another program on the Mac could be sent it. The key it can't get.
- The page sends the ids of rows it was shown, never file paths. The app deletes only what its own
  scan found, and checks again that each path is inside your home folder and not reached through
  a symlink.
- Tasks that need an administrator go through macOS's own password dialog. headroom never sees
  your password.
- It never downloads or installs anything by itself. The update check only reads a version number.
- Each release comes with a `SHA256SUMS` file, and the app is built from pinned, hash-checked tools.

## What it doesn't protect against

A program already running as you can do everything headroom can, so that's out of scope.
