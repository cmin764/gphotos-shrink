# gphotos-shrink

Replace oversized JPEGs in Google Photos with compressed copies that sit in the same albums. The originals go to the Google Photos trash, only after every check passes, and stay restorable for 60 days.

Why this exists: Google Photos has no "replace" call. Re-uploading under the same name creates a second item, never an overwrite. Sony and Android JPEGs carry a Multi-Picture Format (MPF) trailer, which Google's own Storage saver refuses to compress ("Not eligible for Storage saver"), so these files stay at full size and count fully against your quota. This tool re-encodes them locally (which drops the MPF trailer), uploads the result, and swaps it into place.

Status: the logic is tested offline against fake Google clients (11 tests). It has not been run against a live account. Section "Validate the workflow by hand" is how you find out whether Google behaves the way the tool assumes. Do that before any batch run.

## How one photo moves through it

1. download the original, check size and SHA-1 against the library
2. compress locally (resolution cap, quality, EXIF and color profile kept)
3. upload the copy with the original file name
4. add the copy to every album the original was in, carry over favorite, archive and caption
5. verify: copy exists, is smaller, capture time and file name kept, present in each album
6. move the original to trash (separate, gated step)

Progress is saved after every stage. A failure parks that photo and the run continues. Rerun to resume.

## Install

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python gphotos_shrink.py --help        # or: pip install -e . && gphotos-shrink --help
```

Python 3.10+.

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
python gphotos_shrink.py -v auth-check
```

It prints the account each client signed in as (visible with `-v`). Confirm they match. If auth fails later, the cookies or token expired: capture them again.

## Commands

Every command that changes anything online is a dry run until you pass `--apply`.

| Command | What it does | Changes online? |
|---|---|---|
| `auth-check` | verify both credentials | no |
| `scan` | list candidates at or above `--min-size-mb` (default 5) with albums | no |
| `try` | interactive test on a few photos, approval before each irreversible step | only as you approve |
| `process` | download, compress, upload, re-album, verify. Never trashes | with `--apply` |
| `trash` | trash originals of verified photos | with `--apply` |
| `run` | `process` then `trash` in one go | with `--apply` |
| `undo --id KEY` | restore the original from trash, then trash the copy | with `--apply` |
| `status` | stage, sizes and errors per tracked photo | no |

Useful options on `scan`, `try`, `process`, `run`: `--min-size-mb`, `--album` (repeatable), `--id` (repeatable), `--limit`, `--quality` (88), `--max-mp` (16), `--min-saving` (0.20), `--saver/--original`, `--no-hash-check`. Run any command with `--help`.

Compression defaults: cap at 16 MP (what Storage saver uses), JPEG quality 88, 4:2:0 subsampling, progressive. `--original` stores the copy exactly as compressed locally. `--saver` additionally lets Google recompress it, which can shrink more but adds a second lossy pass. Measure with `try` before choosing.

## Validate the workflow by hand

Do this first, on two photos from two different albums, ideally one in your shared Colombia album and one in a plain album.

```bash
python gphotos_shrink.py scan                    # see candidates and their albums
python gphotos_shrink.py try --count 2           # one photo per album where possible
python gphotos_shrink.py try --album Colombia --album "Some Other Album" --count 2   # or choose the albums
```

For each photo `try` walks you through this:

1. Downloads and compresses. Prints original vs compressed size, dimensions and albums. Writes `original.jpg` and `compressed.jpg` to `~/.gphotos-shrink/review/<name>/` and offers to open both.
   Look at: sharpness at 100% zoom on a detailed area, skies and gradients for banding, colors side by side. If quality is not good enough, press `n` at the upload prompt, rerun with `--quality 92` or `--max-mp 24`.
2. Asks `Upload the compressed copy and add it to the same albums?` Default is no.
3. Uploads, adds to albums, carries over favorite, archive and caption, then re-syncs and prints a Checks table. Anything red means the original is left alone.
4. Now open photos.google.com and look yourself. Search the file name. Confirm:
   - both versions exist, the copy shows the smaller size under Info and the same date and location
   - the copy is inside every album the original was in (open each album)
   - in a shared album, the copy appears for members and nothing odd happened to the original. Adding items to a shared album may notify other members, so test with a photo where that is fine.
   - the copy's position in the album is acceptable (new items usually land at the end, the tool does not control order)
5. Choose what happens to the original: `t` trash it, `k` keep both for now (default), `u` undo (trash the copy).

Choose `k` on the first run. After inspecting, either:

```bash
python gphotos_shrink.py trash --id <media_key> --apply      # media keys are in `status`
python gphotos_shrink.py undo  --id <media_key> --apply
```

Approve the workflow when, for both test photos: the Checks table is all green, the albums match, the visual comparison is acceptable, and the trashed original restores cleanly with `undo`. Test `undo` at least once on purpose.

If something is off, run with `-v` and save the output. `RESEARCH.md` lists what is assumed but unverified, which is where a surprise most likely comes from.

## Batch use

```bash
python gphotos_shrink.py scan --min-size-mb 5
python gphotos_shrink.py process --limit 10              # dry run: local download + compress only
python gphotos_shrink.py process --limit 10 --apply      # upload + verify, nothing trashed
python gphotos_shrink.py status
python gphotos_shrink.py trash --apply                   # after you spot-checked in the Photos UI
```

`run --apply` does `process` and `trash` together with two confirmation prompts. `--yes` skips the prompts.

## What counts as safe

- Nothing is deleted permanently. The only destructive call is "move to trash", which Google keeps for 60 days. `undo` reverses it.
- An original is only trashed if a distinct copy exists and all checks passed. The trash step re-checks this itself.
- The download must match the library's size and, when the library stores a SHA-1, the hash. Otherwise the photo stops there.
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
| non-JPEG (HEIC, PNG, RAW) | v1 handles JPEG only |

## Known limits

- Unofficial APIs. gpmc and gpwc use private Google endpoints with your own credentials. That is against Google's terms in spirit, Google can change them at any time, and an account action is possible though unlikely for personal use. The tool uploads with `use_quota=True` so it does not rely on the "unlimited storage" behavior of the mobile API.
- Albums: added copies go to the end, order is not preserved. A photo that fails to join an album blocks the trash step for that photo.
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
pytest test_offline.py
```

They run real compression on generated MPO files (EXIF, GPS, MPF stripping, resize) and drive the whole state machine, including `try`, against fake Google clients. They prove the tool's logic and its safety rules. They cannot prove Google accepts the calls.
