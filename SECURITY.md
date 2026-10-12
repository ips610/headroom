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
  a symlink. Outside the home and temp folders it accepts one thing: the copies of itself an
  automated Chrome leaves next to the temp folder, and never one a running browser was started from.
- What Spotlight says is only a hint. Every file it names is checked again: really there, inside
  your home folder, not a symlink, not only in iCloud.
- With "Move cleaned files to the Trash" on, nothing is removed for good: no `rm`, and no row
  that only a command can clear. A symlink is never handed to Finder, which would move what it
  points at.
- In a git repository it only runs `git gc`, and not while a rebase or merge is half done. Every
  git call is made so that git runs nothing the repository names: no hooks, no file-system
  monitor, no fetching. A repository that sets its own program for gc is left alone.
- Local copies of iCloud Drive files are off until you switch them on under Maintenance. While
  off, iCloud Drive isn't scanned, and the app refuses to remove a copy even if asked by a row's
  id. Settings that can't be read count as off.
- With it on, the local copy of an iCloud Drive file is removed by macOS itself, a file at a
  time, never with `rm` and never through the Trash. headroom's helper only names a file macOS
  says is uploaded, the newest version, not syncing, not paused, without a conflict or an upload
  error, and unchanged for the last hour. Where macOS gives no answer, the copy stays. It asks
  all of that again right before, and refuses anything outside iCloud Drive, reached through a
  symlink, hidden or inside a package. Offline, it offers nothing. The app checks the last hour
  itself too, so an old listing can't take a file that changed since.
- Only a plain yes from Finder or from that helper counts as freed. If macOS says yes and the
  room isn't back yet, the result says it asked and gives no size.
- A scan never opens a file that is only in iCloud, since opening it would download it.
- Mail and Messages attachments only go to the Trash. Ticking the section never picks a Messages
  attachment, and the database of your conversations is never opened.
- Time Machine's local snapshots are removed by `tmutil`, by date. The page can't name one:
  headroom removes the ones it listed, and a date is digits and hyphens before it gets near a
  command. `tmutil` removes a date's snapshots on every mounted disk, and the page says so. The
  password dialog only comes up when `tmutil` says root is required.
- If its settings file can't be read, it refuses to delete instead of guessing whether cleaned
  files were meant for the Trash.
- Tasks that need an administrator go through macOS's own password dialog. headroom never sees
  your password.
- Nothing is deleted on its own, with one exception that is off until you switch it on: "Run Safe
  clean by itself once a week". Then the half-hourly check that sends the reminders clears
  caches, logs and old temporary files, and only those. Installers are left for the button. It
  scans afresh, picks by the same rule as the Safe clean button, and removes through the same
  code, with every check above. A place it couldn't look at properly is left out. It never runs
  while a delete is running or twice within seven days, and it has no session and makes none.
- That run stops at any doubt: settings that can't be read (they count as off), a Never offer
  list that can't be read (it is not taken for an empty one), a switch that moved while it was
  scanning, or the date of the run not being saved.
- It never downloads or installs anything by itself. The update check only reads a version number.
- **Update now** runs only when you press it, with the same sign-in a delete needs. It downloads
  over https from this repository's Releases on GitHub and follows redirects only to GitHub's own
  hosts. The check that matters is the signature: the new app must satisfy the designated
  requirement of the app that is running (the same identifier, signed with the same certificate),
  and be a newer version of the same app. The checksum only catches a broken download. If
  anything fails, the installed app is left exactly as it was. The old app goes to the Trash, not
  deleted. An app signed ad hoc has no signer to hold the new one to, so it isn't offered the
  button.
- A zip carries more than files: modes, access lists, and its own word on how big it unpacks.
  The signature covers none of that. So before the signature is checked, every access list is
  removed and nobody but you may write to any of it, and while it unpacks the folder is measured
  and the unpacking stopped when it grows past what headroom could be.
- While an update is under way, and until the server has stopped afterwards, no delete can start.
- Each release comes with a `SHA256SUMS` file, and the app is built from pinned, hash-checked tools.

## What it doesn't protect against

A program already running as you can do everything headroom can, so that's out of scope.
