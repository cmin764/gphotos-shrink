#!/usr/bin/env python3
"""Shrink oversized Google Photos JPEGs without losing albums, and without losing the originals.

Google Photos has no API to replace a photo, so the only workable loop is:
download the original, compress it locally, upload the compressed copy, put it
in the same albums, verify, then move the original to the Google Photos trash
(restorable for 60 days). This tool runs that loop per photo as a resumable
state machine and refuses to trash anything it has not verified.

Stages per photo: candidate, downloaded, compressed, uploaded, albums_done,
verified, trashed. Failures stop the photo at its current stage and are retried
on the next run. Nothing is ever deleted permanently.

Auth: see README.md. Two credentials are needed, because no single unofficial
client covers everything:
  GP_AUTH_DATA        gpmc (mobile API): library listing, download, upload, trash
  GP_COOKIES_FILE     gpwc (web API): album membership read and album writes

Typical session:
  gphotos-shrink auth-check
  gphotos-shrink scan
  gphotos-shrink try --count 2        # interactive, 2 photos from different albums
  gphotos-shrink process --apply      # upload + verify everything, trash nothing
  gphotos-shrink trash --apply        # after you eyeballed the result
"""

from __future__ import annotations

import base64
import csv
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import closing
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlparse

import requests
import typer
from gpmc import Client as GpmcClient
from gpwc import Client as GpwcClient
from gpwc.payloads import (
    AddItemsToExistingAlbum,
    AddItemsToExistingSharedAlbum,
    GetAlbumPage,
    GetAlbumsPage,
)
from PIL import Image
from rich.console import Console
from rich.progress import track
from rich.table import Table

DEFAULT_WORKDIR = Path.home() / ".gphotos-shrink"
STAGES = ["candidate", "downloaded", "compressed", "uploaded", "albums_done", "verified", "trashed"]
SIDE_STATES = {"skipped", "rolled_back"}
SHA1_B64_LEN = 27  # urlsafe base64 of a 20 byte digest, no padding
GOOGLE_HOST_SUFFIXES = (".googleusercontent.com", ".google.com", ".googleapis.com")
TS_TOLERANCE_MS = 2000

console = Console(highlight=False)


class SkipItem(Exception):
    """The photo is fine as it is (for example, compression gains too little)."""


class VerifyError(Exception):
    """A post-upload check failed, so the original must stay untouched."""


@dataclass
class Config:
    """Compression and safety knobs shared by every command."""

    workdir: Path
    quality: int = 88
    max_mp: float = 16.0
    min_saving: float = 0.20
    saver: bool = False
    verify_hash: bool = True


@dataclass
class Selection:
    """Which photos a command works on."""

    min_mb: float = 5.0
    albums: list[str] = field(default_factory=list)
    ids: list[str] = field(default_factory=list)
    limit: int | None = None


@dataclass
class Item:
    """One photo and everything we learned while replacing it."""

    media_key: str
    dedup_key: str
    file_name: str
    size_bytes: int
    quota_bytes: int
    width: int
    height: int
    utc_ms: int
    caption: str | None = None
    favorite: bool = False
    archived: bool = False
    albums: list[dict[str, Any]] = field(default_factory=list)
    stage: str = "candidate"
    new_media_key: str = ""
    new_dedup_key: str = ""
    new_size: int = 0
    original_path: str = ""
    compressed_path: str = ""
    checks: list[list[Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    note: str = ""
    error: str = ""
    updated_at: float = 0.0


@dataclass(frozen=True)
class AlbumRef:
    """An album as seen by the web client."""

    key: str
    title: str
    shared: bool


class State:
    """Resumable per-photo progress stored in one JSON file.

    Deliberate shortcut: a single JSON file rewritten atomically after every
    stage. Fine up to roughly 10k photos; move to SQLite once a rewrite takes
    noticeable time or two processes need to run concurrently.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self.items: dict[str, Item] = {}
        if path.exists():
            raw = json.loads(path.read_text())
            self.items = {key: Item(**value) for key, value in raw.get("items", {}).items()}

    def get(self, media_key: str) -> Item | None:
        """Return the tracked item for a library media key, if any."""
        return self.items.get(media_key)

    def put(self, item: Item) -> None:
        """Store an item and flush the whole state to disk atomically."""
        item.updated_at = time.time()
        self.items[item.media_key] = item
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"items": {k: asdict(v) for k, v in self.items.items()}}, indent=1))
        tmp.replace(self._path)

    def new_keys(self) -> set[str]:
        """Media keys of copies this tool uploaded, which must never be re-processed."""
        return {item.new_media_key for item in self.items.values() if item.new_media_key}


class Cache:
    """Read-only view of the gpmc local library cache (a SQLite mirror of the library)."""

    _CANDIDATES = """
        SELECT media_key, dedup_key, file_name, size_bytes, quota_charged_bytes, width, height,
               utc_timestamp, caption, is_favorite, is_archived, is_original_quality, make, model
        FROM remote_media
        WHERE (trash_timestamp IS NULL OR trash_timestamp = 0)
          AND origin = 'self'
          AND duration IS NULL
          AND COALESCE(is_micro_video, 0) = 0
          AND COALESCE(is_edited, 0) = 0
          AND COALESCE(is_locked, 0) = 0
          AND size_bytes >= ?
          AND (lower(file_name) LIKE '%.jpg' OR lower(file_name) LIKE '%.jpeg')
        ORDER BY size_bytes DESC
    """

    def __init__(self, gp: Any) -> None:
        self._gp = gp

    def sync(self, show_progress: bool = True) -> None:
        """Pull library changes into the local cache (first run downloads the full index)."""
        self._gp.update_cache(show_progress=show_progress)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(f"file:{self._gp.db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    def candidates(self, min_bytes: int) -> list[sqlite3.Row]:
        """Own, unedited, non-motion JPEG photos at or above the size threshold, largest first."""
        # closing(), because the sqlite3 connection context manager only ends a transaction and leaks the handle.
        with closing(self._connect()) as conn:
            return conn.execute(self._CANDIDATES, (min_bytes,)).fetchall()

    def row(self, media_key: str) -> sqlite3.Row | None:
        """Fetch one cached library row by media key."""
        with closing(self._connect()) as conn:
            row: sqlite3.Row | None = conn.execute("SELECT * FROM remote_media WHERE media_key = ?", (media_key,)).fetchone()
            return row


def item_from_row(row: sqlite3.Row) -> Item:
    """Build a fresh Item from a cache row."""
    return Item(
        media_key=row["media_key"],
        dedup_key=row["dedup_key"],
        file_name=row["file_name"],
        size_bytes=row["size_bytes"] or 0,
        quota_bytes=row["quota_charged_bytes"] or 0,
        width=row["width"] or 0,
        height=row["height"] or 0,
        utc_ms=row["utc_timestamp"] or 0,
        caption=row["caption"],
        favorite=bool(row["is_favorite"]),
        archived=bool(row["is_archived"]),
    )


# --------------------------------------------------------------------------- pure helpers


def has_gain_map(path: Path) -> bool:
    """Detect an Ultra HDR gain map (recent Pixel and Samsung JPEGs).

    The gain map lives in the MPF secondary image and is described in XMP near
    the file start. A Pillow re-encode drops it, which silently turns an HDR
    photo into an SDR one, so those files are skipped instead of degraded.
    """
    with path.open("rb") as handle:
        head = handle.read(256 * 1024)
    return b"hdrgm" in head or b"hdr-gain-map" in head


def sha1_file(path: Path) -> bytes:
    """SHA-1 digest of a file, streamed."""
    digest = hashlib.sha1()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.digest()


def dedup_key_of(sha1: bytes) -> str:
    """Google Photos dedup key: urlsafe base64 of the SHA-1, without padding."""
    return base64.urlsafe_b64encode(sha1).decode().rstrip("=")


def safe_name(name: str) -> str:
    """Filesystem-safe file name."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)


def human(num_bytes: float) -> str:
    """Format a byte count as MB."""
    return f"{num_bytes / 1_000_000:.1f} MB"


def _set_exif_dims(exif: Image.Exif, width: int, height: int) -> None:
    """Keep EXIF pixel dimensions truthful after a resize so viewers do not show stale sizes."""
    sub = exif.get_ifd(0x8769)
    if sub is not None:
        sub[0xA002] = width
        sub[0xA003] = height
    for tag, value in ((0x0100, width), (0x0101, height)):
        if tag in exif:
            exif[tag] = value


def compress_jpeg(src: Path, dst: Path, *, quality: int, max_mp: float, mtime_ms: int | None = None) -> tuple[int, int]:
    """Re-encode a JPEG: cap resolution, lower quality, keep EXIF and color profile.

    Re-encoding with Pillow drops the Multi-Picture Format (MPF) trailer that
    Sony and Android cameras append, which is exactly what makes Google's
    Storage saver refuse these files. EXIF (date, GPS, camera) and the ICC
    profile are carried over. Pixels are never rotated, so the Orientation tag
    stays valid.

    Args:
        src: Original JPEG.
        dst: Output path, written atomically.
        quality: JPEG quality 1-100.
        max_mp: Downscale only when the image exceeds this many megapixels.
        mtime_ms: If given, set the output mtime to this capture time so
            clients that fall back to file time still sort correctly.

    Returns:
        Output (width, height).
    """
    with Image.open(src) as im:
        if getattr(im, "n_frames", 1) > 1:
            im.seek(0)
        exif = im.getexif()
        icc = im.info.get("icc_profile")
        out = im if im.mode == "RGB" else im.convert("RGB")
        width, height = out.size
        if width * height > max_mp * 1_000_000:
            scale = math.sqrt(max_mp * 1_000_000 / (width * height))
            width, height = int(width * scale), int(height * scale)
            out = out.resize((width, height), Image.Resampling.LANCZOS)
            _set_exif_dims(exif, width, height)
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + ".part")
        out.save(tmp, "JPEG", quality=quality, optimize=True, progressive=True, subsampling="4:2:0", exif=exif, icc_profile=icc)
    tmp.replace(dst)
    if mtime_ms:
        seconds = mtime_ms / 1000
        os.utime(dst, (seconds, seconds))
    return width, height


def _original_url(payload: dict[str, Any]) -> str:
    """Pick the original-bytes URL out of gpmc's decoded download response."""
    try:
        node = payload["1"]["5"]["2"]
    except (KeyError, TypeError) as exc:
        raise RuntimeError("download response has no URL block") from exc
    # "6" is the untouched original, "5" is the version with edits applied.
    url = node.get("6") or node.get("5")
    if not isinstance(url, str) or not url.startswith("http"):
        raise RuntimeError("download response has no usable URL")
    return url


def _is_google_host(url: str) -> bool:
    host = urlparse(url).hostname or ""
    return host.endswith(GOOGLE_HOST_SUFFIXES)


# --------------------------------------------------------------------------- web album helpers


def _paged(web: Any, make_payload: Callable[[str | None], Any]) -> Iterator[Any]:
    """Iterate a paginated gpwc payload until next_page_id runs out."""
    page: str | None = None
    while True:
        resp = make_payload(page).execute(web)
        if not resp.success:
            raise RuntimeError(f"web API call {resp.rpcid} failed")
        yield resp.data
        page = resp.data.next_page_id
        if not page:
            return


def list_albums(web: Any) -> list[AlbumRef]:
    """All albums visible to the account."""
    albums: list[AlbumRef] = []
    for data in _paged(web, lambda page: GetAlbumsPage(page_id=page)):
        albums += [AlbumRef(a.media_key, a.title or "(untitled)", bool(a.is_shared)) for a in data.items]
    return albums


def album_media_keys(web: Any, album_key: str) -> set[str]:
    """Media keys currently inside one album."""
    keys: set[str] = set()
    for data in _paged(web, lambda page: GetAlbumPage(media_key=album_key, page_id=page)):
        keys |= {item.media_key for item in data.items}
    return keys


def build_album_index(web: Any) -> dict[str, list[AlbumRef]]:
    """Map every media key to the albums that contain it.

    Deliberate shortcut: reads every album in full on each run. Ceiling is a few
    hundred albums before startup gets slow; cache per album by modified time
    when a library crosses that.
    """
    index: dict[str, list[AlbumRef]] = {}
    for album in list_albums(web):
        try:
            for key in album_media_keys(web, album.key):
                index.setdefault(key, []).append(album)
        except Exception as exc:  # noqa: BLE001 - albums shared with us may refuse reads
            console.print(f"[yellow]skipped album {album.title!r}: {exc}[/yellow]")
    return index


# --------------------------------------------------------------------------- the pipeline


class Shrinker:
    """Runs the per-photo state machine against Google Photos."""

    def __init__(self, gp: Any, web: Any, state: State, cache: Cache, cfg: Config) -> None:
        self.gp = gp
        self.web = web
        self.state = state
        self.cache = cache
        self.cfg = cfg
        self._album_index: dict[str, list[AlbumRef]] | None = None

    # ---- selection

    def album_index(self) -> dict[str, list[AlbumRef]]:
        """Album membership, loaded once per run."""
        if self._album_index is None:
            self._album_index = build_album_index(self.web) if self.web else {}
        return self._album_index

    def select(self, sel: Selection) -> list[Item]:
        """Resolve a selection into tracked items, resuming ones already in the state file."""
        rows = self.cache.candidates(int(sel.min_mb * 1_000_000))
        own_copies = self.state.new_keys()
        index = self.album_index()
        wanted = {a.casefold() for a in sel.albums}
        picked: list[Item] = []
        for row in rows:
            key = row["media_key"]
            if key in own_copies:
                continue
            if sel.ids and key not in sel.ids:
                continue
            item = self.state.get(key) or item_from_row(row)
            if item.stage in {"trashed", "skipped", "rolled_back"} and not sel.ids:
                continue
            if item.stage == "candidate":
                item.albums = [asdict(a) for a in index.get(key, [])]
            if wanted and not wanted & {a["title"].casefold() for a in item.albums}:
                continue
            picked.append(item)
            if sel.limit and len(picked) >= sel.limit:
                break
        return picked

    # ---- driver

    def advance(self, item: Item, last: str, confirm: Callable[[Item, str], bool] | None = None) -> bool:
        """Move an item forward until it reaches ``last``.

        Args:
            item: The photo to process.
            last: Final stage to reach, one of STAGES.
            confirm: Optional gate called with (item, "upload" | "trash") right
                before each side-effecting step. Returning False stops cleanly.

        Returns:
            True if the item reached ``last`` (or was legitimately skipped).
        """
        if item.stage in SIDE_STATES:
            return True
        while STAGES.index(item.stage) < STAGES.index(last):
            nxt = STAGES[STAGES.index(item.stage) + 1]
            if confirm and nxt in ("uploaded", "trashed") and not confirm(item, "upload" if nxt == "uploaded" else "trash"):
                return False
            try:
                getattr(self, f"_to_{nxt}")(item)
            except SkipItem as exc:
                item.stage, item.note, item.error = "skipped", str(exc), ""
                self.state.put(item)
                return True
            except Exception as exc:  # noqa: BLE001 - any failure must park the item, not crash the batch
                item.error = f"{type(exc).__name__}: {exc}"
                self.state.put(item)
                return False
            item.stage, item.error = nxt, ""
            self.state.put(item)
        return True

    # ---- stages

    def _to_downloaded(self, item: Item) -> None:
        dest = self.cfg.workdir / "downloads" / f"{item.media_key[:12]}_{safe_name(item.file_name)}"
        if not (dest.exists() and dest.stat().st_size == item.size_bytes):
            self._download(item, dest)
        item.original_path = str(dest)
        self._check_hash(item, dest)

    def _download(self, item: Item, dest: Path) -> None:
        url = _original_url(self.gp.api.get_download_urls(item.media_key))
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        headers: dict[str, str] = {}
        for attempt in range(3):
            try:
                with requests.get(url, stream=True, headers=headers, timeout=120) as resp:
                    if resp.status_code in (401, 403) and not headers and attempt < 2 and _is_google_host(url):
                        # Some download URLs are not pre-signed; retry once with the session token.
                        headers = {"Authorization": f"Bearer {self.gp.api.bearer_token}"}
                        continue
                    resp.raise_for_status()
                    with part.open("wb") as handle:
                        for chunk in resp.iter_content(1 << 20):
                            handle.write(chunk)
                break
            except requests.RequestException:
                if attempt == 2:
                    raise
                time.sleep(2 * (attempt + 1))
        part.replace(dest)
        if dest.stat().st_size != item.size_bytes:
            size = dest.stat().st_size
            dest.unlink()
            raise RuntimeError(f"downloaded {size} bytes, library says {item.size_bytes}; refusing to continue")

    def _check_hash(self, item: Item, path: Path) -> None:
        """Prove the download is the stored original before anything depends on it."""
        if not self.cfg.verify_hash:
            return
        if len(item.dedup_key) != SHA1_B64_LEN:
            item.warnings.append("dedup key is not a SHA-1, download hash not verified")
            return
        if dedup_key_of(sha1_file(path)) != item.dedup_key:
            raise RuntimeError("downloaded bytes do not match the stored SHA-1 (edited or different file); use --no-hash-check to override")

    def _to_compressed(self, item: Item) -> None:
        src = Path(item.original_path)
        if has_gain_map(src):
            raise SkipItem("Ultra HDR gain map would be lost by re-encoding")
        dst = self.cfg.workdir / "compressed" / src.name
        width, height = compress_jpeg(src, dst, quality=self.cfg.quality, max_mp=self.cfg.max_mp, mtime_ms=item.utc_ms)
        size = dst.stat().st_size
        if size > item.size_bytes * (1 - self.cfg.min_saving):
            dst.unlink(missing_ok=True)
            raise SkipItem(f"gain too small: {human(item.size_bytes)} to {human(size)} (needs {self.cfg.min_saving:.0%})")
        item.compressed_path = str(dst)
        item.new_size = size
        item.new_dedup_key = dedup_key_of(sha1_file(dst))
        item.note = f"{width}x{height}"

    def _to_uploaded(self, item: Item) -> None:
        if item.new_media_key:
            return
        path = Path(item.compressed_path)
        # use_quota is always on: the point is honest accounting, not the unlimited-storage trick.
        result = self.gp.upload(
            target={path: {"hash": sha1_file(path), "filename": item.file_name}},
            use_quota=True,
            saver=self.cfg.saver,
            show_progress=False,
        )
        key = result.get(str(path.resolve())) or result.get(str(path))
        if not key:
            raise RuntimeError("upload returned no media key")
        item.new_media_key = key

    def _to_albums_done(self, item: Item) -> None:
        for album in (AlbumRef(**a) for a in item.albums):
            if item.new_media_key in album_media_keys(self.web, album.key):
                continue
            self._add_to_album(album, item.new_media_key)
        self._carry_metadata(item)

    def _add_to_album(self, album: AlbumRef, new_key: str) -> None:
        payload_cls = AddItemsToExistingSharedAlbum if album.shared else AddItemsToExistingAlbum
        try:
            if payload_cls(item_media_keys=[new_key], album_media_key=album.key).execute(self.web).success:
                return
        except Exception:  # noqa: BLE001 - fall through to the mobile API
            pass
        self.gp.api.add_media_to_album(album_media_key=album.key, media_keys=[new_key])

    def _carry_metadata(self, item: Item) -> None:
        """Favorite, archive and caption are not part of the file, so re-apply them to the copy."""
        steps: list[tuple[str, Callable[[], Any]]] = []
        if item.favorite:
            steps.append(("favorite", lambda: self.gp.api.set_favorite(item.new_dedup_key, True)))
        if item.archived:
            steps.append(("archive", lambda: self.gp.api.set_archived([item.new_dedup_key], True)))
        if item.caption:
            steps.append(("caption", lambda: self.gp.api.set_item_caption(dedup_key=item.new_dedup_key, caption=item.caption)))
        for label, call in steps:
            try:
                call()
            except Exception as exc:  # noqa: BLE001 - cosmetic loss, original stays restorable
                item.warnings.append(f"could not carry over {label}: {exc}")

    def _to_verified(self, item: Item) -> None:
        self.cache.sync(show_progress=False)
        row = self.cache.row(item.new_media_key)
        checks: list[list[Any]] = []

        def check(label: str, ok: bool, detail: str = "") -> None:
            checks.append([label, bool(ok), detail])

        check("copy exists in library", row is not None and not row["trash_timestamp"])
        if row is not None:
            check("copy is smaller", row["size_bytes"] < item.size_bytes, f"{human(item.size_bytes)} to {human(row['size_bytes'])}")
            check("capture time kept", abs((row["utc_timestamp"] or 0) - item.utc_ms) <= TS_TOLERANCE_MS)
            check("file name kept", row["file_name"] == item.file_name, row["file_name"])
        for album in (AlbumRef(**a) for a in item.albums):
            check(f"in album {album.title}", item.new_media_key in album_media_keys(self.web, album.key))
        item.checks = checks
        failed = [c[0] for c in checks if not c[1]]
        if failed:
            raise VerifyError("failed: " + ", ".join(failed))

    def _to_trashed(self, item: Item) -> None:
        # Invariants that must hold before the only destructive call in this tool.
        if not item.new_media_key or item.new_dedup_key == item.dedup_key or item.new_media_key == item.media_key:
            raise RuntimeError("no distinct verified copy, refusing to trash the original")
        if not item.checks or not all(c[1] for c in item.checks):
            raise RuntimeError("verification checks missing or failing, refusing to trash the original")
        self.gp.api.move_remote_media_to_trash(dedup_keys=[item.dedup_key])
        self.cache.sync(show_progress=False)
        row = self.cache.row(item.media_key)
        if row is not None and not row["trash_timestamp"]:
            raise RuntimeError("trash call returned but the original is still in the library")

    # ---- undo

    def undo(self, item: Item) -> None:
        """Return to the pre-run state: restore the original, then drop the copy.

        Order matters: the original is restored first so there is never a moment
        with zero copies in the library.
        """
        if item.stage == "trashed":
            self.gp.api.restore_from_trash(dedup_keys=[item.dedup_key])
        if item.new_dedup_key and item.new_media_key:
            self.gp.api.move_remote_media_to_trash(dedup_keys=[item.new_dedup_key])
        item.stage, item.error, item.note = "rolled_back", "", "undone by user"
        self.state.put(item)


# --------------------------------------------------------------------------- settings and CLI


@dataclass
class Settings:
    """Resolved CLI context with lazily built clients."""

    workdir: Path
    auth_data: str
    cookies: Path | None
    account_index: int
    verbose: bool
    _gp: Any = None
    _web: Any = None

    def gp(self) -> Any:
        """Build the mobile-API client."""
        if self._gp is None:
            if not self.auth_data:
                fail(
                    "No gpmc auth data. Set GP_AUTH_DATA, pass --auth-data, or save it to "
                    f"{self.workdir / 'auth_data.txt'} (see README.md, section Authentication)."
                )
            self._gp = GpmcClient(auth_data=self.auth_data, log_level="INFO" if self.verbose else "WARNING")
        return self._gp

    def web(self, required: bool = True) -> Any:
        """Build the web-API client used for albums."""
        if self._web is None:
            if not self.cookies or not self.cookies.exists():
                if required:
                    fail(
                        "No cookies.txt. Set GP_COOKIES_FILE, pass --cookies, or save it to "
                        f"{self.workdir / 'cookies.txt'} (see README.md, section Authentication)."
                    )
                return None
            warn_if_open_perms(self.cookies)
            self._web = GpwcClient(self.cookies, account_index=self.account_index, log_level="INFO" if self.verbose else "WARNING")
        return self._web

    def shrinker(self, cfg: Config, need_web: bool = True) -> Shrinker:
        """Wire up the pipeline."""
        gp = self.gp()
        return Shrinker(gp, self.web(required=need_web), State(self.workdir / "state.json"), Cache(gp), cfg)


def fail(message: str) -> None:
    """Print an error and exit non-zero."""
    console.print(f"[red]{message}[/red]")
    raise typer.Exit(1)


def warn_if_open_perms(path: Path) -> None:
    """Credential files grant account access, so flag group or world readable ones."""
    if path.stat().st_mode & 0o077:
        console.print(f"[yellow]{path} is readable by other users. Run: chmod 600 {path}[/yellow]")


def _read_if_exists(path: Path) -> str:
    return path.read_text().strip() if path.exists() else ""


def ask_choice(text: str, choices: str, default: str) -> str:
    """Prompt until the answer is one of the single-letter choices.

    Done by hand because click.Choice is not importable from every Typer
    version (recent releases vendor click internally).
    """
    while True:
        answer = str(typer.prompt(text, default=default)).strip().lower()
        if len(answer) == 1 and answer in choices:
            return answer
        console.print(f"Pick one of: {', '.join(choices)}")


app = typer.Typer(
    # The module docstring is for readers of the source; --help gets a short version
    # because click collapses newlines and would mangle the session example.
    help=(
        "Replace oversized Google Photos JPEGs with compressed copies in the same albums, "
        "and move the originals to the Google Photos trash only after every check passes. "
        "Start with: auth-check, scan, try. Full guide in README.md."
    ),
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode=None,
)

MinSize = Annotated[float, typer.Option("--min-size-mb", help="Only photos at or above this size, in MB (decimal).")]
AlbumOpt = Annotated[list[str] | None, typer.Option("--album", "-a", help="Only photos in this album title. Repeatable.")]
IdOpt = Annotated[list[str] | None, typer.Option("--id", help="Only this library media key. Repeatable. Also re-opens skipped or undone items.")]
LimitOpt = Annotated[int | None, typer.Option("--limit", "-n", help="Process at most N photos.")]
QualityOpt = Annotated[int, typer.Option("--quality", min=50, max=100, help="JPEG quality of the compressed copy.")]
MaxMpOpt = Annotated[float, typer.Option("--max-mp", help="Downscale only if above this many megapixels (Google's own Storage saver uses 16).")]
MinSavingOpt = Annotated[float, typer.Option("--min-saving", min=0.0, max=0.95, help="Skip photos where compression saves less than this fraction.")]
SaverOpt = Annotated[bool, typer.Option("--saver/--original", help="Upload the copy as Storage saver (Google recompresses) or Original quality.")]
HashOpt = Annotated[bool, typer.Option("--hash-check/--no-hash-check", help="Require the download to match the stored SHA-1.")]


@app.callback()
def main(
    ctx: typer.Context,
    workdir: Annotated[Path, typer.Option(envvar="GPS_WORKDIR", help="State, downloads and compressed copies live here.")] = DEFAULT_WORKDIR,
    auth_data: Annotated[
        str, typer.Option(envvar="GP_AUTH_DATA", help="gpmc auth string (androidId=...). Prefer the env var or auth_data.txt over the flag.")
    ] = "",
    cookies: Annotated[Path | None, typer.Option(envvar="GP_COOKIES_FILE", help="Netscape cookies.txt from photos.google.com.")] = None,
    account_index: Annotated[int, typer.Option(envvar="GP_ACCOUNT_INDEX", help="The N in photos.google.com/u/N/ for multi-login browsers.")] = 0,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Resolve credentials and working directory."""
    workdir = workdir.expanduser()
    workdir.mkdir(parents=True, exist_ok=True)
    if not auth_data:
        auth_file = workdir / "auth_data.txt"
        if auth_file.exists():
            warn_if_open_perms(auth_file)
            auth_data = _read_if_exists(auth_file)
    if cookies is None and (workdir / "cookies.txt").exists():
        cookies = workdir / "cookies.txt"
    ctx.obj = Settings(workdir, auth_data, cookies.expanduser() if cookies else None, account_index, verbose)


def _cfg(st: Settings, quality: int, max_mp: float, min_saving: float, saver: bool, hash_check: bool) -> Config:
    return Config(st.workdir, quality, max_mp, min_saving, saver, hash_check)


def _sel(min_mb: float, albums: list[str] | None, ids: list[str] | None, limit: int | None) -> Selection:
    return Selection(min_mb, albums or [], ids or [], limit)


def _items_table(items: list[Item], title: str) -> Table:
    table = Table(title=title)
    for col in ("file", "size", "dims", "albums", "stage"):
        table.add_column(col)
    for item in items:
        table.add_row(item.file_name, human(item.size_bytes), f"{item.width}x{item.height}", ", ".join(a["title"] for a in item.albums) or "-", item.stage)
    return table


@app.command("auth-check")
def auth_check(ctx: typer.Context) -> None:
    """Verify both credentials work and show which account they belong to."""
    st: Settings = ctx.obj
    st.gp()
    console.print("[green]gpmc (mobile API): authenticated[/green]")
    web = st.web()
    albums = list_albums(web)
    console.print(f"[green]gpwc (web API): authenticated, {len(albums)} albums visible[/green]")
    console.print("Both accounts must be the same one. gpmc logged its user above (run with -v); gpwc logged its account the same way.")


@app.command()
def scan(
    ctx: typer.Context,
    min_size_mb: MinSize = 5.0,
    album: AlbumOpt = None,
    limit: LimitOpt = None,
    sync: Annotated[bool, typer.Option("--sync/--no-sync", help="Refresh the local library cache first.")] = True,
    csv_path: Annotated[Path | None, typer.Option("--csv", help="Also write the list to this CSV.")] = None,
) -> None:
    """List big JPEGs that are candidates (read-only, changes nothing).

    Photos edited in Google Photos, motion photos, videos, locked-folder items,
    and photos shared with you are excluded because re-uploading would lose or
    break something.
    """
    st: Settings = ctx.obj
    sh = st.shrinker(Config(st.workdir), need_web=False)
    if sync:
        sh.cache.sync()
    if sh.web is None:
        console.print("[yellow]No cookies configured: album column is empty. Albums are required for process/try/run.[/yellow]")
    items = sh.select(_sel(min_size_mb, album, None, limit))
    console.print(_items_table(items, f"{len(items)} candidates"))
    total = sum(i.size_bytes for i in items)
    console.print(f"total {human(total)}; counted against quota: {human(sum(i.quota_bytes for i in items))}")
    if csv_path:
        with csv_path.open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["media_key", "file_name", "size_bytes", "quota_bytes", "width", "height", "albums"])
            for i in items:
                writer.writerow([i.media_key, i.file_name, i.size_bytes, i.quota_bytes, i.width, i.height, "|".join(a["title"] for a in i.albums)])


def _pick_diverse(items: list[Item], count: int) -> list[Item]:
    """Pick photos so each comes from a different album where possible."""
    groups: dict[str, list[Item]] = {}
    for item in items:
        groups.setdefault(item.albums[0]["title"] if item.albums else "(no album)", []).append(item)
    picked = [group[0] for group in groups.values()][:count]
    if len(picked) < count:
        rest = [i for i in items if i not in picked]
        picked += rest[: count - len(picked)]
    return picked


def _review(sh: Shrinker, item: Item) -> None:
    """Drop original and compressed side by side so they can be opened and compared."""
    folder = sh.cfg.workdir / "review" / Path(item.file_name).stem
    folder.mkdir(parents=True, exist_ok=True)
    shutil.copy2(item.original_path, folder / "original.jpg")
    shutil.copy2(item.compressed_path, folder / "compressed.jpg")
    table = Table(title=item.file_name)
    table.add_column("")
    table.add_column("original")
    table.add_column("compressed")
    table.add_row("size", human(item.size_bytes), human(item.new_size))
    table.add_row("dimensions", f"{item.width}x{item.height}", item.note)
    table.add_row("saved", "", f"{1 - item.new_size / item.size_bytes:.0%}")
    table.add_row("albums", ", ".join(a["title"] for a in item.albums) or "-", "")
    console.print(table)
    console.print(f"Files for comparison: {folder}")
    if typer.confirm("Open both files now?", default=True):
        for name in ("original.jpg", "compressed.jpg"):
            try:
                typer.launch(str(folder / name))
            except Exception:  # noqa: BLE001 - headless shells cannot launch viewers
                console.print(f"[yellow]could not open {name}, open it manually[/yellow]")


@app.command("try")
def try_(
    ctx: typer.Context,
    count: Annotated[int, typer.Option("--count", "-c", min=1, help="How many photos to test, each from a different album if possible.")] = 2,
    min_size_mb: MinSize = 5.0,
    album: AlbumOpt = None,
    ids: IdOpt = None,
    quality: QualityOpt = 88,
    max_mp: MaxMpOpt = 16.0,
    min_saving: MinSavingOpt = 0.20,
    saver: SaverOpt = False,
    hash_check: HashOpt = True,
) -> None:
    """Interactive dry run on a few photos with approval at every irreversible step.

    For each photo: download and compress, show before and after, let you open
    both files, then ask before uploading. After upload it adds the copy to the
    same albums, runs the checks, and asks what to do with the original
    (trash it, keep both, or undo). Pick photos from different albums to see
    how album handling behaves, including shared albums.
    """
    st: Settings = ctx.obj
    sh = st.shrinker(_cfg(st, quality, max_mp, min_saving, saver, hash_check))
    sh.cache.sync()
    pool = sh.select(_sel(min_size_mb, album, ids, None))
    if not pool:
        fail("No candidates match.")
    picks = _pick_diverse(pool, count)
    console.print(_items_table(picks, "Test set"))
    for item in picks:
        console.rule(item.file_name)
        if not sh.advance(item, "compressed"):
            console.print(f"[red]{item.error}[/red]")
            continue
        if item.stage == "skipped":
            console.print(f"[yellow]skipped: {item.note}[/yellow]")
            continue
        _review(sh, item)
        if not typer.confirm("Upload the compressed copy and add it to the same albums?", default=False):
            console.print("Nothing uploaded. State saved, rerun to resume.")
            continue
        if not sh.advance(item, "verified"):
            console.print(f"[red]{item.error}[/red]\nThe original is untouched. Run 'undo --id {item.media_key} --apply' to drop the copy.")
            continue
        table = Table(title="Checks")
        table.add_column("check")
        table.add_column("result")
        table.add_column("detail")
        for label, ok, detail in item.checks:
            table.add_row(label, "[green]ok[/green]" if ok else "[red]FAIL[/red]", detail)
        console.print(table)
        for warning in item.warnings:
            console.print(f"[yellow]{warning}[/yellow]")
        console.print("Now look in photos.google.com: search the file name, you should see both versions, and the copy inside the albums above.")
        choice = ask_choice("Original: [t]rash it, [k]eep both for now, [u]ndo (drop the copy)", "tku", "k")
        if choice == "t":
            ok = sh.advance(item, "trashed")
            console.print("[green]original moved to trash (restorable for 60 days)[/green]" if ok else f"[red]{item.error}[/red]")
        elif choice == "u":
            sh.undo(item)
            console.print("Undone: copy trashed, original untouched.")
    _summary(sh.state)


def _summary(state: State) -> None:
    done = [i for i in state.items.values() if i.stage in ("verified", "trashed")]
    saved = sum(i.size_bytes - i.new_size for i in done)
    console.print(f"{len(done)} photos verified or trashed, {human(saved)} saved once the Google Photos trash is emptied.")


def _batch(st: Settings, cfg: Config, sel: Selection, *, apply: bool, trash: bool, yes: bool) -> None:
    sh = st.shrinker(cfg)
    sh.cache.sync()
    items = sh.select(sel)
    if not items:
        console.print("Nothing to do.")
        return
    console.print(_items_table(items, f"{len(items)} photos, {human(sum(i.size_bytes for i in items))}"))
    if not apply:
        console.print("[yellow]Dry run: downloading and compressing locally only. Add --apply to upload and verify.[/yellow]")
    elif not yes:
        typer.confirm(f"Upload compressed copies for {len(items)} photos?", abort=True)
    last = "verified" if apply else "compressed"
    for item in track(items, description="processing"):
        if not sh.advance(item, last):
            console.print(f"[red]{item.file_name}: {item.error}[/red]")
    ready = [i for i in items if i.stage == "verified"]
    for stage in ("compressed", "skipped", "verified", "trashed"):
        count = sum(1 for i in items if i.stage == stage)
        if count:
            console.print(f"{stage}: {count}")
    if trash and apply and ready and (yes or typer.confirm(f"Move {len(ready)} verified originals to the Google Photos trash (restorable for 60 days)?")):
        for item in track(ready, description="trashing"):
            if not sh.advance(item, "trashed"):
                console.print(f"[red]{item.file_name}: {item.error}[/red]")
    _summary(sh.state)


@app.command()
def process(
    ctx: typer.Context,
    min_size_mb: MinSize = 5.0,
    album: AlbumOpt = None,
    ids: IdOpt = None,
    limit: LimitOpt = None,
    apply: Annotated[bool, typer.Option("--apply", help="Actually upload. Without it, only download and compress locally.")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip the confirmation prompt.")] = False,
    quality: QualityOpt = 88,
    max_mp: MaxMpOpt = 16.0,
    min_saving: MinSavingOpt = 0.20,
    saver: SaverOpt = False,
    hash_check: HashOpt = True,
) -> None:
    """Download, compress, upload, re-album and verify. Never trashes anything."""
    st: Settings = ctx.obj
    _batch(st, _cfg(st, quality, max_mp, min_saving, saver, hash_check), _sel(min_size_mb, album, ids, limit), apply=apply, trash=False, yes=yes)


@app.command()
def run(
    ctx: typer.Context,
    min_size_mb: MinSize = 5.0,
    album: AlbumOpt = None,
    ids: IdOpt = None,
    limit: LimitOpt = None,
    apply: Annotated[bool, typer.Option("--apply", help="Actually upload and trash. Without it, local dry run.")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip both confirmation prompts.")] = False,
    quality: QualityOpt = 88,
    max_mp: MaxMpOpt = 16.0,
    min_saving: MinSavingOpt = 0.20,
    saver: SaverOpt = False,
    hash_check: HashOpt = True,
) -> None:
    """The whole loop: process, then trash the originals that passed every check."""
    st: Settings = ctx.obj
    _batch(st, _cfg(st, quality, max_mp, min_saving, saver, hash_check), _sel(min_size_mb, album, ids, limit), apply=apply, trash=True, yes=yes)


@app.command()
def trash(
    ctx: typer.Context,
    ids: IdOpt = None,
    apply: Annotated[bool, typer.Option("--apply")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y")] = False,
) -> None:
    """Move originals of verified photos to the Google Photos trash (restorable for 60 days)."""
    st: Settings = ctx.obj
    sh = st.shrinker(Config(st.workdir))
    ready = [i for i in sh.state.items.values() if i.stage == "verified" and (not ids or i.media_key in ids)]
    if not ready:
        console.print("No verified photos waiting. Run 'process --apply' first.")
        return
    console.print(_items_table(ready, f"{len(ready)} verified originals"))
    if not apply:
        console.print("[yellow]Dry run. Add --apply to trash them.[/yellow]")
        return
    if not yes:
        typer.confirm("Move these originals to the trash?", abort=True)
    for item in ready:
        console.print(f"{item.file_name}: " + ("trashed" if sh.advance(item, "trashed") else f"[red]{item.error}[/red]"))


@app.command()
def undo(
    ctx: typer.Context,
    ids: Annotated[list[str], typer.Option("--id", help="Original library media key. Repeatable.")],
    apply: Annotated[bool, typer.Option("--apply")] = False,
) -> None:
    """Revert a photo: restore the original from trash, then trash the compressed copy."""
    st: Settings = ctx.obj
    sh = st.shrinker(Config(st.workdir))
    for key in ids:
        item = sh.state.get(key)
        if not item:
            console.print(f"[red]{key}: not tracked[/red]")
            continue
        if not apply:
            console.print(f"Would undo {item.file_name} (stage {item.stage}). Add --apply.")
            continue
        sh.undo(item)
        console.print(f"{item.file_name}: undone")


@app.command()
def status(ctx: typer.Context) -> None:
    """Show tracked photos, their stage, and any errors."""
    st: Settings = ctx.obj
    state = State(st.workdir / "state.json")
    if not state.items:
        console.print("Nothing tracked yet.")
        return
    table = Table(title="state")
    for col in ("id (for --id)", "file", "stage", "original", "copy", "problem"):
        table.add_column(col, overflow="fold")
    for item in state.items.values():
        table.add_row(
            item.media_key, item.file_name, item.stage, human(item.size_bytes), human(item.new_size) if item.new_size else "-", item.error or item.note
        )
    console.print(table)
    _summary(state)


if __name__ == "__main__":
    app()
