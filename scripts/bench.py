"""Encoder x resolution x quality matrix on one photo, scored with SSIMULACRA2.

Reproduces the table in RESEARCH.md on any photo (a phone shot, a high-ISO frame)
before changing the compression defaults. Each candidate is scored against a
lossless downscale of the same photo at the same resolution.

Needs the ``ssimulacra2`` binary on PATH (brew install jpeg-xl). mozjpeg (``cjpeg``)
and jpegli (``cjpegli``) are optional and skipped when absent.

Usage: python scripts/bench.py PHOTO OUT_DIR
"""

from __future__ import annotations

import math
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image

MEGAPIXELS = (16, 12, 8)
QUALITIES = (90, 85, 80, 75)
MOZJPEG = shutil.which("cjpeg") or "/opt/homebrew/opt/mozjpeg/bin/cjpeg"
JPEGLI = shutil.which("cjpegli")


def score(ref: Path, candidate: Path) -> float:
    """SSIMULACRA2 of a candidate against its reference: 90 is invisible at 1:1, 80 to 85 is high quality."""
    out = subprocess.run(["ssimulacra2", str(ref), str(candidate)], capture_output=True, text=True, check=True).stdout
    return float(out.split()[-1])


def main(photo: Path, out_dir: Path) -> None:
    if not shutil.which("ssimulacra2"):
        sys.exit("ssimulacra2 not found on PATH (brew install jpeg-xl)")
    out_dir.mkdir(parents=True, exist_ok=True)
    with Image.open(photo) as im:
        im.seek(0)
        base = im.convert("RGB")
    print(f"{'MP':>3} {'encoder':<13} {'Q':>3} {'MB':>6} {'SSIMU2':>7}  file")
    for mp in MEGAPIXELS:
        scale = math.sqrt(mp * 1e6 / (base.width * base.height))
        img = base.resize((round(base.width * scale), round(base.height * scale)), Image.Resampling.LANCZOS)
        ref_png, ref_ppm = out_dir / f"ref_{mp}mp.png", out_dir / f"ref_{mp}mp.ppm"
        img.save(ref_png)
        img.save(ref_ppm)
        for quality in QUALITIES:
            candidates = [("pillow 4:2:0", out_dir / f"pillow_{mp}mp_q{quality}.jpg")]
            img.save(candidates[0][1], "JPEG", quality=quality, optimize=True, progressive=True, subsampling="4:2:0")
            if shutil.which(MOZJPEG):
                moz = out_dir / f"moz_{mp}mp_q{quality}.jpg"
                subprocess.run([MOZJPEG, "-quality", str(quality), "-outfile", str(moz), str(ref_ppm)], check=True)
                candidates.append(("mozjpeg", moz))
            if JPEGLI:
                jpegli = out_dir / f"jpegli_{mp}mp_q{quality}.jpg"
                subprocess.run([JPEGLI, str(ref_png), str(jpegli), "-q", str(quality)], check=True, capture_output=True)
                candidates.append(("jpegli", jpegli))
            for name, path in candidates:
                print(f"{mp:>3} {name:<13} {quality:>3} {path.stat().st_size / 1e6:>6.2f} {score(ref_png, path):>7.2f}  {path.name}")
        ref_ppm.unlink()


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(Path(sys.argv[1]), Path(sys.argv[2]))
