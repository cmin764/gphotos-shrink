"""Offline tests: real Pillow on generated files, fake Google clients.

They prove the tool's logic and its safety rules, not that Google accepts the calls.
"""

from __future__ import annotations

import random
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import requests
import typer
from PIL import Image, ImageOps
from typer.testing import CliRunner

import gphotos_shrink as gs

ORIGINAL_KEY = "orig_media_key"
NEW_KEY = "copy_media_key"
ALBUM = gs.AlbumRef("album_key", "Colombia", shared=False)


def make_jpeg(path: Path, size: tuple[int, int] = (1600, 1200), *, mpo: bool = False) -> None:
    """Smooth gradient at maximum quality and 4:4:4, so re-encoding at 88 always saves well over 20%."""
    image = Image.linear_gradient("L").resize(size).convert("RGB")
    exif = Image.Exif()
    exif[0x010F] = "TestCam"
    if mpo:
        image.save(path, "MPO", save_all=True, append_images=[image.resize((160, 120))], exif=exif, quality=100)
    else:
        image.save(path, "JPEG", quality=100, subsampling=0, exif=exif)


# --------------------------------------------------------------------------- pure helpers


def test_dedup_key_is_unpadded_urlsafe_sha1() -> None:
    key = gs.dedup_key_of(bytes(range(20)))
    assert len(key) == gs.SHA1_B64_LEN
    assert "=" not in key


def test_safe_name() -> None:
    assert gs.safe_name("a b/ü.jpg") == "a_b__.jpg"


def test_original_url_prefers_untouched_original() -> None:
    payload = {"1": {"5": {"2": {"5": "https://edited", "6": "https://original"}}}}
    assert gs._original_url(payload) == "https://original"
    with pytest.raises(RuntimeError):
        gs._original_url({})
    with pytest.raises(RuntimeError):
        gs._original_url({"1": {"5": {"2": {"6": "not-a-url"}}}})


def test_google_host_check() -> None:
    assert gs._is_google_host("https://lh3.googleusercontent.com/x")
    assert not gs._is_google_host("https://evil.example.com/x")
    assert not gs._is_google_host("https://googleusercontent.com.evil.example/x")


def test_gain_map_detection(tmp_path: Path) -> None:
    plain, hdr = tmp_path / "plain.jpg", tmp_path / "hdr.jpg"
    plain.write_bytes(b"\xff\xd8 nothing special")
    hdr.write_bytes(b"\xff\xd8 xmlns:hdrgm=...")
    assert not gs.has_gain_map(plain)
    assert gs.has_gain_map(hdr)


def test_compress_strips_mpf_keeps_exif_and_resizes(tmp_path: Path) -> None:
    src, dst = tmp_path / "in.jpg", tmp_path / "out" / "out.jpg"
    make_jpeg(src, (2000, 1500), mpo=True)
    with Image.open(src) as probe:
        assert getattr(probe, "n_frames", 1) == 2
    width, height, quality = gs.compress_jpeg(src, dst, quality=85, max_mp=1.0, mtime_ms=1_500_000_000_000)
    assert quality == 85
    assert width * height <= 1_000_000
    with Image.open(dst) as out:
        assert out.format == "JPEG"
        assert getattr(out, "n_frames", 1) == 1
        assert out.size == (width, height)
        assert out.getexif()[0x010F] == "TestCam"
    assert dst.stat().st_mtime == 1_500_000_000
    assert not dst.with_name(dst.name + ".part").exists()


def test_compress_keeps_resolution_below_cap(tmp_path: Path) -> None:
    src, dst = tmp_path / "in.jpg", tmp_path / "out.jpg"
    make_jpeg(src, (800, 600))
    assert gs.compress_jpeg(src, dst, quality=85, max_mp=16.0) == (800, 600, 85)


def noisy_jpeg(path: Path) -> None:
    Image.frombytes("RGB", (1200, 900), random.Random(0).randbytes(1200 * 900 * 3)).save(path, "JPEG", quality=100)


def test_quality_steps_down_until_the_budget_fits(tmp_path: Path) -> None:
    src = tmp_path / "noise.jpg"
    noisy_jpeg(src)
    assert gs.compress_jpeg(src, tmp_path / "a.jpg", quality=85, max_mp=16.0, target_mb=100)[2] == 85
    assert gs.compress_jpeg(src, tmp_path / "b.jpg", quality=85, max_mp=16.0, target_mb=0.0001)[2] == 75
    mid = (tmp_path / "a.jpg").stat().st_size * 0.9 / 1e6
    used = gs.compress_jpeg(src, tmp_path / "c.jpg", quality=85, max_mp=16.0, target_mb=mid)[2]
    assert used < 85
    assert (tmp_path / "c.jpg").stat().st_size <= mid * 1e6


def test_strip_mpf_removes_trailer_and_keeps_pixels(tmp_path: Path) -> None:
    src = tmp_path / "in.jpg"
    make_jpeg(src, (800, 600), mpo=True)
    data = src.read_bytes()
    assert b"MPF\x00" in data
    stripped = gs.strip_mpf(data)
    assert b"MPF\x00" not in stripped
    assert len(stripped) < len(data)
    out = tmp_path / "out.jpg"
    out.write_bytes(stripped)
    with Image.open(src) as before, Image.open(out) as after:
        assert after.format == "JPEG"
        assert before.tobytes() == after.tobytes()
        assert before.getexif()[0x010F] == after.getexif()[0x010F]
    padded = data[:2] + b"\xff\xff" + data[2:]  # legal fill byte before the first marker
    assert b"MPF\x00" not in gs.strip_mpf(padded)
    with pytest.raises(ValueError):
        gs.strip_mpf(b"not a jpeg")
    with pytest.raises(ValueError):
        gs.strip_mpf(data[:60])


def test_patch_exif_dims_moves_nothing_else() -> None:
    exif = Image.Exif()
    exif[0x0100], exif[0x0101] = 7008, 4672
    sub = exif.get_ifd(0x8769)
    sub[0xA002], sub[0xA003] = 7008, 4672
    maker = b"MAKERNOTE" * 20
    sub[0x927C] = maker
    blob = exif.tobytes()
    patched = gs.patch_exif_dims(blob, 3464, 2309)
    assert len(patched) == len(blob)
    assert patched.index(maker) == blob.index(maker)  # offsets intact
    reread = Image.Exif()
    reread.load(patched)
    assert (reread[0x0100], reread[0x0101]) == (3464, 2309)
    assert (reread.get_ifd(0x8769)[0xA002], reread.get_ifd(0x8769)[0xA003]) == (3464, 2309)
    assert gs.patch_exif_dims(b"garbage", 1, 1) == b"garbage"


def test_compress_keeps_xmp_and_makernote_bytes(tmp_path: Path) -> None:
    src, dst = tmp_path / "in.jpg", tmp_path / "out.jpg"
    exif = Image.Exif()
    exif[0x010F] = "TestCam"
    exif.get_ifd(0x8769)[0x927C] = b"MAKERNOTE" * 20
    Image.linear_gradient("L").resize((1000, 800)).convert("RGB").save(src, "JPEG", exif=exif, xmp=b"<x:xmpmeta>rating</x:xmpmeta>", quality=95)
    gs.compress_jpeg(src, dst, quality=85, max_mp=0.5)
    with Image.open(src) as a, Image.open(dst) as b:
        assert b.info["xmp"] == a.info["xmp"]
        assert b.getexif().get_ifd(0x8769)[0x927C] == a.getexif().get_ifd(0x8769)[0x927C]


# --------------------------------------------------------------------------- outlier guard


def failed_checks(checks: list[list[Any]]) -> set[str]:
    return {c[0].removeprefix(gs.LOCAL_PREFIX) for c in checks if not c[1]}


@pytest.fixture
def pair(tmp_path: Path) -> tuple[Path, Path, gs.Config]:
    src, dst = tmp_path / "in.jpg", tmp_path / "out.jpg"
    make_jpeg(src, (2400, 1800))
    cfg = gs.Config(tmp_path)
    gs.compress_jpeg(src, dst, quality=85, max_mp=cfg.max_mp)
    return src, dst, cfg


def test_guard_passes_a_good_copy(pair: tuple[Path, Path, gs.Config]) -> None:
    assert failed_checks(gs.check_copy(*pair)) == set()


def test_guard_trips_on_wrong_pixels(pair: tuple[Path, Path, gs.Config]) -> None:
    src, dst, cfg = pair
    with Image.open(src) as im:
        ImageOps.invert(im.convert("RGB")).save(dst, "JPEG", quality=85, exif=im.getexif())
    assert failed_checks(gs.check_copy(src, dst, cfg)) == {"pixels match original"}


def test_guard_trips_on_lost_metadata(pair: tuple[Path, Path, gs.Config]) -> None:
    src, dst, cfg = pair
    with Image.open(src) as im:
        im.save(dst, "JPEG", quality=85)
    assert failed_checks(gs.check_copy(src, dst, cfg)) == {"camera metadata identical"}


def test_guard_trips_on_wrong_shape(pair: tuple[Path, Path, gs.Config]) -> None:
    src, dst, cfg = pair
    with Image.open(src) as im:
        im.resize((1200, 1200)).save(dst, "JPEG", quality=85, exif=im.getexif())
    assert "aspect ratio kept" in failed_checks(gs.check_copy(src, dst, cfg))


def test_guard_trips_on_oversize(pair: tuple[Path, Path, gs.Config]) -> None:
    src, dst, cfg = pair
    cfg.target_mb = 0.001
    assert failed_checks(gs.check_copy(src, dst, cfg)) == {"within size budget"}
    assert "smaller than original" in failed_checks(gs.check_copy(dst, src, cfg))


def test_strip_mode_copy_passes_the_guard(tmp_path: Path) -> None:
    src, dst = tmp_path / "in.jpg", tmp_path / "out.jpg"
    make_jpeg(src, (800, 600), mpo=True)
    cfg = gs.Config(tmp_path, mode=gs.Mode.STRIP)
    gs.make_copy(src, dst, cfg)
    assert failed_checks(gs.check_copy(src, dst, cfg)) == set()
    plain = tmp_path / "plain.jpg"
    make_jpeg(plain, (800, 600))
    with pytest.raises(gs.SkipItem):
        gs.make_copy(plain, tmp_path / "x.jpg", cfg)


# --------------------------------------------------------------------------- album specs


ALBUMS = [gs.AlbumRef("k1", "Colombia", False), gs.AlbumRef("k2", "Peru", True), gs.AlbumRef("k3", "peru", False)]


def test_resolve_album_forms(monkeypatch: pytest.MonkeyPatch) -> None:
    assert gs.resolve_album(ALBUMS, "colombia").key == "k1"
    assert gs.resolve_album(ALBUMS, "k1").key == "k1"
    assert gs.resolve_album(ALBUMS, "https://photos.google.com/u/1/album/k1").key == "k1"
    assert gs.resolve_album(ALBUMS, "https://photos.google.com/share/k1?key=abc").key == "k1"
    monkeypatch.setattr(requests, "head", lambda *_, **__: SimpleNamespace(url="https://photos.google.com/share/k1?key=z"))
    assert gs.resolve_album(ALBUMS, "https://photos.app.goo.gl/AbCd").key == "k1"


def test_resolve_album_errors() -> None:
    with pytest.raises(ValueError, match="2 albums are titled"):
        gs.resolve_album(ALBUMS, "Peru")
    with pytest.raises(ValueError, match="Did you mean: Colombia"):
        gs.resolve_album(ALBUMS, "Colombi")
    with pytest.raises(ValueError, match="no album key"):
        gs.resolve_album(ALBUMS, "https://photos.google.com/photo/xyz")


def test_wait_for() -> None:
    calls: list[int] = []

    def second_time() -> bool:
        calls.append(1)
        return len(calls) == 2

    assert gs._wait_for(second_time, attempts=3, delay=0)
    assert not gs._wait_for(lambda: False, attempts=2, delay=0)


# --------------------------------------------------------------------------- state


def test_state_roundtrip_and_new_keys(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    state = gs.State(path)
    state.put(gs.Item("k1", "d1", "a.jpg", 10, 10, 1, 1, 0, new_media_key="n1"))
    reloaded = gs.State(path)
    assert reloaded.get("k1") is not None
    assert reloaded.new_keys() == {"n1"}
    assert not path.with_suffix(".tmp").exists()


# --------------------------------------------------------------------------- pipeline with fake Google


class FakeApi:
    def __init__(self, library: Library) -> None:
        self._lib = library
        self.bearer_token = "token"
        self.trashed: list[str] = []
        self.restored: list[str] = []
        self.added: list[tuple[str, list[str]]] = []

    def get_download_urls(self, media_key: str) -> dict[str, Any]:
        return {"1": {"5": {"2": {"6": f"https://lh3.googleusercontent.com/{media_key}"}}}}

    def move_remote_media_to_trash(self, dedup_keys: list[str]) -> dict[str, Any]:
        self.trashed += dedup_keys
        self._lib.set_trashed(dedup_keys[0], trashed=True)
        return {}

    def restore_from_trash(self, dedup_keys: list[str]) -> dict[str, Any]:
        self.restored += dedup_keys
        self._lib.set_trashed(dedup_keys[0], trashed=False)
        return {}

    def add_media_to_album(self, album_media_key: str, media_keys: list[str]) -> dict[str, Any]:
        self.added.append((album_media_key, media_keys))
        return {}

    def set_favorite(self, dedup_key: str, is_favorite: bool) -> dict[str, Any]:
        return {}

    def set_archived(self, dedup_keys: list[str], is_archived: bool) -> dict[str, Any]:
        return {}

    def set_item_caption(self, dedup_key: str = "", caption: str = "") -> None:
        return None


class Library:
    """A tiny stand-in for gpmc: a Client facade over a SQLite file shaped like its cache."""

    COLUMNS = (
        "media_key, dedup_key, file_name, size_bytes, quota_charged_bytes, width, height, utc_timestamp, caption, "
        "is_favorite, is_archived, is_original_quality, make, model, trash_timestamp, origin, duration, "
        "is_micro_video, is_edited, is_locked"
    )

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.api = FakeApi(self)
        self.uploads: list[Path] = []
        with self._conn() as conn:
            conn.execute(f"CREATE TABLE remote_media ({self.COLUMNS})")

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def add(self, media_key: str, dedup_key: str, name: str, size: int, utc_ms: int) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO remote_media VALUES (?,?,?,?,?,4000,3000,?,NULL,0,0,0,NULL,NULL,NULL,'self',NULL,0,0,0)",
                (media_key, dedup_key, name, size, size, utc_ms),
            )

    def set_trashed(self, dedup_key: str, *, trashed: bool) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE remote_media SET trash_timestamp = ? WHERE dedup_key = ?", (1 if trashed else None, dedup_key))

    def update_cache(self, show_progress: bool = True) -> None:
        return None

    def upload(self, target: dict[Path, dict[str, Any]], **_: Any) -> dict[str, str]:
        (path,) = target
        self.uploads.append(path)
        self.add(NEW_KEY, gs.dedup_key_of(gs.sha1_file(path)), target[path]["filename"], path.stat().st_size, 1_700_000_000_000)
        return {str(path.resolve()): NEW_KEY}


class FakeResponse:
    status_code = 200

    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def raise_for_status(self) -> None:
        return None

    def iter_content(self, _: int) -> Iterator[bytes]:
        yield self._body


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """A library with one big photo in one album, plus a Shrinker wired to the fakes."""
    original = tmp_path / "source.jpg"
    make_jpeg(original, (2400, 1800))
    body = original.read_bytes()

    lib = Library(tmp_path / "storage.db")
    lib.add(ORIGINAL_KEY, gs.dedup_key_of(gs.sha1_file(original)), "IMG_1.JPG", len(body), 1_700_000_000_000)

    albums = {ALBUM.key: {ORIGINAL_KEY}}
    monkeypatch.setattr(gs, "WAIT_DELAY", 0)
    monkeypatch.setattr(requests, "get", lambda *_, **__: FakeResponse(body))
    monkeypatch.setattr(gs, "list_albums", lambda _web: [ALBUM])
    monkeypatch.setattr(gs, "album_media_keys", lambda _web, key: set(albums[key]))

    def add_payload(*, item_media_keys: list[str], album_media_key: str) -> SimpleNamespace:
        def execute(_web: object) -> SimpleNamespace:
            albums[album_media_key] |= set(item_media_keys)
            return SimpleNamespace(success=True)

        return SimpleNamespace(execute=execute)

    monkeypatch.setattr(gs, "AddItemsToExistingAlbum", add_payload)

    cfg = gs.Config(tmp_path / "work")
    shrinker = gs.Shrinker(lib, object(), gs.State(cfg.workdir / "state.json"), gs.Cache(lib), cfg)
    return SimpleNamespace(lib=lib, shrinker=shrinker, albums=albums, cfg=cfg, body=body)


def pick(world: SimpleNamespace) -> gs.Item:
    (item,) = world.shrinker.select(gs.Selection(min_mb=0))
    assert isinstance(item, gs.Item)
    return item


def test_select_attaches_albums_and_skips_own_copies(world: SimpleNamespace) -> None:
    item = pick(world)
    assert [a["title"] for a in item.albums] == ["Colombia"]
    world.shrinker.state.put(gs.Item("other", "x", "o.jpg", 1, 1, 1, 1, 0, new_media_key=ORIGINAL_KEY))
    assert world.shrinker.select(gs.Selection(min_mb=0)) == []


def test_full_loop_trashes_only_after_verified_copy(world: SimpleNamespace) -> None:
    item = pick(world)
    assert world.shrinker.advance(item, "compressed")
    assert item.new_size < item.size_bytes

    assert world.shrinker.advance(item, "verified"), item.error
    assert item.new_media_key in world.albums[ALBUM.key]
    assert all(ok for _, ok, _ in item.checks)
    assert world.lib.api.trashed == []  # nothing destructive before the explicit trash step

    assert world.shrinker.advance(item, "trashed"), item.error
    assert world.lib.api.trashed == [item.dedup_key]
    assert gs.State(world.cfg.workdir / "state.json").get(ORIGINAL_KEY).stage == "trashed"  # type: ignore[union-attr]


def test_trash_refused_without_passing_checks(world: SimpleNamespace) -> None:
    item = pick(world)
    item.stage, item.new_media_key, item.new_dedup_key = "verified", NEW_KEY, "different"
    assert not world.shrinker.advance(item, "trashed")
    assert "verification checks" in item.error
    assert world.lib.api.trashed == []


def test_confirm_gate_stops_before_upload(world: SimpleNamespace) -> None:
    item = pick(world)
    assert not world.shrinker.advance(item, "verified", confirm=lambda *_: False)
    assert item.stage == "compressed"
    assert world.lib.uploads == []


def test_hash_mismatch_parks_item_at_candidate(world: SimpleNamespace) -> None:
    item = pick(world)
    item.dedup_key = gs.dedup_key_of(b"\x00" * 20)
    assert not world.shrinker.advance(item, "compressed")
    assert item.stage == "candidate"
    assert "SHA-1" in item.error


def test_small_gain_is_skipped(world: SimpleNamespace) -> None:
    world.cfg.min_saving = 0.999
    item = pick(world)
    assert world.shrinker.advance(item, "verified")
    assert item.stage == "skipped"
    assert world.lib.uploads == []


def test_undo_restores_original_then_drops_copy(world: SimpleNamespace) -> None:
    item = pick(world)
    item.stage, item.new_media_key, item.new_dedup_key = "trashed", NEW_KEY, "copy_dedup"
    calls: list[str] = []
    api = world.lib.api
    restore, trash = api.restore_from_trash, api.move_remote_media_to_trash

    def logged(name: str, call: Any) -> Any:
        def inner(dedup_keys: list[str]) -> Any:
            calls.append(name)
            return call(dedup_keys)

        return inner

    api.restore_from_trash = logged("restore", restore)
    api.move_remote_media_to_trash = logged("trash", trash)
    world.shrinker.undo(item)
    assert calls == ["restore", "trash"]  # the original is back before the copy goes
    assert world.lib.api.restored == [item.dedup_key]
    assert world.lib.api.trashed == ["copy_dedup"]
    assert item.stage == "rolled_back"


def test_quota_free_items_are_not_candidates(world: SimpleNamespace) -> None:
    with world.lib._conn() as conn:
        conn.execute("UPDATE remote_media SET quota_charged_bytes = 0")
    assert world.shrinker.select(gs.Selection(min_mb=0)) == []


def test_album_selection_by_key_and_link(world: SimpleNamespace) -> None:
    for spec in ("Colombia", "album_key", "https://photos.google.com/album/album_key"):
        assert len(world.shrinker.select(gs.Selection(min_mb=0, albums=[spec]))) == 1
    with pytest.raises(typer.Exit):
        world.shrinker.select(gs.Selection(min_mb=0, albums=["Nowhere"]))


def test_shared_albums_are_skipped_unless_included(world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gs, "list_albums", lambda _web: [gs.AlbumRef("album_key", "Colombia", shared=True)])
    assert world.shrinker.select(gs.Selection(min_mb=0)) == []
    assert world.shrinker.skipped_shared == 1
    assert len(world.shrinker.select(gs.Selection(min_mb=0, include_shared=True))) == 1


def test_outlier_blocks_upload(world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gs, "check_copy", lambda *_: [[gs.LOCAL_PREFIX + "pixels match original", False, "mean drift 40"]])
    item = pick(world)
    assert not world.shrinker.advance(item, "verified")
    assert "outlier" in item.error
    assert world.lib.uploads == []


def test_trash_refused_without_local_original(world: SimpleNamespace) -> None:
    item = pick(world)
    assert world.shrinker.advance(item, "verified"), item.error
    Path(item.original_path).unlink()
    assert not world.shrinker.advance(item, "trashed")
    assert "local backup" in item.error
    assert world.lib.api.trashed == []


def verified_item(world: SimpleNamespace) -> gs.Item:
    item = pick(world)
    assert world.shrinker.advance(item, "verified"), item.error
    return item


def test_unreadable_album_stops_the_run(world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_web: object, _key: str) -> set[str]:
        raise OSError("403")

    monkeypatch.setattr(gs, "album_media_keys", boom)
    with pytest.raises(RuntimeError, match="could not read album 'Colombia'"):
        world.shrinker.select(gs.Selection(min_mb=0))


def test_trash_rechecks_the_copy_right_now(world: SimpleNamespace) -> None:
    item = verified_item(world)
    world.lib.set_trashed(item.new_dedup_key, trashed=True)  # user deleted the copy after looking at it
    assert not world.shrinker.advance(item, "trashed")
    assert "no longer in the library" in item.error
    assert world.lib.api.trashed == []


def test_trash_rechecks_album_membership(world: SimpleNamespace) -> None:
    item = verified_item(world)
    world.albums[ALBUM.key].discard(item.new_media_key)
    assert not world.shrinker.advance(item, "trashed")
    assert "no longer in album" in item.error
    assert world.lib.api.trashed == []


def test_upload_refuses_a_file_that_changed_after_the_check(world: SimpleNamespace) -> None:
    item = pick(world)
    assert world.shrinker.advance(item, "compressed")
    make_jpeg(Path(item.compressed_path), (300, 200))
    assert not world.shrinker.advance(item, "uploaded")
    assert "changed after it was checked" in item.error
    assert world.lib.uploads == []


def test_undo_restores_even_if_the_trash_confirmation_lagged(world: SimpleNamespace) -> None:
    item = verified_item(world)
    item.trash_requested = True  # the trash call was made, the library had not caught up when the run ended
    world.shrinker.undo(item)
    assert world.lib.api.restored == [item.dedup_key]


def test_changed_settings_remake_an_unuploaded_copy(world: SimpleNamespace) -> None:
    item = pick(world)
    assert world.shrinker.advance(item, "compressed")
    first = item.new_size
    world.cfg.quality = 60
    assert world.shrinker.advance(item, "compressed")
    assert item.new_size < first
    assert item.note.endswith("Q60, no MPF")


def test_id_reopens_a_skipped_item(world: SimpleNamespace) -> None:
    world.cfg.min_saving = 0.999
    item = pick(world)
    world.shrinker.advance(item, "compressed")
    assert item.stage == "skipped"
    assert world.shrinker.select(gs.Selection(min_mb=0)) == []
    (again,) = world.shrinker.select(gs.Selection(min_mb=0, ids=[ORIGINAL_KEY]))
    assert again.stage == "candidate"


def test_errors_do_not_leak_signed_urls(world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_: object, **__: object) -> None:
        raise requests.HTTPError("403 for url: https://lh3.googleusercontent.com/x?sig=SECRET")

    monkeypatch.setattr(requests, "get", boom)
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    item = pick(world)
    assert not world.shrinker.advance(item, "downloaded")
    assert "SECRET" not in item.error


# --------------------------------------------------------------------------- CLI


def test_cli_help_and_empty_status(tmp_path: Path) -> None:
    runner = CliRunner()
    env = {"GPS_WORKDIR": str(tmp_path)}
    help_result = runner.invoke(gs.app, ["--help"], env=env)
    assert help_result.exit_code == 0
    for command in ("auth-check", "scan", "try", "process", "run", "trash", "undo", "status", "local"):
        assert command in help_result.output
    status_result = runner.invoke(gs.app, ["status"], env=env)
    assert status_result.exit_code == 0
    assert "Nothing tracked yet" in status_result.output


def test_cli_without_credentials_fails_cleanly(tmp_path: Path) -> None:
    result = CliRunner().invoke(gs.app, ["scan"], env={"GPS_WORKDIR": str(tmp_path), "GP_AUTH_DATA": ""})
    assert result.exit_code == 1
    assert "No gpmc auth data" in result.output


# --------------------------------------------------------------------------- CLI happy paths


@pytest.fixture
def cli(world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Invoke the real CLI with the Google clients swapped for the fakes."""
    cookies = world.cfg.workdir / "cookies.txt"
    cookies.parent.mkdir(parents=True, exist_ok=True)
    cookies.write_text("# Netscape HTTP Cookie File\n")
    cookies.chmod(0o600)
    monkeypatch.setattr(gs, "GpmcClient", lambda **_: world.lib)
    monkeypatch.setattr(gs, "GpwcClient", lambda *_, **__: object())
    env = {"GPS_WORKDIR": str(world.cfg.workdir), "GP_AUTH_DATA": "androidId=fake"}
    return lambda *args, **kwargs: CliRunner().invoke(gs.app, list(args), env=env, **kwargs)


def test_cli_scan_lists_candidate_and_writes_csv(cli: Any, world: SimpleNamespace, tmp_path: Path) -> None:
    csv_path = tmp_path / "out.csv"
    result = cli("scan", "--min-size-mb", "0", "--no-sync", "--csv", str(csv_path))
    assert result.exit_code == 0, result.output
    assert "IMG_1.JPG" in result.output
    assert "Colombia" in csv_path.read_text()


def test_cli_process_dry_run_uploads_nothing(cli: Any, world: SimpleNamespace) -> None:
    result = cli("process", "--min-size-mb", "0")
    assert result.exit_code == 0, result.output
    assert "Dry run" in result.output
    assert world.lib.uploads == []
    assert gs.State(world.cfg.workdir / "state.json").get(ORIGINAL_KEY).stage == "compressed"  # type: ignore[union-attr]


def test_cli_process_apply_then_trash_then_undo(cli: Any, world: SimpleNamespace) -> None:
    state_path = world.cfg.workdir / "state.json"

    result = cli("process", "--min-size-mb", "0", "--apply", "--yes")
    assert result.exit_code == 0, result.output
    assert len(world.lib.uploads) == 1
    assert gs.State(state_path).get(ORIGINAL_KEY).stage == "verified"  # type: ignore[union-attr]
    assert world.lib.api.trashed == []  # process never trashes

    assert "Dry run" in cli("trash").output
    assert world.lib.api.trashed == []

    result = cli("trash", "--apply", "--yes")
    assert result.exit_code == 0, result.output
    assert gs.State(state_path).get(ORIGINAL_KEY).stage == "trashed"  # type: ignore[union-attr]

    result = cli("undo", "--id", ORIGINAL_KEY, "--apply")
    assert result.exit_code == 0, result.output
    assert gs.State(state_path).get(ORIGINAL_KEY).stage == "rolled_back"  # type: ignore[union-attr]
    assert world.lib.api.restored  # the original came back

    status = cli("status")
    assert status.exit_code == 0
    assert "rolled_back" in status.output


def test_cli_run_apply_does_the_whole_loop(cli: Any, world: SimpleNamespace) -> None:
    result = cli("run", "--min-size-mb", "0", "--apply", "--yes")
    assert result.exit_code == 0, result.output
    assert gs.State(world.cfg.workdir / "state.json").get(ORIGINAL_KEY).stage == "trashed"  # type: ignore[union-attr]
    assert len(world.lib.api.trashed) == 1


def test_cli_auth_check(cli: Any) -> None:
    result = cli("auth-check")
    assert result.exit_code == 0, result.output
    assert "authenticated" in result.output


def test_cli_halts_at_first_outlier_unless_keep_going(cli: Any, world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    world.lib.add("second_key", "x" * 27, "IMG_2.JPG", len(world.body), 1_700_000_000_000)
    monkeypatch.setattr(gs, "check_copy", lambda *_: [[gs.LOCAL_PREFIX + "pixels match original", False, ""]])
    state_path = world.cfg.workdir / "state.json"

    result = cli("process", "--min-size-mb", "0", "--apply", "--yes")
    assert "Halted" in result.output
    assert len(gs.State(state_path).items) == 1
    assert world.lib.uploads == []

    result = cli("process", "--min-size-mb", "0", "--keep-going")
    assert "Halted" not in result.output
    assert len(gs.State(state_path).items) == 2  # the second photo was attempted too
    assert world.lib.uploads == []  # and nothing failed ever reached the upload


def test_cli_try_warns_before_touching_a_shared_album(cli: Any, world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gs, "list_albums", lambda _web: [gs.AlbumRef("album_key", "Colombia", shared=True)])
    result = cli("try", "--min-size-mb", "0", "--album", "Colombia", "--count", "1", input="n\nn\n")
    assert "Shared with others" in result.output
    assert world.lib.uploads == []


def test_cli_batch_reports_skipped_shared(cli: Any, world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gs, "list_albums", lambda _web: [gs.AlbumRef("album_key", "Colombia", shared=True)])
    result = cli("process", "--min-size-mb", "0")
    assert "--include-shared" in result.output
    assert cli("process", "--min-size-mb", "0", "--include-shared").exit_code == 0


def test_cli_local_needs_no_account(tmp_path: Path) -> None:
    src, out = tmp_path / "in.jpg", tmp_path / "out"
    make_jpeg(src, (2400, 1800))
    result = CliRunner().invoke(gs.app, ["local", str(src), "-o", str(out)], env={"GPS_WORKDIR": str(tmp_path / "w"), "GP_AUTH_DATA": ""})
    assert result.exit_code == 0, result.output
    assert (out / "in_compress.jpg").exists()
    assert "all ok" in result.output


def test_cli_local_folders_png_and_oversized(tmp_path: Path) -> None:
    root = tmp_path / "pics"
    (root / "Geo").mkdir(parents=True)
    make_jpeg(root / "a.JPG", (2400, 1800))
    make_jpeg(root / "Geo" / "b.jpeg", (2400, 1800))
    Image.effect_noise((1500, 1500), 90).convert("RGBA").save(root / "Geo" / "c.png")  # noise: cannot hit a tiny budget
    (root / "Geo" / "raw.ARW").write_bytes(b"x")
    out = root / "Compressed"  # inside the input on purpose: a rerun must not eat its own output
    args = ["local", str(root), "-o", str(out), "--target-mb", "0.2"]
    env = {"GPS_WORKDIR": str(tmp_path / "w"), "GP_AUTH_DATA": ""}
    for _ in range(2):
        result = CliRunner().invoke(gs.app, args, env=env)
        assert result.exit_code == 0, result.output
        assert sorted(p.relative_to(out).as_posix() for p in out.rglob("*.jpg")) == ["Geo/b_compress.jpg", "Geo/c_compress_oversized.jpg", "a_compress.jpg"]


# --------------------------------------------------------------------------- real photo (local only)

SAMPLE = Path(__file__).parent / "samples" / "CMN08911.JPG"
needs_sample = pytest.mark.skipif(not SAMPLE.exists(), reason="samples/CMN08911.JPG is gitignored; add a real Sony MPF JPEG to run these")


@needs_sample
def test_real_photo_default_compress(tmp_path: Path) -> None:
    cfg, dst = gs.Config(tmp_path), tmp_path / "out.jpg"
    width, height, _ = gs.make_copy(SAMPLE, dst, cfg)
    assert width * height <= cfg.max_mp * 1.001e6
    assert dst.stat().st_size <= cfg.target_mb * 1e6
    assert failed_checks(gs.check_copy(SAMPLE, dst, cfg)) == set()
    with Image.open(SAMPLE) as a, Image.open(dst) as b:
        assert b.info["xmp"] == a.info["xmp"]
        assert b.getexif().get_ifd(0x8769)[0x927C] == a.getexif().get_ifd(0x8769)[0x927C]
        assert b.getexif().get_ifd(0x8769)[0xA002] == width


@needs_sample
def test_real_photo_strip(tmp_path: Path) -> None:
    cfg, dst = gs.Config(tmp_path, mode=gs.Mode.STRIP), tmp_path / "out.jpg"
    gs.make_copy(SAMPLE, dst, cfg)
    assert not gs.has_mpf(dst)
    assert SAMPLE.stat().st_size - dst.stat().st_size > 900_000  # the 944 KB trailer
    assert failed_checks(gs.check_copy(SAMPLE, dst, cfg)) == set()


def test_cli_halt_blocks_the_trash_phase(cli: Any, world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    item = verified_item(world)  # photo A is already verified from an earlier run
    world.lib.add("second_key", "x" * 27, "IMG_2.JPG", len(world.body) + 1, 1_700_000_000_000)  # photo B, listed first
    monkeypatch.setattr(gs, "check_copy", lambda *_: [[gs.LOCAL_PREFIX + "pixels match original", False, ""]])
    result = cli("run", "--min-size-mb", "0", "--apply", "--yes")
    assert "Halted" in result.output
    assert world.lib.api.trashed == []
    assert gs.State(world.cfg.workdir / "state.json").get(item.media_key).stage == "verified"  # type: ignore[union-attr]
