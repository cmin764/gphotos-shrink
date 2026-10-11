# Research notes and handoff

For the next agent (or for you in a month). Read this, then `README.md`, run `pytest`, then follow the pilot protocol in section 6. The most valuable next step is not code, it is one live `try` on two photos of one album, because every assumption about Google's side is still unverified.

## 1. The case

- Owner: Cosmin. Camera: Sony A7 IV. Example file `CMN08911.JPG`: 32.7 MP, 7008 x 4672, 26.0 MB, in the shared album "Colombia" (88 items). Info panel: `Backed up (26 MB)`, `Original quality`, `Not eligible for Storage saver`.
- Goal: same pictures, same albums, 1 to 2 MB each, good enough for Instagram, Facebook and a printed photobook, originals in trash, done safely from a Typer CLI.
- First use is interactive (`try`) on a couple of the owner's own photos in one chosen album (possibly shared with others), then widen. Only big photos that still count against quota are touched.
- The file is `RAW + JPEG` exported by Imaging Edge Desktop (Apr 25), so ARW masters probably exist on a local disk. Not confirmed. If they do, the JPEGs are the disposable part and the risk is low.
- The camera clock was at UTC-02:00 (`OffsetTimeOriginal -02:00`) while shooting in Colombia (UTC-5). Google shows GMT-02:00 because it trusts the file. Re-uploading keeps this identical, which is correct.
- Unknown: how many such photos exist and how much quota a full run recovers.

## 2. How the sources were weighed

Three external reports were ingested and then tested against a real photo. The rule: a claim counts only if it is sourced or measured here. Any source that cites this repo or this file is circular and discounted.

| Source | Verdict |
|---|---|
| Perplexity brief (about 120 citations) | Main source. Careful, correct on API limits, MPF behaviour and trash. Its "12 MP is the size to hit 2 MB" hypothesis (H1) is confirmed by measurement. |
| Pasted Gemini style report | Overclaims. Its unsourced SSIMULACRA2 / MB table is refuted: "12 MP Q85 = 1.4 to 1.9 MB" is really 2.3 MB on the sample. "Pillow drops orientation and ICC" does not apply (both are passed through). "exiftool copy-back fixes the dimensions" is false (it carries the old values). "Attribution resets on re-upload" is irrelevant for your own photos. |
| `deep-research-report.md` | Circular: cites gphotos-shrink and the old section 7 of this file as "Google's advice". Independently useful for one fact: the web "Recover storage" tool skips MPF files too. Unverified lead: a Japanese "MPF Remover" app. Note `exiftool -MPF=` alone may leave the trailer behind; a full strip also needs `-Trailer:All=`. |

## 3. Ratified findings

1. **MPF blocks Storage saver.** Storage saver cannot recompress JPEGs in Multi-Picture Format (APP2 `MPF\0` plus a trailing preview image). "Convert existing photos" and "Recover storage" skip them. Why Google skips them is our explanation, not Google's statement. Stripping MPF is reported to make a file eligible (assumption A13, to be measured).
2. **No replace or delete in any API.** Since 2025-03-31 the official Library API only reads items the app created, the Picker API selects existing ones, and `appendonly` upload still works. Re-uploading creates a second item. Dedup is by content hash. `batchAddMediaItems` works on app-created albums only.
3. **State is eventually consistent.** Library cache, album membership and trash all lag. The tool polls for about a minute (`_wait_for`) instead of failing on the first read.
4. **Google-side edits live in Google's database, not in the file.** Date and location edits, descriptions, favorite, archive and album membership do not travel with a re-upload. The tool re-applies favorite, archive and caption and re-adds albums. Date and location edits would be lost, which is why edited items are excluded.
5. **Shared albums.** Adding an item probably notifies members and the custom position is not kept. Batch commands skip photos in shared albums unless `--include-shared`. `try` asks first.
6. **Trash retention.** Google's own docs say both 30 and 60 days. The tool says "at least 30 days" and treats 30 as the limit.
7. **Terms.** gpmc and gpwc use private endpoints with your own credentials. No enforcement is documented, but it is against the spirit of the terms. The tool always uploads with `use_quota=True`.
8. **Only photos charged to quota matter.** Items already stored free (`quota_charged_bytes = 0`) gain nothing and are excluded by the candidate query.

## 4. Measurements on the real photo

Photo: `samples/CMN08911.JPG` (gitignored). Sony ILCE-7M4, FE 70-200 F4 G OSS, 70 mm, f/8, 1/160, ISO 320, Portrait style with Sharpness +3, sRGB, no ICC, no GPS. Structure: APP1 Exif 49 KB (MakerNote 38 KB), APP1 XMP 57 KB (rating and document ids), APP2 MPF 88 B, primary image 25.07 MB, then a 944 KB trailer (an 891 KB 1616x1080 preview plus Sony hidden data). Original is 4:2:2 baseline.

**Encoder benchmark** (`scripts/bench.py`; SSIMULACRA2 against a lossless downscale at the same resolution; 90 is invisible at 1:1, 80 to 85 is high quality; MB excludes metadata; worst case: foliage):

| MP | Pillow Q90 | Q85 | Q80 | Q75 |
|---|---|---|---|---|
| 16 | 3.62 / 86.8 | 2.91 / 84.3 | 2.50 / 82.1 | 2.20 / 79.7 |
| 12 | 2.87 / 86.4 | 2.32 / 83.8 | 2.00 / 81.7 | 1.77 / 79.2 |
| 8 | 2.08 / 85.9 | 1.70 / 83.1 | 1.47 / 80.7 | 1.30 / 78.2 |

mozjpeg and jpegli (built from source) land within about 1 point of Pillow at matched file size (12 MP Q85: Pillow 2.32 MB / 83.8, mozjpeg 2.23 / 84.2, jpegli 2.10 / 82.9). 4:4:4 gains nothing at matched size. **Verdict: stay on Pillow, 4:2:0, no new dependency.** The "jpegli saves 35 percent" claim does not show up on this photo.

**The chosen default: 8 MP, Q85.** 3464 x 2309, 1.80 MB with all metadata, SSIMULACRA2 83.1, against 79.2 for 12 MP Q75 (1.87 MB), the best a 2 MB budget allows at 12 MP. Opened side by side at 100 percent, the original, 12 MP Q75 and 8 MP Q85 could not be told apart, and the owner picked 8 MP Q85.

| Use | Needs | 8 MP gives |
|---|---|---|
| Instagram, Facebook | about 1080 to 2048 px wide | 3464 px |
| A4 photobook page at 300 DPI | 8.7 MP | 29.3 x 19.6 cm, about 290 DPI on the full page |
| 30 x 30 cm page | 12.6 MP on a square crop, 18.8 MP filling it from a 3:2 frame | about 195 DPI; 12 MP gives about 240 DPI |
| A4 spread at 300 DPI | 17.4 MP | not enough, use `--max-mp 18 --target-mb 4` |

If a photo still exceeds the budget, quality steps down 82, 80, 78, 75. If Q75 is over the budget plus the guard's 10 percent slack (2.2 MB), the outlier guard stops the run; there is no resolution ladder.

**Metadata technique.** Pillow's `Image.Exif` re-serialization moved the Sony MakerNote (offset 1156 to 936) and the old encode dropped the 57 KB XMP. The fix writes the original EXIF blob verbatim, patches only the four pixel-dimension values in place (`patch_exif_dims`), and passes XMP and ICC through. An exiftool diff of the result against the original shows only the intended changes: dimensions, subsampling and progressive flag, a JFIF header added, MPF gone, and the Sony preview and hidden-data offsets pointing at the removed trailer (harmless exiftool "minor" warnings). Every MakerNote tag, XMP rating, DateTimeOriginal, OffsetTime, SubSec and lens tag is identical.

**Strip mode.** Lossless: drops the MPF segment and the trailer, 26.01 MB to 25.07 MB (only 0.94 MB, 3.6 percent). All of its value depends on whether Google then recompresses the file (A13).

## 5. Assumptions, verified offline vs live

Verified by `pytest` (the `try` command is tested only on its shared-album warning path, not an end-to-end upload) (real Pillow on generated and real files, fake Google clients): the whole state machine, strip and compress output, byte-exact MakerNote and XMP, the outlier guard on injected faults, album spec parsing, halt-on-first-failure, shared-album skipping and warning, quota filter, polling, trash refused without the local original, undo ordering.

Not verified, each is a way the tool may fail on a real account:

| # | Assumption in code | If wrong, you will see | Status |
|---|---|---|---|
| A1 | `get_download_urls(media_key)["1"]["5"]["2"]["6"]` is a fetchable original URL (bearer retry exists for 401/403) | `download response has no usable URL` or HTTP error | open |
| A2 | Downloaded size equals library `size_bytes`, and `dedup_key` is urlsafe base64 of the file SHA-1 | `refusing to continue` or hash mismatch | open |
| A3 | `Client.upload({path: {"hash", "filename"}}, use_quota=True)` keeps the file name and returns `{resolved_path: media_key}` | `upload returned no media key`, wrong name | open |
| A4 | Upload without `--saver` is stored as Original quality at the smaller size | Info panel shows another size or quality | open |
| A5 | Web album calls `E1Cajb` (own) and `laUYf` (shared) accept a media key from a mobile upload, with gpmc `add_media_to_album` as fallback | check `in album X` fails, original stays | open |
| A6 | After trash, a cache sync shows `trash_timestamp` within about a minute | `still in the library` error; rerun resolves | mitigated by polling |
| A7 | `utc_timestamp` is milliseconds and the copy keeps it within 2 s | check `capture time kept` fails | open |
| A8 | Cache columns `origin`, `is_edited`, `is_micro_video`, `is_locked`, `duration`, `quota_charged_bytes` mean what the names say | scan list too long or short | open |
| A9 | `set_favorite`, `set_archived`, `set_item_caption` take the dedup key | warning `could not carry over ...` | open |
| A10 | Re-encode keeps MakerNote, XMP and EXIF | missing fields in Photos Info | **resolved offline** by verbatim EXIF and XMP, guard checks it per photo |
| A11 | Adding to a shared album behaves (notifications, ordering) | surprise in album activity | open, `try` asks first |
| A12 | Google-side edits (date, location, description) revert on re-upload | lost edit on the copy | believed true, edited items excluded |
| A13 | Storage saver recompresses a stripped file uploaded with `saver=True` | `copy is smaller` fails in `--mode strip` | open, measure with one photo |
| A14 | A `photos.google.com/share/<key>` or `/album/<key>` URL carries the album media key gpwc lists | `no album matches` | open |

## 6. Live pilot protocol

1. Cold backup first: the ARW masters, or a Takeout of the pilot album.
2. `gphotos-shrink local samples/*.jpg -o out/` on a handful of your own photos, open the results, confirm the checks say `all ok`. No account is touched.
3. `gphotos-shrink auth-check`, then `scan --album <name or link>` to see exactly what would be touched.
4. `gphotos-shrink try --album <link> -c 2`. Answer `k` (keep both) after upload. The session stops at the first failed check.
5. Wait 24 hours, then check in the web UI: copy inside the album, date and time zone, favorite, the shared album activity, and the quota. Record each assumption as held, failed or changed in `LIVE_RESULTS.md`.
6. Trash the two originals with `trash --id ... --apply`, restore one with `undo` on purpose.
7. Try `--mode strip` on one photo to settle A13.
8. Batches of 25, then 250 (`process --apply --limit N`, then `trash`). A batch halts at the first outlier; `--keep-going` overrides.

## 7. Library internals used

gpmc 0.9.0 (`xob0t/gphotos_mobile_client`):
- `Client(auth_data=..., log_level=...)`, `.update_cache(show_progress)`, `.db_path` (SQLite, table `remote_media`), `.upload(target=..., use_quota, saver, show_progress)`.
- `Client.api`: `get_download_urls`, `move_remote_media_to_trash(dedup_keys)`, `restore_from_trash(dedup_keys)`, `add_media_to_album(album_media_key, media_keys)`, `set_favorite(dedup_key, bool)`, `set_archived(dedup_keys, bool)`, `set_item_caption(dedup_key, caption)`, `bearer_token`.
- `remote_media` columns used: `media_key, dedup_key, file_name, size_bytes, quota_charged_bytes, width, height, utc_timestamp, caption, is_favorite, is_archived, is_original_quality, origin (self/partner/shared), duration, is_micro_video, is_edited, is_locked, trash_timestamp`.
- `dedup_key` is the urlsafe base64 of the SHA-1 when the server provides it, otherwise it falls back to the media key.

gpwc 0.1 (`xob0t/google_photos_web_client`):
- `Client(cookies_txt_path, account_index=)`, payloads executed with `payload.execute(client)`.
- Used: `GetAlbumsPage` (`Z5xsfc`), `GetAlbumPage` (`snAcKc`), `AddItemsToExistingAlbum` (`E1Cajb`), `AddItemsToExistingSharedAlbum` (`laUYf`).
- Present but unused, relevant for backlog 3: `MoveToTrash` and `RestoreFromTrash` (`XwAOJf`), `GetItemInfo` (download URL and `space_taken`), library page listing (`lcxiM`, `EzkLib`), `GetStorageQuota` (`EzwWhf`), `GetTrashPage` (`zy0IHe`).

## 8. Backlog, ranked by impact

1. **The live pilot above, and `LIVE_RESULTS.md`.** Blocking.
2. **Settle strip versus compress** from the pilot data. If Google's own recompression of a stripped file is good, compression code can shrink to the strip path.
3. **Drop the Android requirement.** Hypothesis: gpwc covers listing, original download URL, trash and albums with cookies only, and the official `appendonly` upload covers the rest with a normal OAuth client. Open question: mapping the Library API `mediaItem.id` to the web `media_key`.
4. **Quota readout.** gpwc has `GetStorageQuota` (`EzwWhf`). Today the tool cannot say how much a full run recovers.
5. **Concurrency.** `gpmc.upload` takes `threads`. Only after rate limits are known.
6. **Ultra HDR and HEIC.** Skipped today.
7. **HTML contact sheet** with before and after thumbnails and check results.

Hardened after an independent adversarial review (three blind read-only reviewers): unreadable albums now stop the run, trash re-checks the copy and its albums at the moment of trashing, the uploaded file must match the checked SHA-1, a halt blocks the trash phase, `undo` restores after a lagging trash confirmation, changed settings re-make an un-uploaded copy, signed URLs are scrubbed from stored errors. Known and left: `state.json` has no lock (one instance at a time), Q75 over budget halts instead of lowering resolution, `has_mpf` and `has_gain_map` match raw bytes.

Done in this round: quality guard and EXIF comparison, strip mode, safe album selection, shared-album policy, hard-stop on first failure, 8 MP default. Dropped: jpegli and mozjpeg (measured, not worth a dependency).

## 9. Questions for the user

1. Do the ARW masters exist on disk? If yes, the safest cheap path for the pilot is a backup of those.
2. Are you willing to capture the gpmc auth data from an Android device, or would you prefer the OAuth plus cookies route from backlog 3?
3. How many photos at or above 5 MB are in the library, and how much quota do you want back?

## 10. Risks

- Terms of service: private endpoints, account action possible though unlikely for personal use.
- Endpoint drift: both clients are reverse engineered and can break any day. gpwc is pinned to a commit, gpmc to its minor version.
- Credential exposure: auth data and cookies grant account access. Files are read from `~/.gphotos-shrink`, a permission warning is printed for loose modes, nothing is sent anywhere except Google.
- Quota does not drop until the Google Photos trash is emptied, which the tool deliberately never does.
- Visual loss is bounded by the guard (pixel drift on a 256 px sample) but not eliminated; it is lossy compression. The local original stays on disk until you delete it.

## 11. For the next agent

Working conventions from the owner's standing instructions:
- Python or TypeScript, simplest solution that works first, small diffs, one concern per change.
- Ruff for lint and format (`pyproject.toml` sets line length 160). Google style docstrings, comments explain why, no em dashes anywhere (code, docs, replies).
- Do not commit unless asked. Align on a plan before non-trivial changes.
- Never run `--apply`, `try`, `trash` or `undo` against the real account without the owner present and approving. Offline tests are free to run.
- Do not read, print or transmit the auth data or cookies.

Starter prompt:

> Read `RESEARCH.md` and `README.md` in `gphotos-shrink/`. Run `pytest` and `ruff check .`. Do not touch the real Google account. Help me run the pilot protocol in section 6 step by step, and wait for my approval before any command that changes my account.

## 12. Sources

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
- Encoders and metric used in the benchmark: https://github.com/google/jpegli, https://github.com/mozilla/mozjpeg, https://github.com/cloudinary/ssimulacra2
