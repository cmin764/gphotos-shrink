# gphotos-shrink

Replace oversized JPEGs in Google Photos with compressed copies that sit in the same albums. The originals go to the Google Photos trash, only after every check passes, and stay restorable for at least 30 days.

Why this exists: Google Photos has no "replace" call. Re-uploading under the same name creates a second item, never an overwrite. Sony and Android JPEGs carry a Multi-Picture Format (MPF) trailer, which Google's own Storage saver refuses to compress ("Not eligible for Storage saver"), so these files stay at full size and count fully against your quota. This tool shrinks them locally (dropping the MPF trailer), uploads the result, and swaps it into place. A 26 MB Sony JPEG becomes about 1.8 MB, with every EXIF, MakerNote and XMP field intact.

Status: the logic is tested offline against fake Google clients and a real Sony photo. It has not been run against a live account. Start with `local` (no account needed), then follow "Validate the workflow by hand". Do that before any batch run.

## The compression rule

Default: cap at **8 MP (3464 x 2309), JPEG quality 85**, 4:2:0, progressive, Lanczos downscale, under a **2 MB budget**. Chosen by measurement on a real 33 MP Sony file, not by guess: it scored SSIMULACRA2 83 (high quality) at 1.8 MB and was visually indistinguishable from the original at 100 percent. See `RESEARCH.md` section 4.

| Use | Enough? |
|---|---|
| Instagram, Facebook | yes, 3 to 4 times the pixels they keep |
| A4 photobook page at 300 DPI | yes (29 x 20 cm) |
| 30 x 30 cm page | about 200 DPI, fine for a book; for 300 DPI use `--max-mp 12` |
| A4 spread or poster | `--max-mp 16 --target-mb 3.5` |

If a copy exceeds `--target-mb`, quality steps down (82, 80, 78, 75). The original EXIF blob is written byte for byte (Sony MakerNote offsets stay valid), with only the pixel dimensions patched. XMP and the ICC profile are carried over. Nothing is rotated, so Orientation stays valid.

`--mode strip` is the lossless alternative: it only drops the MPF block and its trailing preview (saves about 1 MB on a 26 MB file) and uploads as Storage saver so Google does the recompression. Whether Google really does is the open question A13 in `RESEARCH.md`; try it on one photo.

## How one photo moves through it

1. download the original, check size and SHA-1 against the library
2. make the copy (compress or strip)
3. **outlier guard**, before anything is uploaded: smaller than the original, inside the size budget, same aspect ratio and orientation, Make, Model, DateTimeOriginal, OffsetTime, lens, MakerNote, GPS and XMP identical, and a 256 px pixel comparison to catch a wrong frame or colour shift. Any failure stops the run
4. upload the copy with the original file name
5. add the copy to every album the original was in, carry over favorite, archive and caption
6. verify (polls up to a minute, the library is eventually consistent): copy exists, is smaller, capture time and file name kept, present in each album
7. move the original to trash (separate, gated step, refused unless the downloaded original is still on disk)

Progress is saved after every stage. **A batch halts at the first photo that fails a check** and prints where the original and the copy are; `--keep-going` parks it and continues instead. Rerun to resume.

## Install

```bash
uv venv                                # picks Python 3.14 from .python-version, creates .venv
uv pip install -e .                    # runtime only; use '.[dev]' (or -r requirements.txt) to add dev tools
. .venv/bin/activate && gphotos-shrink --help
```

Plain `python -m venv .venv && pip install -e .` works the same. Python 3.14 is the recommended version (everything is tested on it); 3.11 to 3.14 are supported.

With uv, run tools as `uv run --no-sync <cmd>` or activate the venv. A bare `uv run` re-resolves the project and writes a `uv.lock`, which is git-ignored on purpose: there is no lockfile, so minor and patch releases flow in on their own.
Dependencies are capped below the next major version, so minor and patch releases are picked up automatically and majors are bumped by hand. `gpwc` is not on PyPI and has no releases, so it is installed from a pinned git commit in `pyproject.toml`; bump that commit when you want its changes.

## Authentication

Two credentials, because no single unofficial client covers everything. Neither is a normal Google login. Both are account-level secrets: keep them out of git, shell history and chat, and `chmod 600` the files.

| Credential | Used for | Client |
|---|---|---|
| auth data string (`androidId=...`) | list library, download originals, upload, trash, restore | gpmc, Google Photos mobile API |
| `cookies.txt` | read albums, add copies to albums | gpwc, Google Photos web API |

Both must belong to the same Google account.

### 1. gpmc auth data (the heavy one)

You capture it once from an Android device running Google Photos ReVanced. Condensed from the gpmc README, check it for current links:

1. Install GmsCore and the patched Google Photos ReVanced apk (links in the gpmc README: https://pypi.org/project/gpmc/).
2. Connect the device to your computer with ADB.
3. Run:
   ```bash
   adb logcat | grep "auth%2Fphotos.native"        # Windows: adb logcat | FINDSTR "auth%2Fphotos.native"
   ```
4. If ReVanced was already in use, remove the Google account from GmsCore first.
5. Open Google Photos ReVanced and sign in. One or more log lines appear.
6. Copy the text from `androidId=` to the end of the line.

There is a second route with a rooted device or emulator and HTTP Toolkit, described in the same README.

Save it so it never touches shell history:

```bash
mkdir -p ~/.gphotos-shrink
printf '%s' 'androidId=...paste...' > ~/.gphotos-shrink/auth_data.txt
chmod 600 ~/.gphotos-shrink/auth_data.txt
```

### 2. Web cookies

1. Log into https://photos.google.com in a desktop browser.
2. Export the cookies for that site in Netscape `cookies.txt` format with a cookies.txt browser extension.
3. Follow the export procedure in the gpwc README (https://github.com/xob0t/google_photos_web_client). From the excerpt I read, it has you open a new empty tab and close the Photos tab after exporting, presumably so the browser does not rotate the session. Check the README, I did not read it in full.
4. Save it:
   ```bash
   cp cookies.txt ~/.gphotos-shrink/cookies.txt && chmod 600 ~/.gphotos-shrink/cookies.txt
   ```
5. If you use several Google accounts in one browser, pass `--account-index N` (the N in `photos.google.com/u/N/`).

### Where the tool looks

Highest priority first:

| Setting | Flag | Env var | Default file |
|---|---|---|---|
| auth data | `--auth-data` | `GP_AUTH_DATA` | `~/.gphotos-shrink/auth_data.txt` |
| cookies | `--cookies` | `GP_COOKIES_FILE` | `~/.gphotos-shrink/cookies.txt` |
| account index | `--account-index` | `GP_ACCOUNT_INDEX` | 0 |
| work dir | `--workdir` | `GPS_WORKDIR` | `~/.gphotos-shrink` |

Check it:

```bash
gphotos-shrink -v auth-check
```

It prints the account each client signed in as (visible with `-v`). Confirm they match. If auth fails later, the cookies or token expired: capture them again.

## Commands

Every command that changes anything online is a dry run until you pass `--apply`.

| Command | What it does | Changes online? |
|---|---|---|
| `local FILE... -o DIR` | run the exact copy pipeline and checks on local files, no credentials needed | no |
| `auth-check` | verify both credentials | no |
| `scan` | list candidates at or above `--min-size-mb` (default 5) with albums | no |
| `try` | interactive test on a few photos, approval before each irreversible step, stops at the first failed check | only as you approve |
| `process` | download, make copy, upload, re-album, verify. Never trashes | with `--apply` |
| `trash` | trash originals of verified photos | with `--apply` |
| `run` | `process` then `trash` in one go | with `--apply` |
| `undo --id KEY` | restore the original from trash, then trash the copy | with `--apply` |
| `status` | stage, sizes and errors per tracked photo | no |

Options on `scan`, `try`, `process`, `run`:

- `--album` (repeatable): the album's title, its key, a `photos.google.com/.../album/<key>` or `/share/<key>` URL, or a `photos.app.goo.gl` short link. A duplicate title is refused and the keys are listed; a typo gets suggestions.
- `--include-shared`: photos in albums shared with others are skipped by default (adding a copy may notify members and loses custom position). `try` includes them but asks before touching one.
- `--id`, `--limit`, `--min-size-mb` (5).
- `--max-mp` (8), `--quality` (85), `--target-mb` (2.0), `--mode compress|strip`, `--min-saving` (0.20), `--saver/--original`, `--no-hash-check`, `--keep-going`.

Only photos that are your own, unedited and charged to your quota are candidates. Run any command with `--help`.

## Validate the workflow by hand

Back up first (the ARW masters, or a Takeout of the pilot album), then:

```bash
gphotos-shrink local ~/Pictures/some.jpg -o out/                   # no account: vet size, quality and the checks
gphotos-shrink scan --album "<name or link>"                       # see exactly what would be touched
gphotos-shrink try --album "<name or link>" --count 2              # interactive, 2 photos of that album
```

For each photo `try` walks you through this:

1. Downloads and makes the copy. Prints original vs copy size, dimensions, quality and albums. Writes `original.jpg` and `compressed.jpg` to `~/.gphotos-shrink/review/<name>/` and offers to open both.
   Look at: sharpness at 100% zoom on a detailed area, skies and gradients for banding, colors side by side. If it is not good enough, press `n` at the upload prompt and rerun with `--max-mp 12` or `--quality 90 --target-mb 3`.
2. If the album is shared with others, warns first. Then asks `Upload the compressed copy and add it to the same albums?` Default is no.
3. Uploads, adds to albums, carries over favorite, archive and caption, then re-syncs and prints a Checks table. Anything red stops the session and leaves the original alone.
4. Now open photos.google.com and look yourself. Search the file name. Confirm:
   - both versions exist, the copy shows the smaller size under Info and the same date, time zone and location
   - the copy is inside every album the original was in
   - in a shared album, the copy appears for members and nothing odd happened to the original
   - the copy's position in the album is acceptable (new items land at the end)
5. Choose what happens to the original: `t` trash it, `k` keep both for now (default), `u` undo (trash the copy).

Choose `k` on the first run, wait a day, then:

```bash
gphotos-shrink trash --id <media_key> --apply      # media keys are in `status`
gphotos-shrink undo  --id <media_key> --apply
```

Approve the workflow when, for both test photos: the Checks table is all green, the albums match, the visual comparison is acceptable, and the trashed original restores cleanly with `undo`. Test `undo` at least once on purpose. Then widen in steps: `process --apply --limit 25`, then 250. The full protocol, with what to record, is in `RESEARCH.md` section 6.

If something is off, run with `-v` and save the output. `RESEARCH.md` lists what is assumed but unverified, which is where a surprise most likely comes from.

## Batch use

```bash
gphotos-shrink scan --min-size-mb 5
gphotos-shrink process --limit 10              # dry run: local download + copy + checks only
gphotos-shrink process --limit 10 --apply      # upload + verify, nothing trashed
gphotos-shrink status
gphotos-shrink trash --apply                   # after you spot-checked in the Photos UI
```

`run --apply` does `process` and `trash` together with two confirmation prompts. `--yes` skips the prompts.

## What counts as safe

- Nothing is deleted permanently. The only destructive call is "move to trash", which Google keeps for at least 30 days (its docs say both 30 and 60). `undo` reverses it.
- An original is only trashed if a distinct copy exists and all checks passed. The trash step re-checks this itself.
- The download must match the library's size and, when the library stores a SHA-1, the hash. Otherwise the photo stops there.
- Every copy must pass the outlier guard before upload, and a batch stops at the first failure.
- The trash step also refuses unless the downloaded original is still on disk and unchanged.
- The tool never re-processes copies it created, and skips photos where compression saves less than `--min-saving`.
- Originals stay on disk in `~/.gphotos-shrink/downloads/` until you delete them. Keep them until you are happy, they are your second backup.
- Trashed items still count against quota until the Google Photos trash is emptied. The tool never empties it. Do that yourself after you are sure.

## What is skipped and why

| Excluded | Reason |
|---|---|
| items edited in Google Photos | the download is the unedited original, re-uploading it would lose your edits |
| motion photos and videos | re-encoding breaks the embedded video |
| Ultra HDR JPEGs | re-encoding drops the gain map, turning HDR into SDR |
| locked folder, shared-with-you, partner items | not yours to change |
| items already stored free (not charged to quota) | nothing to gain |
| photos in albums shared with others | only with `--include-shared` (members may be notified) |
| non-JPEG (HEIC, PNG, RAW) | v1 handles JPEG only |

## Known limits

- Unofficial APIs. gpmc and gpwc use private Google endpoints with your own credentials. That is against Google's terms in spirit, Google can change them at any time, and an account action is possible though unlikely for personal use. The tool uploads with `use_quota=True` so it does not rely on the "unlimited storage" behavior of the mobile API.
- Albums: added copies go to the end, order is not preserved. Date and location edits made in Google Photos live outside the file and would not carry over, which is why edited items are excluded.
- A photo that fails to join an album blocks the trash step for that photo.
- Favorite, archive and caption are re-applied on a best effort basis. A failure is shown as a warning, not a block.
- Sequential, one photo at a time. Fine for hundreds of photos.
- State is one JSON file at `~/.gphotos-shrink/state.json`.

## Work directory

```
~/.gphotos-shrink/
  state.json        per-photo stage, ids, checks, errors
  downloads/        originals (your safety net, delete when done)
  compressed/       the copies that were uploaded
  review/           side by side files from `try`
  auth_data.txt     optional, chmod 600
  cookies.txt       optional, chmod 600
```

The gpmc library cache lives in `~/.gpmc/<email>/storage.db`. The first sync downloads the whole index and can take a while on a large library.

## Tests

```bash
uv pip install -r requirements.txt     # or: pip install -r requirements.txt
ruff check . && ruff format --check . && mypy && pytest
```

The tests run real compression on generated MPO files and drive the whole state machine and the CLI (`local`, `scan`, `try`, `process`, `trash`, `undo`, `run`, `status`, `auth-check`) against fake Google clients. They cover byte-exact MakerNote and XMP, strip mode, the quality step-down, each outlier-guard fault, album specs, halt-on-failure, shared-album handling and the trash invariants. They prove the tool's logic and its safety rules. They cannot prove Google accepts the calls.

Two extra tests run on a real Sony MPF JPEG when `samples/CMN08911.JPG` exists. That folder is gitignored (large, personal), so CI skips them. Drop any real MPF JPEG there under that name to run them.

CI runs ruff, mypy and pytest on Python 3.11 to 3.14 for every push to `main` and every pull request.

`scripts/bench.py PHOTO OUT_DIR` reproduces the encoder and resolution benchmark behind the defaults (needs `ssimulacra2`, via `brew install jpeg-xl`). Run it on a phone photo or a high-ISO frame before changing the defaults.
