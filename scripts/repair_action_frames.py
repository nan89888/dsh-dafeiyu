#!/usr/bin/env python3
"""Repair short DSH action clips from the checked-in ds-local-pet references.

The first WebP import kept the right canvas but several one-pose actions were
encoded repeatedly, so a timer technically advanced while the character did
not visibly move.  This maintainer script normalizes the 306px reference PNGs
onto the runtime canvas and emits a small, smooth authored-motion cycle.  The
runtime still only reads the generated WebP frames; no image generation occurs
at runtime.
"""

from __future__ import annotations

import json
import math
import shutil
from pathlib import Path

from PIL import Image, ImageOps


ROOT = Path(__file__).resolve().parents[1]
REFERENCE = ROOT.parent / "work" / "ds-local-pet-ref" / "assets" / "processed" / "runtime" / "states"
PET = ROOT / "assets" / "pet"
MANIFEST = ROOT / "assets" / "pet-manifest.json"
CANVAS = (412, 344)


def strip_transparent_matte(image: Image.Image, threshold: int = 32) -> Image.Image:
    """Make low-alpha WebP matte pixels truly transparent.

    Several imported WebP frames contain RGBA (0, 0, 0, 1) across the
    nominally empty canvas.  That is technically non-zero alpha and is
    composited by AppKit/Qt as the black silhouette seen behind DSH.
    """
    image = image.convert("RGBA")
    pixels = image.load()
    for y in range(image.height):
        for x in range(image.width):
            r, g, b, a = pixels[x, y]
            if a <= threshold:
                pixels[x, y] = (0, 0, 0, 0)
    return image


def reference_frames(action: str, size: int = 238) -> list[Path]:
    """Return the authored frames for a directional action at one size."""
    directory = REFERENCE / action
    frames = sorted(directory.glob(f"*_{size}_*.png"))
    if not frames:
        frames = sorted(directory.glob("*.png"))
    if not frames:
        raise FileNotFoundError(f"no reference frames for {action}: {directory}")
    return frames


def normalize_ground_frame(
    source: Image.Image,
    target_height: int,
    bottom: int,
    center_x: float,
) -> Image.Image:
    """Put a 238px reference frame on the runtime canvas at the idle scale.

    The official side-view art uses a narrower source canvas than the front
    412x344 runtime frames.  Drawing those source canvases directly changes
    the apparent size during a walk.  Scale by the opaque character bounds,
    then align the opaque bottom to the front idle baseline so every gait
    frame has one stable size and ground anchor.
    """
    image = strip_transparent_matte(source)
    alpha = image.getchannel("A")
    bbox = alpha.point(lambda value: 255 if value > 4 else 0).getbbox()
    if bbox is None:
        return Image.new("RGBA", CANVAS, (0, 0, 0, 0))
    left, top, right, raw_bottom = bbox
    raw_height = max(1, raw_bottom - top)
    factor = target_height / raw_height
    resized = image.resize(
        (round(image.width * factor), round(image.height * factor)),
        Image.Resampling.LANCZOS,
    )
    resized_bbox = resized.getchannel("A").point(lambda value: 255 if value > 4 else 0).getbbox()
    if resized_bbox is None:
        return Image.new("RGBA", CANVAS, (0, 0, 0, 0))
    rleft, rtop, rright, rbottom = resized_bbox
    canvas = Image.new("RGBA", CANVAS, (0, 0, 0, 0))
    x = round(center_x - (rleft + rright) / 2)
    y = round(bottom - rbottom)
    canvas.alpha_composite(resized, (x, y))
    return canvas


def idle_metrics() -> tuple[int, int, float]:
    """Return the canonical visible height, baseline, and horizontal anchor."""
    idle = strip_transparent_matte(Image.open(PET / "idle" / "idle_001.webp"))
    bbox = idle.getchannel("A").point(lambda value: 255 if value > 4 else 0).getbbox()
    if bbox is None:
        raise RuntimeError("idle_001.webp has no opaque character bounds")
    return bbox[3] - bbox[1], bbox[3], (bbox[0] + bbox[2]) / 2


def normalize_visible_canvas(
    source: Image.Image,
    target_height: int,
    bottom: int,
    center_x: float,
) -> Image.Image:
    """Normalize a generated canvas by its visible character, not its canvas.

    Action source PNGs have pose-specific canvas sizes.  Without this final
    pass, standing actions are taller than idle while lying/dizzy actions are
    smaller even though the runtime canvas is nominally identical.
    """
    image = strip_transparent_matte(source)
    bbox = image.getchannel("A").point(lambda value: 255 if value > 4 else 0).getbbox()
    if bbox is None:
        return Image.new("RGBA", CANVAS, (0, 0, 0, 0))
    left, top, right, raw_bottom = bbox
    factor = target_height / max(1, raw_bottom - top)
    resized = image.resize(
        (max(1, round(image.width * factor)), max(1, round(image.height * factor))),
        Image.Resampling.LANCZOS,
    )
    resized_bbox = resized.getchannel("A").point(lambda value: 255 if value > 4 else 0).getbbox()
    if resized_bbox is None:
        return Image.new("RGBA", CANVAS, (0, 0, 0, 0))
    rleft, rtop, rright, rbottom = resized_bbox
    canvas = Image.new("RGBA", CANVAS, (0, 0, 0, 0))
    x = round(center_x - (rleft + rright) / 2)
    y = round(bottom - rbottom)
    canvas.alpha_composite(resized, (x, y))
    return canvas


def write_directional_cycle(action: str, source_action: str | None = None, *, frames: int | None = None) -> list[str]:
    source_frames = reference_frames(source_action or action, 238)
    # Match the visible opaque height and ground line of the shipped idle
    # canvas, not the transparent source canvas dimensions.
    target_height, bottom, center_x = idle_metrics()
    directory = PET / action
    directory.mkdir(parents=True, exist_ok=True)
    for old in directory.glob("*.webp"):
        old.unlink()
    paths: list[str] = []
    for index, source_path in enumerate(source_frames[:frames] if frames else source_frames):
        target = directory / f"{action}_{index + 1:03d}.webp"
        source = Image.open(source_path)
        # The reference pack contains a separate right-facing candidate whose
        # leg phase is not the mirror of the left cycle.  Use the authored
        # left sequence and mirror each corresponding frame instead: the feet
        # then alternate in the same cadence in both directions, with no
        # one-foot/right-only gait.
        if action.endswith("_right"):
            source = ImageOps.mirror(source)
        normalize_ground_frame(source, target_height, bottom, center_x).save(
            target, "WEBP", quality=88, method=4
        )
        paths.append(f"{action}/{target.name}")
    return paths


def reference_frame(action: str) -> Path:
    directory = REFERENCE / action
    frames = sorted(directory.glob("*_306*.png"))
    # The processed tree uses action_306.png and action_306_00.png names.
    if not frames:
        frames = sorted(directory.glob("*.png"))
    if not frames:
        raise FileNotFoundError(f"no reference frames for {action}: {directory}")
    return next((path for path in frames if "_306" in path.name and "_00" not in path.name), frames[0])


def paste_center(source: Image.Image) -> Image.Image:
    source = strip_transparent_matte(source)
    canvas = Image.new("RGBA", CANVAS, (0, 0, 0, 0))
    x = (CANVAS[0] - source.width) // 2
    y = CANVAS[1] - source.height
    canvas.alpha_composite(source, (x, y))
    return canvas


def motion_frame(base: Image.Image, index: int, action: str) -> Image.Image:
    phase = (index / 12.0) * math.tau
    if action == "angry":
        dx, dy, angle = math.sin(phase * 2) * 3.0, 0.0, math.sin(phase * 2) * 1.0
        scale = 1.0 + abs(math.sin(phase * 2)) * 0.008
    elif action == "dizzy":
        dx, dy, angle = math.sin(phase) * 2.0, math.cos(phase) * 1.0, math.sin(phase) * 5.0
        scale = 1.0
    elif action == "falling":
        dx, dy, angle = math.sin(phase) * 2.0, math.sin(phase) * 2.5, -7.0 + index * 1.15
        scale = 1.0
    elif action == "sweep":
        dx, dy, angle = math.sin(phase) * 2.0, 0.0, math.sin(phase) * 4.5
        scale = 1.0
    elif action == "talk":
        dx, dy, angle = 0.0, -abs(math.sin(phase * 2)) * 2.0, math.sin(phase * 2) * 0.8
        scale = 1.0
    elif action == "sleep":
        dx, dy, angle = 0.0, math.sin(phase) * 1.5, math.sin(phase) * 0.8
        scale = 1.0 + math.sin(phase) * 0.012
    elif action == "happy":
        dx, dy, angle = 0.0, -abs(math.sin(phase)) * 4.0, 0.0
        scale = 1.0 + math.sin(phase) * 0.012
    elif action == "eating":
        dx, dy, angle = math.sin(phase) * 1.2, -abs(math.sin(phase * 2)) * 2.0, math.sin(phase) * 0.5
        scale = 1.0
    else:
        dx = dy = angle = 0.0
        scale = 1.0

    transformed = base.resize((round(base.width * scale), round(base.height * scale)), Image.Resampling.BICUBIC)
    transformed = transformed.rotate(angle, resample=Image.Resampling.BICUBIC, expand=False)
    frame = Image.new("RGBA", CANVAS, (0, 0, 0, 0))
    x = round((CANVAS[0] - transformed.width) / 2 + dx)
    y = round(CANVAS[1] - transformed.height + dy)
    frame.alpha_composite(transformed, (x, y))
    return frame


def write_cycle(action: str, source_action: str | None = None, *, frames: int = 12) -> list[str]:
    source_action = source_action or action
    source = reference_frame(source_action)
    base = paste_center(Image.open(source))
    target_height, bottom, center_x = idle_metrics()
    directory = PET / action
    directory.mkdir(parents=True, exist_ok=True)
    for old in directory.glob("*.webp"):
        old.unlink()
    paths: list[str] = []
    for index in range(frames):
        target = directory / f"{action}_{index + 1:03d}.webp"
        # Method 4 keeps the maintainer repair quick while retaining the
        # transparent edges; the original source assets are still preserved
        # under work/ds-local-pet-ref.
        normalized = normalize_visible_canvas(
            motion_frame(base, index, action), target_height, bottom, center_x
        )
        normalized.save(target, "WEBP", quality=84, method=4)
        paths.append(f"{action}/{target.name}")
    return paths


def normalize_existing_cycle(action: str, frame_paths: list[str]) -> None:
    """Normalize authored throw/landing frames while preserving their order."""
    target_height, bottom, center_x = idle_metrics()
    for relative in frame_paths:
        path = PET / relative
        normalize_visible_canvas(Image.open(path), target_height, bottom, center_x).save(
            path, "WEBP", quality=88, method=4
        )


def main() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    clips = manifest["clips"]
    for action, source_action, count in (
        ("walk_left", "walk_side", 4),
        ("walk_right", "walk_side", 4),
        ("walk_start_left", "walk_start_left", 2),
        ("walk_start_right", "walk_start_left", 2),
        ("walk_stop_left", "walk_stop_left", 2),
        ("walk_stop_right", "walk_stop_left", 2),
    ):
        clips[action]["frames"] = write_directional_cycle(action, source_action, frames=count)
        clips[action]["frameMs"] = 120 if count == 4 else 1000
        # Transition clips are held by the walk state machine for their full
        # two-frame duration. Looping here prevents AnimationModel from
        # auto-clearing to idle a few timer ticks before the scheduled body or
        # idle hand-off, which was the source of the visible pose jump.
        clips[action]["loop"] = True
    # These were the clips whose files were visually duplicated in the bundle.
    for action in ("angry", "dizzy", "happy", "sleep", "sweep", "talk", "eating"):
        source_action = "eat" if action == "eating" else action
        generated_frames = write_cycle(action, source_action)
        clips[action]["frames"] = generated_frames
        # Non-core authored actions should stay visible for roughly two
        # seconds; the three established companion actions keep their source
        # timing and are excluded from this repair pass.
        clips[action]["frameMs"] = 110 if action == "sleep" else max(
            1, (2000 + len(generated_frames) - 1) // len(generated_frames)
        )
        clips[action]["loop"] = False
    for action in ("falling", "landing"):
        normalize_existing_cycle(action, list(clips[action]["frames"]))
    # The throw chain intentionally keeps a single procedural dizzy pose and
    # the original five-frame falling entrance; only the visible daily/action
    # clips above needed repair.
    clips["falling"]["frames"] = clips["falling"]["frames"][:5]
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("repaired", ", ".join(("angry", "dizzy", "falling", "happy", "sleep", "sweep", "talk", "eating")))


if __name__ == "__main__":
    main()
