# Changelog

## 1.0.2 - 2026-11-26

- Install packs whose archives contain characters Windows cannot store in a
  file name (such as `:` in packs made on macOS): those characters are
  replaced with `-`. Names that would then collide are an error.
- Keep each archive's own folder layout, as Companion does, instead of
  removing a single top-level folder.
- Add a desktop interface (`reasonable_companion_gui.py`).

## 1.0.1 - 2026-11-26

- Read the full catalog from `pack/`; an empty catalog search now returns no
  packs, so the previous release listed nothing. An empty catalog is now an
  error instead of a clean run.
- Never renew a session read from Companion's storage, so Companion stays
  signed in. An expired session stops the run with instructions; the stored
  session is re-read when Companion renews it.
- Take the session and the Packs folder from the same Wine user, preferring the
  most recently used Companion; symlinked Wine users count as one profile.
- Download and unpack next to the Packs folder instead of the system temporary
  directory, and move each pack into place with a single rename.
- Treat pack folders case-insensitively and never merge into a folder created
  while a download was in progress.
- Only register installs with cloud sync when installing into Companion's own
  Packs folder.
- Stop when a paginated catalog repeats a page instead of looping forever.
- Stop the run when the disk cannot hold the next pack, and print verbose
  download progress only when the percentage changes.
- Do not crash on Windows when output is redirected to a file or pipe.
- On Linux, ask for `--wine-prefix` or `--output` when no Reason prefix is
  found, rather than installing into `./Packs`.

## 1.0.0 - 2026-11-26

- Discover the live, paginated Reason Companion pack catalog rather than using
  a fixed maximum pack ID.
- Add native Windows and macOS support plus deterministic Wine/Bottles profile
  detection on Linux.
- Add cross-platform automated tests and a GitHub Actions test matrix.
- Safely validate ZIP paths before extraction and avoid creating directories in
  dry-run mode.
- Remove the Unix-only `strings` dependency from session discovery.
