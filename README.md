# gcode_sync

# Untested AI SLOP

## Here's what the machine wrote

Sync local G-code folders to the correct 3D-printer SD cards or USB drives.
Printer profiles and card-to-printer assignments are saved between runs. Cards
are identified by filesystem UUID, not changing device names like `/dev/sdb1`.
A printer can have multiple cards; each card belongs to one printer.

## Requirements

- Linux, Python **3.8+**, and `rsync`.
- `lsblk`, `findmnt`, `mount`, and `umount` (normally supplied by util-linux).
- `sudo` access for registering cards in `/etc/fstab` only. **Run the script as
  your normal user, not with sudo.** File copying does not run as root.
- Filesystem support for your cards: FAT (`vfat`), exFAT, ext2/3/4, or NTFS.
  Your printer must also support the format; most expect FAT32. This program
  never partitions or formats a card. NTFS/exFAT may require additional drivers
  or mount helpers supplied by your distribution.

This is portable across Linux distributions, not a macOS/Windows tool: those
systems do not use the same block-device tools and `/etc/fstab` workflow.
The implementation uses Python, with a shell-safe entry point that immediately
executes Python even when invoked with `sh`. `gcode_sync -h` or `--help` prints
usage and exits without reading profiles, starting setup, or mounting cards.

## First-time setup

From this directory:

```sh
python3 gcode_sync --setup
```

Setup asks for:

1. A unique printer ID, such as `ender3`, and a display name.
2. The **existing directory containing that printer's G-code**, not an individual
   `.gcode` file. Use a separate directory per printer. Spaces, Unicode, `~`,
   absolute paths, and relative paths work; enter the path without shell quotes.
   Empty and invalid paths are rejected. The resolved directory is shown for
   confirmation. Its **contents**, including subfolders and other regular files,
   are synced to the card root, not a nested copy of the directory.
3. An optional root filename to copy **from the card back to the source folder**
   before syncing, such as `ROBIN_NANO.CUR`. Leave blank to disable this behavior.
   This overwrites the same filename in the source if it exists; multiple cards
   for a printer share that local file, with the last synced card taking precedence.
4. A unique card ID, such as `ender3-card1`, and the inserted card's filesystem.
   Check the device, size, filesystem, label, UUID, and mount point carefully.
   Some USB readers report cards as fixed disks, so those require extra confirmation.
5. Explicit approval of the proposed `/etc/fstab` entry. Only then does setup
   request sudo to create `/mnt/gcode_sync/CARD_ID` and register it.

Setup can add several cards for the same printer. It does not sync files until
you run the script again. Running without options when the config is missing
or has no saved printers also starts setup, including when standard input is
redirected. Provide input interactively for the prompts. `--help` never starts
setup; `--dry-run` with no profiles asks you to run setup instead of changing files.

Profiles are saved atomically, with user-only permissions, in
`$XDG_CONFIG_HOME/gcode_sync/config.json` (default
`~/.config/gcode_sync/config.json`). Use `--config /path/to/config.json` to select
a different profile file. There is no automatic import of the old hard-coded
paths: register your printers and cards once using setup.

## Daily use

```sh
# Sync all connected, registered cards; absent cards are skipped.
python3 gcode_sync

# Inspect saved printer folders and card assignments.
python3 gcode_sync --list

# Limit syncing to one printer or one card.
python3 gcode_sync --printer ender3
python3 gcode_sync --card ender3-card1

# Preview file operations without copying, backing up, or renaming files.
# A dry run may still mount and unmount a card.
python3 gcode_sync --dry-run

# Explicitly mirror the folder, DELETING card files absent from the source.
# Preview first! Without --delete, existing card-only files are retained.
python3 gcode_sync --printer ender3 --delete --dry-run
python3 gcode_sync --printer ender3 --delete
```

Files are compared using checksums, so same-sized changed files are not silently
skipped. This reads file contents and may take longer than timestamp-only checks.
Symlinks and special files are not copied. The source directory must not be the
card mount or contain it, and must not be inside the card mount.

## Manage printers and cards

```sh
python3 gcode_sync --add-printer
python3 gcode_sync --edit-printer ender3
python3 gcode_sync --add-card --printer ender3
python3 gcode_sync --assign-card ender3-card1 --printer another-printer
```

Editing a printer lets you update its display name, source folder, and optional
configuration-backup filename without losing its card assignments. Reassigning
a card changes its saved printer association; no `/etc/fstab` change is needed.
Use `--list` to see the saved IDs. Changes to optional backup filenames are
explicit: enter a filename again when editing, or leave blank to disable it.

## Mounting and `/etc/fstab` safety

- Entries use `UUID=...` and fixed, space-free paths under `/mnt/gcode_sync`.
- `noauto,users,nofail,rw` avoids automatically mounting cards at boot, permits
  user mounting/unmounting, and makes missing cards nonfatal. `users` also
  implies `noexec,nosuid,nodev`. It permits **other local users** to mount/unmount
  these cards; use this on a trusted workstation.
- FAT/exFAT/NTFS entries set your numeric user/group IDs and `umask=022`.
  ext filesystems retain their existing ownership: an administrator must grant
  your user write access **on the mounted filesystem** before syncing. Changing
  ownership of the empty mount directory alone is insufficient.
- Existing exact entries are reused; conflicting UUID/mount entries and duplicate
  registrations are refused. If an older entry already manages a card, resolve
  it manually rather than adding a second entry for the same UUID.
- Before changing `/etc/fstab`, setup creates a timestamped
  `/etc/fstab.gcode_sync.*.bak` backup, preserves existing contents and file mode,
  and atomically replaces the file. Cooperating setup processes use a lock;
  avoid editing fstab manually while setup runs. No reboot is needed for `mount`.
- Every copy verifies the mounted filesystem's UUID. Cloned duplicate UUIDs
  connected at the same time are refused. Reformatting a card may change its UUID;
  register it again and retire the old profile/entry manually.
- If your desktop automounts a card under `/media` or `/run/media`, **unmount it
  there first** (without powering off/ejecting the reader), then run the script.
  The script refuses to write to a card mounted at an unexpected location.
- Cards mounted by this script are unmounted even if copying fails. Cards already
  mounted at their configured paths are left mounted: eject/unmount them before
  removal. An unmount failure is reported as an error; **do not pull out the card**.
- A failed card does not stop attempts on other cards. Any failure, or no connected
  matching cards, produces a nonzero exit status.

To retire a card, remove its object from the JSON `cards` mapping and remove its
specific UUID/mount entry from `/etc/fstab` with an administrator's editor. To
retire a printer, remove/reassign its cards first, then its `printers` entry.
Back up both files before editing. The program never removes fstab entries on
its own.

## Optional firmware renaming

```sh
python3 gcode_sync --card ender3-card1 --rename-bin
# -r is the short form
```

After syncing, rename a single root `.bin` file to a fresh 8.3 name such as
`F2345678.BIN`, for bootloaders that require a new filename to flash. **This may
trigger a firmware flash on the printer's next power-on.** Verify that it is the
correct firmware for that printer. Renaming is opt-in and disabled for dry runs.
Ambiguous multiple binaries are refused. Generated names are tracked per card;
a later rename can replace that known previous artifact when exactly one source
firmware file is present. Unknown binaries are never removed by this logic
(`--delete`, if explicitly requested, still applies to all card contents).

## Tests

```sh
python3 -m unittest discover -s tests -v
```

Tests use temporary directories and mocked devices/commands. The privileged
fstab helper is tested with all paths relocated into a temporary directory;
tests never edit the machine's `/etc/fstab`, mount a real card, or invoke sudo.
