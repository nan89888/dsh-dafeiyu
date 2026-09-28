#!/usr/bin/env python3
"""Remove the WebP alpha matte from every packaged DSH animation frame.

Some imported clips encode their transparent canvas as nearly transparent
black, commonly RGBA ``(0, 0, 0, 1)``.  That is invisible in most viewers,
but macOS can retain it while a translucent Qt window moves and show a second
black character-shaped silhouette.  Runtime decoding also defends against
this; sanitising the files makes the packaged app robust before Qt sees them.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageChops


ROOT = Path(__file__).resolve().parents[1]
PET_ROOT = ROOT / "assets" / "pet"


def sanitize(path: Path, threshold: int) -> bool:
    with Image.open(path) as source:
        image = source.convert("RGBA")
    alpha = image.getchannel("A")
    matte_mask = alpha.point(lambda value: 255 if value <= threshold else 0)
    if matte_mask.getbbox() is None:
        return False

    before = image.getchannel("A")
    image.paste((0, 0, 0, 0), mask=matte_mask)
    if ImageChops.difference(before, image.getchannel("A")).getbbox() is None:
        return False
    # method=4 is materially faster for the 2,700-frame atlas and still
    # preserves the lossless alpha channel used by the runtime.
    image.save(path, "WEBP", quality=90, method=4)
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--threshold", type=int, default=32)
    args = parser.parse_args()
    if not 0 <= args.threshold <= 254:
        parser.error("threshold must be between 0 and 254")

    changed = 0
    total = 0
    for frame in sorted(PET_ROOT.rglob("*.webp")):
        total += 1
        changed += int(sanitize(frame, args.threshold))
    print(f"sanitized {changed}/{total} DSH animation frames (alpha <= {args.threshold})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
