"""Offline tests: real Pillow on generated files, fake Google clients.

They prove the tool's logic and its safety rules, not that Google accepts the calls.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import requests
from PIL import Image
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
    width, height = gs.compress_jpeg(src, dst, quality=85, max_mp=1.0, mtime_ms=1_500_000_000_000)
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
    assert gs.compress_jpeg(src, dst, quality=85, max_mp=16.0) == (800, 600)


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
    world.shrinker.undo(item)
    assert world.lib.api.restored == [item.dedup_key]
    assert world.lib.api.trashed == ["copy_dedup"]
    assert item.stage == "rolled_back"


# --------------------------------------------------------------------------- CLI


def test_cli_help_and_empty_status(tmp_path: Path) -> None:
    runner = CliRunner()
    env = {"GPS_WORKDIR": str(tmp_path)}
    help_result = runner.invoke(gs.app, ["--help"], env=env)
    assert help_result.exit_code == 0
    for command in ("auth-check", "scan", "try", "process", "run", "trash", "undo", "status"):
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
    return lambda *args: CliRunner().invoke(gs.app, list(args), env=env)


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
