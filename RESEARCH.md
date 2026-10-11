# Research notes and handoff

For the next agent (or for you in a month). Read this, then `README.md`, run `pytest test_offline.py`, then pick from the backlog. The most valuable next step is not code, it is one live `try` run, because the tool's assumptions about Google are all unverified.

## 1. The case

- Owner: Cosmin. Camera: Sony A7 IV. Example file `CMN08911.JPG`: 32.7 MP, 7008 x 4672, 26 MB, in the shared album "Colombia" (88 items, Mar 26 to Apr 10).
- Google Photos Info panel for it: `Backed up (26 MB)`, `Original quality`, `Not eligible for Storage saver`.
- Goal: same pictures, same albums, smaller stored size, originals in trash, done safely, automated, runnable from a CLI built with Typer.
- Unknown: how many such photos exist, how much quota there is to recover, whether the originals also live on a local disk. None of these were asked or measured yet.

## 2. Findings that shaped the design

1. **Why the file is not optimizable.** Storage saver cannot compress JPEGs in Multi-Picture Format (MPF, `.mpo`). Google's own example is Android portrait mode, and reporting notes Ultra HDR falls in the same bucket. The Sony file carries an MPF index plus an embedded second image, so it is skipped and stored at full size. Recompressing the primary image would invalidate the offsets to the second image, which is my explanation for why Google skips them, not something Google states. "Convert existing photos to Storage saver" therefore does nothing for these files.
2. **No replace in any API.** Since 31 March 2025 the official Library API only lists, searches and reads items the app itself created. Selecting existing items goes through the Picker API. Upload (`photoslibrary.appendonly`) still works. There is no delete and no content replace. Re-uploading the same name creates a second item, dedup is by content hash, not by name.
3. **How people actually do it.** Everyone runs the same loop: download, compress, delete originals, upload again (ShortPixel's guide is the manual version). Tooling that automates it leaves the official API: a Chrome extension or userscript driving the web UI, or reverse engineered clients (`gpmc` mobile API, `gpwc` web API, Google Photos Toolkit). The Photos deduper author states the API cannot delete items and can only modify items the app uploaded, which is why he built a Chrome extension.
4. **Google's own conversion is one way.** A blog on this topic warns that retroactive conversion to Storage saver is permanent and Google keeps no originals. Reason enough to keep local copies and never rely on it.
5. **Upload quality.** The official API uploads at Original quality. `gpmc` has `saver` and `use_quota` switches. Without `use_quota` it behaves like a device with unlimited storage, which is the part most likely to attract attention, so this tool always sets `use_quota=True`.

## 3. Architecture and why

| Decision | Reason | Rejected alternative |
|---|---|---|
| gpmc (mobile API) for list, download, upload, trash, restore | one client covers all of it, local SQLite cache of the library gives sizes and flags without extra calls | official Library API: cannot read or delete existing items |
| gpwc (web API) for albums | gpmc has no album listing. gpwc has album pages and add calls | gpmc only: loses "same albums" |
| Local compression with Pillow, MPF dropped as a side effect | no exiftool dependency, one lossy pass is under our control | strip MPF only and let Storage saver compress (see backlog 4) |
| Copy first, trash original last, behind checks | an original is never touched until a verified copy exists | in place edits: impossible |
| JSON state, one stage at a time | resumable, readable, no extra dependency | SQLite (see ceiling note in code) |
| Dry run default, `--apply` to mutate, `try` for interactive approval | the account is the user's, mistakes are expensive | auto-run |
| Skip edited, motion, Ultra HDR, locked, shared-with-you, non-JPEG | each would lose something silently | process everything |

Per photo stages: `candidate, downloaded, compressed, uploaded, albums_done, verified, trashed`, side states `skipped`, `rolled_back`.

## 4. Verified offline vs assumed

Verified by `test_offline.py` (real Pillow on generated MPO files, fake Google clients):

- MPF trailer is removed, EXIF date and GPS and make survive, EXIF pixel dimensions follow a resize, mtime is set to capture time.
- Whole state machine, album add through both the own-album and shared-album payloads (constructor and data shape only), favorite and caption carry over, trash, undo ordering, copies never re-selected, small gain skipped, corrupt download rejected, album failure blocks trash, trash refuses without passing checks, Ultra HDR skipped, `try` end to end with scripted answers, every command's `--help`.

Not verified, each is a way the tool may fail on a real account. Test in this order with `try`:

| # | Assumption in code | If wrong, you will see | Where |
|---|---|---|---|
| A1 | `get_download_urls(media_key)["1"]["5"]["2"]["6"]` is a fetchable original URL, no auth header needed (bearer retry exists for 401/403) | `download response has no usable URL` or HTTP error | `_original_url`, `_download` |
| A2 | Downloaded size equals library `size_bytes`, and `dedup_key` equals urlsafe base64 of the file's SHA-1 for camera uploads | `refusing to continue` or hash mismatch error. `--no-hash-check` bypasses the hash only | `_download`, `_check_hash` |
| A3 | `Client.upload({path: {"hash": bytes, "filename": name}}, use_quota=True)` keeps the file name and returns `{resolved_path: media_key}` | `upload returned no media key`, or wrong name in Photos | `_to_uploaded` |
| A4 | Upload without `--saver` is stored as Original quality and counts the new smaller size. `--saver` is honored for a file without MPF | Info panel shows another size or quality than expected | uploaded copy |
| A5 | Web album calls `E1Cajb` (own) and `laUYf` (shared) accept a media key from a mobile upload, with fallback to gpmc `add_media_to_album`. Album keys from gpwc are valid for gpmc | verify check `in album X` fails, original stays | `_add_to_album` |
| A6 | After the trash call, a cache sync shows `trash_timestamp` immediately. If the delta sync lags, `_to_trashed` raises `still in the library` even though trash worked | error on a photo that is actually trashed. Rerun resolves it | `_to_trashed` |
| A7 | `utc_timestamp` is milliseconds and the copy keeps the original value within 2 s | check `capture time kept` fails | `_to_verified` |
| A8 | Cache columns `origin='self'`, `is_edited`, `is_micro_video`, `is_locked`, `duration IS NULL` mean what the names say | scan list too long or too short | `Cache._CANDIDATES` |
| A9 | `set_favorite`, `set_archived`, `set_item_caption` take the dedup key | warning `could not carry over ...` | `_carry_metadata` |
| A10 | Pillow re-encode of a real Sony A7 IV file keeps MakerNote and EXIF acceptably, and the quality 88 at 16 MP result looks fine | visual diff, missing EXIF in Photos Info | `compress_jpeg` |
| A11 | Adding to a shared album behaves (notifications to members, ordering) | surprise in the album activity | manual |

Record results of the run in a `LIVE_RESULTS.md` next to this file, one line per assumption: held, failed, or changed.

## 5. Library internals used

gpmc 0.9.0 (`xob0t/gphotos_mobile_client`):
- `Client(auth_data=..., log_level=...)`, `.update_cache(show_progress)`, `.db_path` (SQLite, table `remote_media`), `.upload(target=..., use_quota, saver, show_progress)`.
- `Client.api`: `get_download_urls`, `move_remote_media_to_trash(dedup_keys)`, `restore_from_trash(dedup_keys)`, `add_media_to_album(album_media_key, media_keys)`, `set_favorite(dedup_key, bool)`, `set_archived(dedup_keys, bool)`, `set_item_caption(dedup_key, caption)`, `bearer_token`.
- `remote_media` columns used: `media_key, dedup_key, file_name, size_bytes, quota_charged_bytes, width, height, utc_timestamp, caption, is_favorite, is_archived, is_original_quality, origin (self/partner/shared), duration, is_micro_video, is_edited, is_locked, trash_timestamp`.
- `dedup_key` is the urlsafe base64 of the SHA-1 when the server provides it, otherwise it falls back to the media key.

gpwc 0.1 (`xob0t/google_photos_web_client`):
- `Client(cookies_txt_path, account_index=)`, payloads executed with `payload.execute(client)`.
- Used: `GetAlbumsPage` (`Z5xsfc`), `GetAlbumPage` (`snAcKc`), `AddItemsToExistingAlbum` (`E1Cajb`), `AddItemsToExistingSharedAlbum` (`laUYf`).
- Present but unused, relevant for backlog 3: `MoveToTrash` and `RestoreFromTrash` (`XwAOJf`), `GetItemInfo` (download URL and `space_taken`), library page listing (`lcxiM`, `EzkLib`), `GetStorageQuota` (`EzwWhf`), `GetTrashPage` (`zy0IHe`).

## 6. Backlog, ranked by impact

1. **Run `try` live on 2 photos from 2 albums and fill `LIVE_RESULTS.md`.** Everything else depends on it. Blocking.
2. **Quantify the win before polishing.** Add a `scan` summary of expected savings using a sample compress, and a `GetStorageQuota` readout. Today the tool cannot say how much space a full run recovers.
3. **Drop the Android requirement.** The ReVanced plus adb capture is the biggest setup cost. Hypothesis: gpwc already covers listing, original download URL, trash and albums with cookies only. Only upload is missing, and the official Library API `appendonly` scope still supports it with a normal OAuth client. Open question: how to map the Library API `mediaItem.id` to the web `media_key` (match by hash or by file name plus timestamp through gpwc). If that works, `auth_data` disappears. Highest UX gain.
4. **Try the simplest path and compare.** Strip MPF losslessly (cut the APP2 MPF segment and trailing second image, pure Python, no recompression) and upload with Storage saver, so Google does the only lossy pass. Compare resulting sizes and quality against the current local compress plus original upload. If Google's output is good enough this deletes the compression code. Add as `--mode strip-only`.
5. **Automatic quality guard.** Compute SSIM or PSNR between original and copy and refuse below a threshold. Compare EXIF before and after (Make, Model, DateTimeOriginal, GPS) and fail the photo if any field is lost. Both are cheap and run before upload.
6. **Concurrency.** `gpmc.upload` takes `threads`. Download and compress are embarrassingly parallel. Needed once a run exceeds an hour.
7. **Ultra HDR and HEIC.** Currently skipped. Options: preserve the gain map by compressing it separately and re-embedding, or add `pillow-heif` for HEIC.
8. **Album order and shared album etiquette.** Preserve position if the web API exposes it. Option to skip photos in shared albums by default.
9. **Report.** HTML contact sheet with before and after thumbnails, sizes and check results per photo.
10. **Packaging and CI.** `pip install -e .` works via `pyproject.toml`. Add a CI job for ruff and pytest.

## 7. Questions for the user

1. Is Google Photos your archive, or a viewing layer over originals stored elsewhere? If the originals are on disk, the simplest workflow is: strip MPF locally, set backup to Storage saver, upload through the web uploader, delete the old ones in the UI. This tool is for the case where Google Photos holds the only copy, or for repeated use after every trip.
2. Are you willing to capture the gpmc auth data from an Android device, or would you prefer the OAuth plus cookies route from backlog 3?
3. Acceptable risk: this uses private Google endpoints with your own credentials.
4. How many photos at or above 5 MB are in the library, and how much quota do you want back?

## 8. Risks

- Terms of service: private endpoints, account action possible though unlikely for personal use.
- Endpoint drift: both clients are reverse engineered and can break any day. Pin versions after the first successful live run.
- Credential exposure: auth data and cookies grant account access. Files are read from `~/.gphotos-shrink`, a permission warning is printed for loose modes, nothing is sent anywhere except Google. Keep it that way.
- Quota does not drop until the Google Photos trash is emptied, which the tool deliberately never does.

## 9. For the next agent

Working conventions from the owner's standing instructions:
- Python or TypeScript, simplest solution that works first, small diffs, one concern per change.
- Ruff for lint and format (`pyproject.toml` sets line length 160). Google style docstrings, comments explain why, no em dashes anywhere (code, docs, replies).
- Do not commit unless asked. Align on a plan before non-trivial changes.
- Never run `--apply`, `try`, `trash` or `undo` against the real account without the owner present and approving. Offline tests are free to run.
- Do not read, print or transmit the auth data or cookies.

Starter prompt:

> Read `RESEARCH.md` and `README.md` in `gphotos-shrink/`. Run `pytest test_offline.py` and `ruff check .`. Do not touch the real Google account. Propose a short plan for backlog items 3 and 4 (cookie only route and the strip-only mode), listing what has to be measured live first and what can be built and tested offline with fakes. Wait for approval before coding.

## 10. Sources

- Not eligible for Storage saver (MPF, portraits, Ultra HDR): https://9to5google.com/2024/05/17/google-photos-not-eligible-storage-saver/
- Google Photos help, backup quality and storage saver: https://support.google.com/photos/answer/6220791
- Photos API changes effective 31 March 2025: https://developers.google.cn/photos/support/updates?authuser=1
- Manual optimize loop (download, compress, delete, re-upload): https://blog.shortpixel.com/how-to-optimize-your-google-photos/
- Deduper author on API limits (no delete, modify own items only): https://github.com/mtalcott/google-photos-deduper-python
- Bulk delete via the web UI because the API has no delete: https://github.com/SylphxAI/Google-Photos-Delete-Tool/wiki
- Retroactive conversion is permanent: https://compresto.app/blog/compress-photos-for-google-photos
- gpmc: https://pypi.org/project/gpmc/
- gpwc: https://github.com/xob0t/google_photos_web_client
- Google Photos Toolkit: https://github.com/xob0t/Google-Photos-Toolkit
- Read-only MCP built on gpwc (cookies.txt method, notes on the March 2025 change): https://glama.ai/mcp/servers/vsmelov/google-photos-mcp
