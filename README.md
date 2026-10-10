# Retro Library Manager

A self-hosted web app for a RetroBat (EmulationStation) ROM library kept on an Unraid share. It can:

- **Library dashboard**: per-system game counts, disk usage, source-archive status, staged BIOS and last job. Flags unexpected file types and lists systems whose source archives are missing.
- **Import and extract**: upload an archive or pick one on the share, detect the system, and store it in `_source` as `<system>-roms.zip`. Extraction uses region/quality filters and shows a full dry run first (CSV export included). It handles nested per-game zip/7z archives, dedupes by file stem so re-runs are idempotent, stages `[BIOS]` files separately, and writes Lynx LNX headers. Arcade sets are copied as-is.
- **Disc → CHD**: `chdman createcd`/`createdvd` per game. Handles multi-track and multi-disc sets, reports compression ratios, backs up originals to `_source/<system>/`, and keeps temp files when a conversion fails.
- **BIOS manager**: a catalog with MD5s, scanning of `_bios/` and of any archive, a status view per system, and two install modes (a mounted desktop folder, or an export bundle plus a PowerShell script).
- **Arcade tools**: import MAME `-listxml`. Verify sets by CRC32, including parents, BIOS and device sets. Classify sets as game, clone, BIOS, device or mechanical, hide non-games in `gamelist.xml`, look up short names, and install BIOS/device bundles.
- **Jobs**: a persistent SQLite queue that runs one job at a time, with live logs over Server-Sent Events, cancel, and a history page.

Safety is the design priority. `_source` is never modified apart from adding newly imported archives. Existing files are never overwritten. Anything removed goes to `_trash/<timestamp>/`, where it can be restored. Every bulk action has a dry run first. Archives are checked for completeness before they are read. `gamelist.xml` is backed up before any edit, and you must confirm RetroBat is closed. Moves between shares copy, verify, then delete.

---

## Install on Unraid

### Option A: Community Applications-style template

1. Copy `unraid/retro-library-manager.xml` to `/boot/config/plugins/dockerMan/templates-user/` on the flash drive:
   ```bash
   wget -O /boot/config/plugins/dockerMan/templates-user/my-retro-library-manager.xml \
     https://raw.githubusercontent.com/benskigomez/retro-library-manager/main/unraid/retro-library-manager.xml
   ```
2. **Docker → Add Container**, pick the *retro-library-manager* template, check the paths (below) and click **Apply**.
3. Open `http://jarvis:8787`.

The image is built by GitHub Actions and published as `ghcr.io/benskigomez/retro-library-manager:latest`. If you would rather build it on the server:

```bash
docker build -t ghcr.io/benskigomez/retro-library-manager:latest https://github.com/benskigomez/retro-library-manager.git
```

### Option B: docker compose

```bash
git clone https://github.com/benskigomez/retro-library-manager.git /mnt/user/appdata/retro-library-manager/src
cd /mnt/user/appdata/retro-library-manager/src
docker compose up -d --build
```

### Volume mappings

| Container path | Host path (Unraid) | Purpose |
|---|---|---|
| `/roms` | `/mnt/user/roms` | The RetroBat ROM share. The app keeps its config, SQLite DB, logs and uploads in `/roms/_manager`, so the Synology backup covers them. |
| `/tmp/rlm` | `/mnt/cache/appdata/retro-library-manager/tmp` | Scratch space for extraction and CHD work. Use fast storage (cache/SSD). |
| `/desktop_bios` *(optional)* | e.g. `/mnt/remotes/DESKTOP_bios` | `G:\RetroBat\bios` mounted with Unassigned Devices, for the "mounted" BIOS install mode. |

The container runs as **UID 99 / GID 100** (`nobody:users`) with umask 000. Every file it writes is also `chmod a+rwX`, matching Unraid share conventions. Tools inside the image: `unzip`/`zipinfo`, `7zz` (7-Zip) and `chdman` (Debian `mame-tools`; the full MAME binary is not needed).

Folders the app creates all start with `_` (`_manager`, `_trash`, `_bios/_install`), so RetroBat ignores them.

---

## First-run settings

1. **Log in.** If `RLM_PASSWORD` was set, use it with `RLM_USERNAME` (default `admin`). Otherwise a random password is printed once to the container log (`docker logs retro-library-manager`). Its hash is stored in `/roms/_manager/config.yaml`; change it under **Settings → Change password**.
2. **Settings → config.yaml**: check
   - `paths.temp`, the scratch path (keep `/tmp/rlm` if you mapped it as above).
   - `paths.desktop_bios`: set `/desktop_bios` if you mapped the desktop BIOS share.
   - `paths.browse_roots`: extra container paths the file picker may browse, for example an `/import` volume.
   - `stability.wait_seconds`: how long an archive's size and mtime must stay unchanged before it is read (default 5 s).
   - `bios.share_unc` / `bios.desktop_path`: used in the generated PowerShell (`\\jarvis\roms`, `G:\RetroBat\bios`).
3. **Settings → systems.yaml**: RetroBat folder names, extensions, `disc: cd|dvd`, `arcade: true`, and hooks. Keys must match RetroBat folder names exactly.
4. **Filters**: global include/exclude regexes are in `config.yaml`. For per-system overrides plus allow/deny lists, open a system page and use **Filters → edit**, which also has a test box for checking file names.

All edited YAML is validated before saving, and the previous version is kept as `<file>.bak-<timestamp>`.

### Filter rules (defaults)

Rules match the entry's **base file name**, never its folder. They are applied in this order: `[BIOS]` prefix → per-system deny-list → allow-list → exclude → include. Anything that matches none of them is excluded.

| Rule | Pattern |
|---|---|
| include: No-Intro/Redump USA/World (incl. `(USA, Europe)`, `(Japan, USA)`) | `\(([^)]*, )?(USA\|World)(, [^)]*)?\)` |
| include: Japanese release already in English | `\(Japan\) \(En[,)]` |
| include: GoodTools | `\((U\|W\|JU\|UE\|JUE)\)` |
| exclude | `\((Proto\|Beta\|Demo\|Pirate\|Unl\|Aftermarket\|Sample\|Program)[^)]*\)` |
| BIOS staging | base name starts with `[BIOS]` → `_bios/<system>/` |

---

## Typical workflow

1. Copy a set to the share, e.g. `\\jarvis\roms\_source\snes-roms.zip`, or anywhere under the share. You can also upload it from the browser.
2. **Import → Inspect** (refuses files that are still being copied) → confirm the system → **Dry run** → review or download the CSV → **Run**.
3. Follow the live log on the job page. Re-running the same archive later only adds what is new.

Disc systems: put per-game archives in a folder, then go to **Disc → CHD** → pick the folder and system → **Dry run** → **Convert selected**.

---

## Downloading a game to this device

Each file on a system page has a **⬇** link:

- Single-file games download as-is.
- Multi-file games (a `.cue` with its tracks, `.gdi`, `.m3u` with all its discs) download as one zip, built on the fly with no temp copy.
- `.chd` files on disc systems also offer **as BIN/CUE…** (CD) or **as ISO…** (DVD/UMD). This runs `chdman extractcd`/`extractdvd` as a job into the scratch folder, and the job page then shows a **Download** button. Prepared files are deleted after 24 hours. The library itself is never modified.

**Choosing the save location:** Chromium browsers (Chrome, Edge) open a "Save as" dialog only on HTTPS or localhost. That covers using the app through an HTTPS reverse proxy, or over Tailscale with `tailscale serve`. On plain `http://jarvis:8787` the browser does a normal download, which asks for a location if *Settings → Downloads → Ask where to save each file* is on.

---

## BIOS install modes

The desktop is not always on, so the app never depends on reaching it.

**Mounted.** On the desktop, share `G:\RetroBat\bios` (Properties → Sharing). On Unraid, mount it with **Unassigned Devices → Add Remote SMB Share**; it appears at `/mnt/remotes/<NAME>`. Map it to `/desktop_bios` and set `paths.desktop_bios: /desktop_bios`. **BIOS → Preview: install to mounted folder** shows exactly what will be copied. Existing files are never overwritten, and a target that differs from the catalog is reported as a conflict. Each copied file is checked by MD5.

**Export.** **BIOS → Preview: build export bundle** writes `_bios/_install/` with each file already renamed to its target name, plus `install-bios.ps1`. On the desktop, paste the script shown on the BIOS page into PowerShell, or run it directly:

```powershell
powershell -ExecutionPolicy Bypass -File \\jarvis\roms\_bios\_install\install-bios.ps1
```

It copies only missing files and prints each file's MD5 with `OK` or `MD5-MISMATCH`.

The catalog is `/roms/_manager/bios_catalog.yaml`, seeded from `app/data/bios_catalog.yaml`. Extend it by hand, or paste RetroBat's BIOS definition XML into **BIOS → Import a BIOS definition file**. The import only adds new (system, file) pairs and never changes existing entries.

---

## Generating the MAME listxml

The reference data must match the MAME version RetroBat uses (currently **0.289**). On the desktop:

```powershell
cd G:\RetroBat\emulators\mame
.\mame.exe -listxml > mame0289.xml
```

Copy `mame0289.xml` to the share (e.g. `\\jarvis\roms\_manager\uploads\`) or upload it on the **Arcade** page, then click **Import**. The file is about 250 MB and is parsed as a stream into `/roms/_manager/mame.db`. Re-import whenever RetroBat updates MAME.

**Verify** reads only zip central directories (CRC32), so it is fast. It reports only the sets you have:
- **good**: every required ROM was found in the set, its parent/BIOS chain, or the required device zips.
- **missing**: lists each missing ROM with the zip that would supply it (e.g. Spy Hunter needs `midssio.zip` and `midcsd.zip`).
- **unknown**: the zip name is not a MAME machine.

ROMs marked "no good dump known" and CHD requirements are informational, not failures. **Hide non-games** marks BIOS, device and mechanical sets (e.g. `spyhuntr`, Bally's Spy Hunter *pinball*) `hidden=true` in `/roms/mame/gamelist.xml`, adding entries where missing.

---

## Recovery

- **Trash**: removals (files trashed from a system page, Lynx headers replaced by "regenerate") go to `/roms/_trash/<timestamp>/` with their path relative to the share. **Trash → restore** moves them back and skips anything that already exists at the original path. **Purge** is the only permanent delete, and it shows a dry run first.
- **gamelist.xml backups**: every edit first writes `gamelist.xml.bak-<timestamp>` next to the file. To roll back, close RetroBat, then on Unraid:
  ```bash
  cd /mnt/user/roms/mame
  ls -l gamelist.xml.bak-*
  cp -p gamelist.xml.bak-20261010-153000 gamelist.xml
  ```
- **Config backups**: `/roms/_manager/*.yaml.bak-<timestamp>`.
- **Failed CHD conversions**: temp files stay under the scratch path (`chd-<job>/<game>`) for inspection, and the original archive is not moved. Delete the folder when you are done.
- **Interrupted jobs**: a job that was running when the container stopped is marked `interrupted`. Re-run it; every job re-plans against the disk and skips what is already done.

---

## Development

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
pytest -q
RLM_ROMS=./roms RLM_PASSWORD=dev uvicorn app.main:create_app --factory --reload --port 8787
```

All test data is synthetic: dummy bytes and generated zip/7z archives, a synthetic `-listxml`, and a fake `chdman`. The repo never contains ROMs, BIOS files or DATs, and `.gitignore` blocks their extensions.

Layout:

```
app/
  main.py            FastAPI factory, auth, routers
  config.py          config.yaml model (pydantic) + comment-preserving edits
  jobs.py, db.py     SQLite job queue, worker thread, logs
  fsutil.py          all writes: no-overwrite, trash, copy-verify-move, stability check, perms
  archives.py        zip (zipfile or Info-ZIP unzip with glob escaping) and 7z (7zz / py7zr)
  filters.py         region/quality rules
  extract.py         dry-run planner + extraction engine + _source import
  hooks/lynx.py      LNX header writer
  chd.py             disc discovery, cue/gdi parsing, chdman runner
  bios.py            catalog, MD5 scan, install/export, definition import
  arcade/            MAME listxml import, verify/classify/lookup, jobs
  gamelist.py        gamelist.xml backup + hide
  routes/, templates/, static/
app/data/            shipped systems.yaml, bios_catalog.yaml, config.default.yaml
unraid/              Unraid template
```

See [MIGRATION.md](MIGRATION.md) for how this replaces `_scripts/extract-roms.sh`.
