#!/usr/bin/env python3
"""Shrink oversized Google Photos JPEGs without losing albums, and without losing the originals.

Google Photos has no API to replace a photo, so the only workable loop is:
download the original, compress it locally, upload the compressed copy, put it
in the same albums, verify, then move the original to the Google Photos trash
(restorable for at least 30 days). This tool runs that loop per photo as a
resumable state machine and refuses to trash anything it has not verified. Every
copy must also pass local outlier checks (size, aspect, metadata, pixel drift)
before upload, and a batch halts at the first photo that fails one.

Stages per photo: candidate, downloaded, compressed, uploaded, albums_done,
verified, trashed. Failures stop the photo at its current stage and are retried
on the next run. Nothing is ever deleted permanently.

Auth: see README.md. Two credentials are needed, because no single unofficial
client covers everything:
  GP_AUTH_DATA        gpmc (mobile API): library listing, download, upload, trash
  GP_COOKIES_FILE     gpwc (web API): album membership read and album writes

Typical session:
  gphotos-shrink auth-check
  gphotos-shrink local photo.jpg -o out/   # vet the technique offline, no account needed
  gphotos-shrink scan
  gphotos-shrink try --album <name|link> -c 2   # interactive, 2 photos of one album
  gphotos-shrink process --apply      # upload + verify everything, trash nothing
  gphotos-shrink trash --apply        # after you eyeballed the result
"""

from __future__ import annotations

import base64
import csv
import difflib
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import struct
import time
from collections.abc import Callable, Iterator
from contextlib import closing
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from functools import partial
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
from PIL import Image, ImageChops, ImageStat
from rich.console import Console
from rich.progress import track
from rich.table import Table

DEFAULT_WORKDIR = Path.home() / ".gphotos-shrink"
STAGES = ["candidate", "downloaded", "compressed", "uploaded", "albums_done", "verified", "trashed"]
SIDE_STATES = {"skipped", "rolled_back"}
SHA1_B64_LEN = 27  # urlsafe base64 of a 20 byte digest, no padding
GOOGLE_HOST_SUFFIXES = (".googleusercontent.com", ".google.com", ".googleapis.com")
TS_TOLERANCE_MS = 2000

# Chosen on a real 33 MP Sony JPEG: 8 MP Q85 scored SSIMULACRA2 83 at 1.8 MB, visually
# indistinguishable from the original at photobook scale. See RESEARCH.md section 4.
DEFAULT_QUALITY = 85
DEFAULT_MAX_MP = 8.0
DEFAULT_TARGET_MB = 2.0
QUALITY_STEPS = (82, 80, 78, 75)  # tried in order when a copy exceeds the size budget
SIZE_SLACK = 1.10
ASPECT_TOLERANCE = 0.005
DRIFT_THRESHOLD = 3.0  # mean absolute difference (0-255) of 256 px thumbnails
LOCAL_PREFIX = "local: "  # marks pre-upload checks so verification can keep them
WAIT_ATTEMPTS = 12  # eventual consistency: poll the library for about a minute
WAIT_DELAY = 5.0

console = Console(highlight=False)


class SkipItem(Exception):
    """The photo is fine as it is (for example, compression gains too little)."""


class Mode(StrEnum):
    """How a copy is made."""

    COMPRESS = "compress"  # downscale and re-encode, guaranteed small
    STRIP = "strip"  # lossless: drop the MPF block and its trailing preview image


@dataclass
class Config:
    """Compression and safety knobs shared by every command."""

    workdir: Path
    quality: int = DEFAULT_QUALITY
    max_mp: float = DEFAULT_MAX_MP
    min_saving: float = 0.20
    saver: bool = False
    verify_hash: bool = True
    target_mb: float = DEFAULT_TARGET_MB
    mode: Mode = Mode.COMPRESS
    keep_going: bool = False


@dataclass
class Selection:
    """Which photos a command works on."""

    min_mb: float = 5.0
    albums: list[str] = field(default_factory=list)
    ids: list[str] = field(default_factory=list)
    limit: int | None = None
    include_shared: bool = False


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
    made_with: str = ""  # settings that produced the copy, so a changed run re-makes it
    trash_requested: bool = False  # set before the trash call, so undo still restores after a lagging confirmation
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
               utc_timestamp, caption, is_favorite, is_archived
        FROM remote_media
        WHERE (trash_timestamp IS NULL OR trash_timestamp = 0)
          AND origin = 'self'
          AND duration IS NULL
          AND COALESCE(is_micro_video, 0) = 0
          AND COALESCE(is_edited, 0) = 0
          AND COALESCE(is_locked, 0) = 0
          AND size_bytes >= ?
          AND COALESCE(quota_charged_bytes, 0) > 0
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


def _wait_for(check: Callable[[], bool], attempts: int | None = None, delay: float | None = None) -> bool:
    """Retry a check until it passes or the attempts run out; True if it passed."""
    attempts = WAIT_ATTEMPTS if attempts is None else attempts
    delay = WAIT_DELAY if delay is None else delay
    for n in range(attempts):
        if check():
            return True
        if n < attempts - 1:
            time.sleep(delay)
    return False


def has_mpf(path: Path) -> bool:
    """Whether the JPEG carries a Multi-Picture Format block (what blocks Storage saver)."""
    with path.open("rb") as handle:
        return b"MPF\x00" in handle.read(256 * 1024)


def strip_mpf(buf: bytes) -> bytes:
    """Losslessly drop the APP2 MPF segment and everything after the primary image.

    Sony and Android cameras append a second, preview-sized JPEG after the main
    one. Google's Storage saver refuses such files, so cutting the MPF header and
    that trailer makes the photo eligible without touching a single pixel or any
    other metadata. Everything after the first start-of-scan is entropy data in
    which every 0xFF is stuffed, so the first end-of-image marker is the primary's.

    Raises:
        ValueError: The bytes are not a JPEG this parser understands.
    """
    if buf[:2] != b"\xff\xd8":
        raise ValueError("not a JPEG")
    out = bytearray(buf[:2])
    i = 2
    try:
        while True:
            if buf[i] != 0xFF:
                raise ValueError(f"bad marker at byte {i}")
            if buf[i + 1] == 0xFF:  # fill byte before a marker is legal
                i += 1
                continue
            marker = buf[i + 1]
            if marker == 0x01 or 0xD0 <= marker <= 0xD7:  # standalone markers carry no length
                out += buf[i : i + 2]
                i += 2
                continue
            length = struct.unpack(">H", buf[i + 2 : i + 4])[0]
            segment = buf[i : i + 2 + length]
            if marker == 0xDA:
                end = buf.index(b"\xff\xd9", i + 2 + length) + 2
                out += buf[i:end]
                return bytes(out)
            if not (marker == 0xE2 and segment[4:8] == b"MPF\x00"):
                out += segment
            i += 2 + length
    except (IndexError, struct.error) as exc:
        raise ValueError("truncated JPEG") from exc


def strip_file(src: Path, dst: Path, *, mtime_ms: int | None = None) -> None:
    """Write the MPF-free version of a JPEG, atomically."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".part")
    tmp.write_bytes(strip_mpf(src.read_bytes()))
    tmp.replace(dst)
    _touch(dst, mtime_ms)


def patch_exif_dims(exif: bytes, width: int, height: int) -> bytes:
    """Rewrite the pixel-dimension tags inside a raw EXIF blob, in place.

    Pillow's own EXIF re-serialization moves the Sony MakerNote, whose internal
    pointers are absolute offsets, so the camera data would silently break. The
    in-place patch overwrites only the four dimension values and keeps every
    other byte and offset identical. A blob that cannot be parsed is returned
    unchanged: stale dimensions are cosmetic, a damaged MakerNote is not.
    """
    blob = bytearray(exif)
    base = 6 if blob.startswith(b"Exif\x00\x00") else 0
    endian = "<" if blob[base : base + 2] == b"II" else ">"

    def entries(ifd: int) -> Iterator[tuple[int, int, int, int]]:
        count = struct.unpack_from(endian + "H", blob, base + ifd)[0]
        for n in range(count):
            pos = base + ifd + 2 + 12 * n
            tag, kind, cnt = struct.unpack_from(endian + "HHI", blob, pos)
            yield tag, kind, cnt, pos + 8

    targets = {0x0100: width, 0x0101: height, 0xA002: width, 0xA003: height}
    try:
        ifd0 = struct.unpack_from(endian + "I", blob, base + 4)[0]
        ifds = [ifd0] + [struct.unpack_from(endian + "I", blob, pos)[0] for tag, _, _, pos in entries(ifd0) if tag == 0x8769]
        for ifd in ifds:
            for tag, kind, cnt, value_pos in entries(ifd):
                if tag in targets and cnt == 1 and kind in (3, 4):
                    struct.pack_into(endian + ("H" if kind == 3 else "I"), blob, value_pos, targets[tag])
    except struct.error:
        return exif
    return bytes(blob)


def _touch(path: Path, mtime_ms: int | None) -> None:
    """Set the file time to the capture time so clients that fall back to it sort correctly."""
    if mtime_ms:
        seconds = mtime_ms / 1000
        os.utime(path, (seconds, seconds))


def compress_jpeg(
    src: Path, dst: Path, *, quality: int, max_mp: float, target_mb: float = DEFAULT_TARGET_MB, mtime_ms: int | None = None
) -> tuple[int, int, int]:
    """Re-encode a JPEG: cap resolution, lower quality, keep every metadata block.

    Re-encoding with Pillow drops the Multi-Picture Format (MPF) trailer that
    Sony and Android cameras append, which is exactly what makes Google's
    Storage saver refuse these files. EXIF (with the MakerNote byte-exact), XMP
    and the ICC profile are carried over verbatim. Pixels are never rotated, so
    the Orientation tag stays valid. If the result exceeds the budget, the
    quality steps down through QUALITY_STEPS; resolution is never reduced further.

    Args:
        src: Original JPEG.
        dst: Output path, written atomically.
        quality: Starting JPEG quality 1-100.
        max_mp: Downscale only when the image exceeds this many megapixels.
        target_mb: Size budget in MB (decimal) that triggers the quality step-down.
        mtime_ms: If given, set the output mtime to this capture time.

    Returns:
        Output (width, height, quality actually used).
    """
    with Image.open(src) as im:
        if getattr(im, "n_frames", 1) > 1:
            im.seek(0)
        extras: dict[str, Any] = {key: im.info[key] for key in ("xmp", "icc_profile") if im.info.get(key)}
        exif: bytes = im.info.get("exif", b"")
        if im.mode != "RGB":
            extras.pop("icc_profile", None)  # a CMYK or grayscale profile does not describe the RGB pixels written below
        out = im if im.mode == "RGB" else im.convert("RGB")
        width, height = out.size
        if width * height > max_mp * 1_000_000:
            scale = math.sqrt(max_mp * 1_000_000 / (width * height))
            width, height = int(width * scale), int(height * scale)
            out = out.resize((width, height), Image.Resampling.LANCZOS)
        if exif:
            extras["exif"] = patch_exif_dims(exif, width, height)
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + ".part")
        used = quality
        for used in [quality, *(q for q in QUALITY_STEPS if q < quality)]:
            out.save(tmp, "JPEG", quality=used, optimize=True, progressive=True, subsampling="4:2:0", **extras)
            if tmp.stat().st_size <= target_mb * 1_000_000:
                break
    tmp.replace(dst)
    _touch(dst, mtime_ms)
    return width, height, used


def make_copy(src: Path, dst: Path, cfg: Config, mtime_ms: int | None = None) -> tuple[int, int, int]:
    """Produce the smaller copy in the configured mode; returns (width, height, quality or 0 for strip)."""
    if cfg.mode is Mode.STRIP:
        if not has_mpf(src):
            raise SkipItem("no MPF block, nothing to strip")
        strip_file(src, dst, mtime_ms=mtime_ms)
        with Image.open(dst) as im:
            return im.size[0], im.size[1], 0
    return compress_jpeg(src, dst, quality=cfg.quality, max_mp=cfg.max_mp, target_mb=cfg.target_mb, mtime_ms=mtime_ms)


def _thumb(path: Path) -> Image.Image:
    """256 px square RGB sample.

    Full decode on purpose: JPEG draft mode on a 33 MP MPO shifted the colours by
    about 3/255 on a real Sony file, which made the pixel check useless.
    """
    with Image.open(path) as im:
        if getattr(im, "n_frames", 1) > 1:
            im.seek(0)
        return im.convert("RGB").resize((256, 256), Image.Resampling.BILINEAR)


def check_copy(src: Path, dst: Path, cfg: Config) -> list[list[Any]]:
    """Outlier guard: compare a fresh copy with its original before anything is uploaded.

    A bug in resizing, metadata handling or the encoder would otherwise be
    uploaded 1000 times before anyone noticed. Each check returns
    ``[label, ok, detail]``; any failure must stop the run.
    """
    checks: list[list[Any]] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        checks.append([LOCAL_PREFIX + label, bool(ok), detail])

    src_size, dst_size = src.stat().st_size, dst.stat().st_size
    check("smaller than original", dst_size < src_size, f"{human(src_size)} to {human(dst_size)}")
    if cfg.mode is Mode.COMPRESS:
        budget = cfg.target_mb * 1_000_000 * SIZE_SLACK
        check("within size budget", dst_size <= budget, f"{human(dst_size)} vs {cfg.target_mb:g} MB target")
    with Image.open(src) as a, Image.open(dst) as b:
        if getattr(a, "n_frames", 1) > 1:
            a.seek(0)
        ratio = (b.size[0] / b.size[1]) / (a.size[0] / a.size[1])
        check("aspect ratio kept", abs(ratio - 1) <= ASPECT_TOLERANCE, f"{a.size[0]}x{a.size[1]} to {b.size[0]}x{b.size[1]}")
        exif_a, exif_b = a.getexif(), b.getexif()
        check("orientation kept", exif_a.get(0x0112) == exif_b.get(0x0112))
        sub_a, sub_b = exif_a.get_ifd(0x8769), exif_b.get_ifd(0x8769)
        pairs = [
            ("Make", exif_a.get(0x010F), exif_b.get(0x010F)),
            ("Model", exif_a.get(0x0110), exif_b.get(0x0110)),
            ("DateTimeOriginal", sub_a.get(0x9003), sub_b.get(0x9003)),
            ("OffsetTimeOriginal", sub_a.get(0x9011), sub_b.get(0x9011)),
            ("LensModel", sub_a.get(0xA434), sub_b.get(0xA434)),
            ("MakerNote", sub_a.get(0x927C), sub_b.get(0x927C)),
            ("GPS", dict(exif_a.get_ifd(0x8825)), dict(exif_b.get_ifd(0x8825))),
            ("XMP", bool(a.info.get("xmp")), bool(b.info.get("xmp"))),
        ]
        changed = [name for name, before, after in pairs if before != after]
        check("camera metadata identical", not changed, ", ".join(changed))
    drift = max(ImageStat.Stat(ImageChops.difference(_thumb(src), _thumb(dst))).mean)
    check("pixels match original", drift <= DRIFT_THRESHOLD, f"mean drift {drift:.2f} of 255")
    return checks


def _original_url(payload: dict[str, Any]) -> str:
    """Pick the original-bytes URL out of gpmc's decoded download response."""
    try:
        node = payload["1"]["5"]["2"]
    except (KeyError, TypeError) as exc:
        raise RuntimeError("download response has no URL block") from exc
    # "6" is the untouched original, "5" is the version with edits applied.
    url = node.get("6") or node.get("5")
    if not isinstance(url, str) or not url.startswith("https://"):
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


ALBUM_URL = re.compile(r"photos\.google\.com/(?:u/\d+/)?(?:album|share)/([A-Za-z0-9_-]+)")
SHORT_LINK_HOSTS = ("photos.app.goo.gl", "goo.gl")


def resolve_album(albums: list[AlbumRef], spec: str) -> AlbumRef:
    """Turn what the user typed into exactly one album.

    Accepts an exact title (case-insensitive), a raw album media key, a
    photos.google.com album or share URL, or a photos.app.goo.gl short link.

    Raises:
        ValueError: Nothing or more than one album matches. The message says how to fix it.
    """
    spec = spec.strip()
    if spec.startswith("http"):
        if urlparse(spec).hostname in SHORT_LINK_HOSTS:
            spec = requests.head(spec, allow_redirects=True, timeout=15).url
        match = ALBUM_URL.search(spec)
        if not match:
            raise ValueError(f"no album key found in link {spec!r}")
        spec = match.group(1)
    for album in albums:
        if album.key == spec:
            return album
    same_title = [a for a in albums if a.title.casefold() == spec.casefold()]
    if len(same_title) == 1:
        return same_title[0]
    if same_title:
        keys = ", ".join(a.key for a in same_title)
        raise ValueError(f"{len(same_title)} albums are titled {spec!r}; pass one of these keys or its link instead: {keys}")
    close = difflib.get_close_matches(spec, [a.title for a in albums], n=3, cutoff=0.5)
    hint = f" Did you mean: {', '.join(close)}?" if close else ""
    raise ValueError(f"no album matches {spec!r}.{hint}")


def build_album_index(web: Any) -> dict[str, list[AlbumRef]]:
    """Map every media key to the albums that contain it.

    Raises RuntimeError if any album cannot be read. Deliberate shortcut: reads every album in full on each run. Ceiling is a few
    hundred albums before startup gets slow; cache per album by modified time
    when a library crosses that.
    """
    index: dict[str, list[AlbumRef]] = {}
    for album in list_albums(web):
        try:
            keys = album_media_keys(web, album.key)
        except Exception as exc:
            # Fail closed: a photo in an album we could not read would look album-less, so its copy
            # would skip that album and the original would then be trashed out of it.
            raise RuntimeError(f"could not read album {album.title!r} ({album.key}): {exc}") from exc
        for key in keys:
            index.setdefault(key, []).append(album)
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
        self.skipped_shared = 0

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
        wanted: set[str] = set()
        if sel.albums:
            if self.web is None:
                fail("--album needs cookies.txt (web API). See README.md, section Authentication.")
            try:
                wanted = {resolve_album(list_albums(self.web), spec).key for spec in sel.albums}
            except ValueError as exc:
                fail(str(exc))
        self.skipped_shared = 0
        picked: list[Item] = []
        for row in rows:
            key = row["media_key"]
            if key in own_copies:
                continue
            if sel.ids and key not in sel.ids:
                continue
            item = self.state.get(key) or item_from_row(row)
            if item.stage == "trashed" or (item.stage in SIDE_STATES and not sel.ids):
                continue
            if item.stage in SIDE_STATES:  # only reachable with --id: start the photo over
                item = item_from_row(row)
            if STAGES.index(item.stage) < STAGES.index("uploaded"):
                item.albums = [asdict(a) for a in index.get(key, [])]  # memberships may have changed since the last run
            if wanted and not wanted & {a["key"] for a in item.albums}:
                continue
            if not sel.include_shared and any(a["shared"] for a in item.albums):
                self.skipped_shared += 1
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
        if item.stage == "compressed" and not item.new_media_key and item.made_with != self._signature():
            item.stage = "downloaded"  # settings changed since the copy was made and nothing is uploaded yet: re-make it
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
                # Signed download URLs inside error text would otherwise land in state.json and the console.
                item.error = re.sub(r"https?://\S+", "<url>", f"{type(exc).__name__}: {exc}")
                self.state.put(item)
                return False
            item.stage, item.error = nxt, ""
            self.state.put(item)
        return True

    def _signature(self) -> str:
        c = self.cfg
        return f"{c.mode}:{c.quality}:{c.max_mp}:{c.target_mb}"

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
        mpf = has_mpf(src)
        width, height, quality = make_copy(src, dst, self.cfg, item.utc_ms)
        size = dst.stat().st_size
        if self.cfg.mode is Mode.COMPRESS and size > item.size_bytes * (1 - self.cfg.min_saving):
            dst.unlink(missing_ok=True)
            raise SkipItem(f"gain too small: {human(item.size_bytes)} to {human(size)} (needs {self.cfg.min_saving:.0%})")
        item.compressed_path = str(dst)
        item.made_with = self._signature()
        item.new_size = size
        item.new_dedup_key = dedup_key_of(sha1_file(dst))
        item.note = f"{width}x{height}" + (f" Q{quality}" if quality else " stripped") + (", had MPF" if mpf else ", no MPF")
        item.checks = check_copy(src, dst, self.cfg)
        failed = [c[0].removeprefix(LOCAL_PREFIX) for c in item.checks if not c[1]]
        if failed:
            raise RuntimeError(f"outlier, not uploading ({', '.join(failed)}); inspect {dst}")

    def _to_uploaded(self, item: Item) -> None:
        if item.new_media_key:
            return
        path = Path(item.compressed_path)
        if dedup_key_of(sha1_file(path)) != item.new_dedup_key:
            raise RuntimeError(f"{path} changed after it was checked, refusing to upload it")
        # use_quota is always on: the point is honest accounting, not the unlimited-storage trick.
        result = self.gp.upload(
            target={path: {"hash": sha1_file(path), "filename": item.file_name}},
            use_quota=True,
            saver=self.cfg.saver or self.cfg.mode is Mode.STRIP,  # a stripped file only gains if Google recompresses it
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

    def _row_where(self, media_key: str, predicate: Callable[[sqlite3.Row | None], bool]) -> sqlite3.Row | None:
        """Poll the library until the row satisfies the predicate (it is eventually consistent)."""

        def settled() -> bool:
            self.cache.sync(show_progress=False)
            return predicate(self.cache.row(media_key))

        _wait_for(settled)
        return self.cache.row(media_key)

    def _in_album(self, media_key: str, album: AlbumRef) -> bool:
        return media_key in album_media_keys(self.web, album.key)

    def _to_verified(self, item: Item) -> None:
        row = self._row_where(item.new_media_key, lambda r: r is not None and not r["trash_timestamp"])
        checks = [c for c in item.checks if c[0].startswith(LOCAL_PREFIX)]  # keep the pre-upload guard results

        def check(label: str, ok: bool, detail: str = "") -> None:
            checks.append([label, bool(ok), detail])

        check("copy exists in library", row is not None and not row["trash_timestamp"])
        if row is not None:
            if not (self.cfg.saver or self.cfg.mode is Mode.STRIP):  # Google recompresses saver uploads, so the bytes legitimately differ
                check("uploaded the checked bytes", row["dedup_key"] == item.new_dedup_key and row["size_bytes"] == item.new_size)
            check("copy is smaller", row["size_bytes"] < item.size_bytes, f"{human(item.size_bytes)} to {human(row['size_bytes'])}")
            check("capture time kept", abs((row["utc_timestamp"] or 0) - item.utc_ms) <= TS_TOLERANCE_MS)
            check("file name kept", row["file_name"] == item.file_name, row["file_name"])
        for album in (AlbumRef(**a) for a in item.albums):
            check(f"in album {album.title}", _wait_for(partial(self._in_album, item.new_media_key, album)))
        item.checks = checks
        failed = [c[0] for c in checks if not c[1]]
        if failed:
            raise RuntimeError("failed: " + ", ".join(failed))

    def _to_trashed(self, item: Item) -> None:
        # Invariants that must hold before the only destructive call in this tool.
        if not item.new_media_key or item.new_dedup_key == item.dedup_key or item.new_media_key == item.media_key:
            raise RuntimeError("no distinct verified copy, refusing to trash the original")
        if not item.checks or not all(c[1] for c in item.checks):
            raise RuntimeError("verification checks missing or failing, refusing to trash the original")
        # The downloaded original is the last local backup, so it must be intact before the cloud one goes.
        backup = Path(item.original_path)
        if not backup.is_file() or backup.stat().st_size != item.size_bytes:
            raise RuntimeError(f"local backup {backup} is missing or changed, refusing to trash the original")
        if len(item.dedup_key) == SHA1_B64_LEN and dedup_key_of(sha1_file(backup)) != item.dedup_key:
            raise RuntimeError(f"local backup {backup} no longer matches the original SHA-1, refusing to trash the original")
        # The checks on file are only as fresh as the last run, so look at the library again right now.
        row = self._row_where(item.new_media_key, lambda r: r is not None and not r["trash_timestamp"])
        if row is None or row["trash_timestamp"]:
            raise RuntimeError("the copy is no longer in the library (trashed or deleted?), refusing to trash the original")
        missing = [a["title"] for a in item.albums if not self._in_album(item.new_media_key, AlbumRef(**a))]
        if missing:
            raise RuntimeError(f"the copy is no longer in album {', '.join(missing)}, refusing to trash the original")
        item.trash_requested = True
        self.state.put(item)
        self.gp.api.move_remote_media_to_trash(dedup_keys=[item.dedup_key])
        row = self._row_where(item.media_key, lambda r: r is None or bool(r["trash_timestamp"]))
        if row is not None and not row["trash_timestamp"]:
            raise RuntimeError("trash call returned but the original is still in the library")

    # ---- undo

    def undo(self, item: Item) -> None:
        """Return to the pre-run state: restore the original, then drop the copy.

        Order matters: the original is restored first so there is never a moment
        with zero copies in the library.
        """
        if item.stage == "trashed" or item.trash_requested:
            self.gp.api.restore_from_trash(dedup_keys=[item.dedup_key])
            if self._row_where(item.media_key, lambda r: r is not None and not r["trash_timestamp"]) is None:
                raise RuntimeError("the original is not back in the library yet, not trashing the copy")
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
AlbumOpt = Annotated[
    list[str] | None,
    typer.Option("--album", "-a", help="Only photos in this album: title, album key, photos.google.com album/share URL or photos.app.goo.gl link. Repeatable."),
]
IdOpt = Annotated[list[str] | None, typer.Option("--id", help="Only this library media key. Repeatable. Also re-opens skipped or undone items.")]
LimitOpt = Annotated[int | None, typer.Option("--limit", "-n", help="Process at most N photos.")]
QualityOpt = Annotated[int, typer.Option("--quality", min=50, max=100, help="Starting JPEG quality; steps down if the copy exceeds --target-mb.")]
MaxMpOpt = Annotated[
    float, typer.Option("--max-mp", help="Downscale only if above this many megapixels. 8 covers A4 at 300 DPI; 12 gives about 240 DPI on 30x30 cm.")
]
TargetMbOpt = Annotated[float, typer.Option("--target-mb", min=0.2, help="Size budget per copy in MB. Quality steps down to stay under it.")]
ModeOpt = Annotated[
    Mode, typer.Option("--mode", help="compress: downscale and re-encode. strip: lossless, only drops the MPF block (uploads as Storage saver).")
]
KeepGoingOpt = Annotated[bool, typer.Option("--keep-going", help="Continue with the next photo after a failure. Default is to halt at the first one.")]
SharedOpt = Annotated[
    bool, typer.Option("--include-shared", help="Also process photos that sit in albums shared with others (adding the copy may notify members).")
]
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


def _cfg(
    st: Settings,
    quality: int,
    max_mp: float,
    min_saving: float,
    saver: bool,
    hash_check: bool,
    target_mb: float = DEFAULT_TARGET_MB,
    mode: Mode = Mode.COMPRESS,
    keep_going: bool = False,
) -> Config:
    return Config(st.workdir, quality, max_mp, min_saving, saver, hash_check, target_mb, mode, keep_going)


def _sel(min_mb: float, albums: list[str] | None, ids: list[str] | None, limit: int | None, include_shared: bool = False) -> Selection:
    return Selection(min_mb, albums or [], ids or [], limit, include_shared)


def _note_shared(sh: Shrinker) -> None:
    if sh.skipped_shared:
        console.print(f"[yellow]{sh.skipped_shared} photos skipped because they sit in shared albums. Add --include-shared to process them.[/yellow]")


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
    include_shared: SharedOpt = False,
    sync: Annotated[bool, typer.Option("--sync/--no-sync", help="Refresh the local library cache first.")] = True,
    csv_path: Annotated[Path | None, typer.Option("--csv", help="Also write the list to this CSV.")] = None,
) -> None:
    """List big JPEGs that are candidates (read-only, changes nothing).

    Photos edited in Google Photos, motion photos, videos, locked-folder items,
    photos shared with you, and photos already stored free (not charged to your
    quota) are excluded because re-uploading would lose or break something, or
    gain nothing. Photos in shared albums are listed only with --include-shared.
    """
    st: Settings = ctx.obj
    sh = st.shrinker(Config(st.workdir), need_web=False)
    if sync:
        sh.cache.sync()
    if sh.web is None:
        console.print("[yellow]No cookies configured: album column is empty. Albums are required for process/try/run.[/yellow]")
    items = sh.select(_sel(min_size_mb, album, None, limit, include_shared))
    _note_shared(sh)
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
    quality: QualityOpt = DEFAULT_QUALITY,
    max_mp: MaxMpOpt = DEFAULT_MAX_MP,
    target_mb: TargetMbOpt = DEFAULT_TARGET_MB,
    mode: ModeOpt = Mode.COMPRESS,
    min_saving: MinSavingOpt = 0.20,
    saver: SaverOpt = False,
    hash_check: HashOpt = True,
) -> None:
    """Interactive run on a few photos with approval at every irreversible step.

    For each photo: download and compress, show before and after, let you open
    both files, then ask before uploading. After upload it adds the copy to the
    same albums, runs the checks, and asks what to do with the original
    (trash it, keep both, or undo). Start with one album you own, passed as
    --album (title, key or link). Photos in shared albums are included here, but
    you are warned before anything is added to one. The session stops at the
    first photo that fails a check.
    """
    st: Settings = ctx.obj
    sh = st.shrinker(_cfg(st, quality, max_mp, min_saving, saver, hash_check, target_mb, mode))
    sh.cache.sync()
    pool = sh.select(_sel(min_size_mb, album, ids, None, include_shared=True))
    if not pool:
        fail("No candidates match.")
    picks = _pick_diverse(pool, count)
    console.print(_items_table(picks, "Test set"))
    for item in picks:
        console.rule(item.file_name)
        if not sh.advance(item, "compressed"):
            _halt(item)
            break
        if item.stage == "skipped":
            console.print(f"[yellow]skipped: {item.note}[/yellow]")
            continue
        _review(sh, item)
        shared = [a["title"] for a in item.albums if a["shared"]]
        if shared and not typer.confirm(
            f"Shared with others: {', '.join(shared)}. Adding the copy may notify members and its position in the album is not kept. Continue?",
            default=False,
        ):
            console.print("Nothing uploaded. State saved, rerun to resume.")
            continue
        if not typer.confirm("Upload the compressed copy and add it to the same albums?", default=False):
            console.print("Nothing uploaded. State saved, rerun to resume.")
            continue
        if not sh.advance(item, "verified"):
            _halt(item)
            console.print(f"The original is untouched. Run 'undo --id {item.media_key} --apply' to drop the copy.")
            break
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
            console.print("[green]original moved to trash (restorable for at least 30 days)[/green]" if ok else f"[red]{item.error}[/red]")
        elif choice == "u":
            sh.undo(item)
            console.print("Undone: copy trashed, original untouched.")
    _summary(sh.state)


def _halt(item: Item) -> None:
    """Explain why a run stopped at this photo and where to look."""
    console.print(f"[red]Stopping at {item.file_name}: {item.error}[/red]")
    if item.compressed_path:
        console.print(f"Original: {item.original_path}\nCopy: {item.compressed_path}")


def _summary(state: State) -> None:
    done = [i for i in state.items.values() if i.stage in ("verified", "trashed")]
    saved = sum(i.size_bytes - i.new_size for i in done)
    console.print(f"{len(done)} photos verified or trashed, {human(saved)} saved once the Google Photos trash is emptied.")


def _batch(st: Settings, cfg: Config, sel: Selection, *, apply: bool, trash: bool, yes: bool) -> None:
    sh = st.shrinker(cfg)
    sh.cache.sync()
    items = sh.select(sel)
    _note_shared(sh)
    if not items:
        console.print("Nothing to do.")
        return
    console.print(_items_table(items, f"{len(items)} photos, {human(sum(i.size_bytes for i in items))}"))
    if not apply:
        console.print("[yellow]Dry run: downloading and compressing locally only. Add --apply to upload and verify.[/yellow]")
    elif not yes:
        typer.confirm(f"Upload compressed copies for {len(items)} photos?", abort=True)
    last = "verified" if apply else "compressed"
    halted = False
    for item in track(items, description="processing"):
        if not sh.advance(item, last) and item.error:
            _halt(item)
            if not cfg.keep_going:
                halted = True
                console.print("[red]Halted: nothing after this photo was touched and nothing will be trashed. Fix or inspect it, or pass --keep-going.[/red]")
                break
    ready = [i for i in items if i.stage == "verified"]
    for stage in ("compressed", "skipped", "verified", "trashed"):
        count = sum(1 for i in items if i.stage == stage)
        if count:
            console.print(f"{stage}: {count}")
    if (
        trash
        and apply
        and not halted
        and ready
        and (yes or typer.confirm(f"Move {len(ready)} verified originals to the Google Photos trash (restorable for at least 30 days)?"))
    ):
        for item in track(ready, description="trashing"):
            if not sh.advance(item, "trashed"):
                console.print(f"[red]{item.file_name}: {item.error}[/red]")
                if not cfg.keep_going:
                    break
    _summary(sh.state)


@app.command()
def process(
    ctx: typer.Context,
    min_size_mb: MinSize = 5.0,
    album: AlbumOpt = None,
    ids: IdOpt = None,
    limit: LimitOpt = None,
    include_shared: SharedOpt = False,
    keep_going: KeepGoingOpt = False,
    apply: Annotated[bool, typer.Option("--apply", help="Actually upload. Without it, only download and compress locally.")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip the confirmation prompt.")] = False,
    quality: QualityOpt = DEFAULT_QUALITY,
    max_mp: MaxMpOpt = DEFAULT_MAX_MP,
    target_mb: TargetMbOpt = DEFAULT_TARGET_MB,
    mode: ModeOpt = Mode.COMPRESS,
    min_saving: MinSavingOpt = 0.20,
    saver: SaverOpt = False,
    hash_check: HashOpt = True,
) -> None:
    """Download, compress, upload, re-album and verify. Never trashes anything."""
    st: Settings = ctx.obj
    cfg = _cfg(st, quality, max_mp, min_saving, saver, hash_check, target_mb, mode, keep_going)
    _batch(st, cfg, _sel(min_size_mb, album, ids, limit, include_shared), apply=apply, trash=False, yes=yes)


@app.command()
def run(
    ctx: typer.Context,
    min_size_mb: MinSize = 5.0,
    album: AlbumOpt = None,
    ids: IdOpt = None,
    limit: LimitOpt = None,
    include_shared: SharedOpt = False,
    keep_going: KeepGoingOpt = False,
    apply: Annotated[bool, typer.Option("--apply", help="Actually upload and trash. Without it, local dry run.")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip both confirmation prompts.")] = False,
    quality: QualityOpt = DEFAULT_QUALITY,
    max_mp: MaxMpOpt = DEFAULT_MAX_MP,
    target_mb: TargetMbOpt = DEFAULT_TARGET_MB,
    mode: ModeOpt = Mode.COMPRESS,
    min_saving: MinSavingOpt = 0.20,
    saver: SaverOpt = False,
    hash_check: HashOpt = True,
) -> None:
    """The whole loop: process, then trash the originals that passed every check."""
    st: Settings = ctx.obj
    cfg = _cfg(st, quality, max_mp, min_saving, saver, hash_check, target_mb, mode, keep_going)
    _batch(st, cfg, _sel(min_size_mb, album, ids, limit, include_shared), apply=apply, trash=True, yes=yes)


@app.command()
def trash(
    ctx: typer.Context,
    ids: IdOpt = None,
    apply: Annotated[bool, typer.Option("--apply")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y")] = False,
) -> None:
    """Move originals of verified photos to the Google Photos trash (restorable for at least 30 days)."""
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
        done = sh.advance(item, "trashed")
        console.print(f"{item.file_name}: " + ("trashed" if done else f"[red]{item.error}[/red]"))
        if not done:
            break


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
def local(
    ctx: typer.Context,
    files: Annotated[list[Path], typer.Argument(help="JPEG files to shrink.", exists=True, dir_okay=False)],
    out: Annotated[Path, typer.Option("--out", "-o", help="Folder for the copies.")] = Path("shrunk"),
    quality: QualityOpt = DEFAULT_QUALITY,
    max_mp: MaxMpOpt = DEFAULT_MAX_MP,
    target_mb: TargetMbOpt = DEFAULT_TARGET_MB,
    mode: ModeOpt = Mode.COMPRESS,
) -> None:
    """Shrink local files with the exact pipeline and checks used on Google Photos. No account needed.

    Use it to vet the settings on your own photos before touching the library.
    Exits non-zero if any copy fails a check.
    """
    st: Settings = ctx.obj
    cfg = _cfg(st, quality, max_mp, 0.0, False, True, target_mb, mode)
    table = Table(title=f"{mode.value} to {out}")
    for col in ("file", "size", "result", "dims", "checks"):
        table.add_column(col)
    bad = 0
    for src in files:
        dst = out / f"{src.stem}_{mode.value}.jpg"
        try:
            width, height, used = make_copy(src, dst, cfg)
        except SkipItem as exc:
            table.add_row(src.name, human(src.stat().st_size), f"skipped: {exc}", "-", "-")
            continue
        checks = check_copy(src, dst, cfg)
        failed = [c[0].removeprefix(LOCAL_PREFIX) for c in checks if not c[1]]
        bad += bool(failed)
        table.add_row(
            src.name,
            human(src.stat().st_size),
            f"{human(dst.stat().st_size)}" + (f" Q{used}" if used else ""),
            f"{width}x{height}",
            "[red]FAIL: " + ", ".join(failed) + "[/red]" if failed else "[green]all ok[/green]",
        )
    console.print(table)
    if bad:
        raise typer.Exit(1)


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
