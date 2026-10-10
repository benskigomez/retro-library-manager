# Migrating from `extract-roms.sh` v3

`/mnt/user/roms/_scripts/extract-roms.sh` (v3) **stays where it is and is never modified or deleted by the app**.
The app does everything it did, adds dry runs and a job history, and follows the same on-disk conventions,
so the two can coexist during the changeover.

## What carries over unchanged

| Convention | Script v3 | App |
|---|---|---|
| Source archives | `_source/<system>-roms.zip` | Same. Imports are stored under exactly this name; `_source` is otherwise read-only. |
| Extracted games | `/roms/<system>/`, flat | Same: folder paths inside the archive are dropped. |
| Region filter | USA/World, `(Japan) (En)`, GoodTools codes; Proto/Beta/… excluded | Same regexes as defaults, editable globally and per system, plus allow/deny lists. |
| BIOS in sets | `[BIOS]` files moved aside | Staged to `_bios/<system>/` under the original names. |
| Inner per-game zips | unpacked via temp | Same; `.7z` inner archives are also handled. `.txt`/`.nfo` ignored. |
| Dedupe | skip when `<stem>.*` exists | Same rule; re-runs are fast and idempotent. |
| Odd file names | glob characters escaped for `unzip` | Python `zipfile` is used by default (no globbing at all); the optional `unzip` backend escapes `[ ] * ?`. |
| Ownership | `chown nobody:users`, `chmod a+rwX` | Same, after each system. |
| Lynx | `.lyx` → `.lnx` with a 64-byte header, `.lyx` kept | Same header layout, plus per-game rotation overrides and a regenerate page. |

## What is new

- A **dry run** for every bulk action (with a CSV of every decision and the rule that made it).
- A **completeness check** before any archive is read: size and mtime stable across two checks, and the central directory parses. This prevents reading a file that is still being copied over SMB.
- **Never overwrite**, and removals go to **`_trash/`** (restorable), not `rm`.
- **Jobs** with live logs, cancel and history (`_manager/logs`, `_manager/rlm.db`).
- Disc → CHD, the BIOS manager, and the arcade tools.

## Suggested changeover

1. Deploy the container (README → *Install on Unraid*). Nothing is written until you run a job.
2. For one system you have already extracted with the script, run **Import → Inspect → Dry run** on its `_source/<system>-roms.zip`.
   Expect almost everything to show as `present`. Anything shown as `include` is something the script skipped or that was added to the archive since.
3. Compare a few decisions against the script's output, using the CSV download. If a rule differs from what you want, adjust it on the system's **Filters** page.
4. From then on, use the app for new sets. The script can stay for emergencies; it and the app use the same conventions, so running either one is safe.
5. Use the dashboard's **Missing sources** panel as the to-do list for source zips that were lost: systems that have games but no `_source` archive.

Nothing in `_scripts/` is read, executed or changed by the app.
