"""How fast the vision model reads a frame (server/vision.py) — run it before and after changing the model.

    uv run python scripts/vision_probe.py photo.jpg
    uv run python scripts/vision_probe.py photo.jpg --model gpt-6-luna --effort none --runs 5

Uses OPENAI_API_KEY from .env. Prints each answer and its time; a frame from the page is 768–1024 px JPEG,
so try a photo of that size.
"""
from __future__ import annotations

import argparse
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import OPENAI_VISION_EFFORT, OPENAI_VISION_MODEL  # noqa: E402
from server.vision import Vision, VisionError  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("image", type=Path, help="a JPEG frame")
    parser.add_argument("--model", default=OPENAI_VISION_MODEL)
    parser.add_argument("--effort", default=OPENAI_VISION_EFFORT)
    parser.add_argument("--question", default="що ти бачиш?")
    parser.add_argument("--runs", type=int, default=3)
    args = parser.parse_args()
    jpeg = args.image.read_bytes()
    vision = Vision(model=args.model, effort=args.effort)
    print(f"model={args.model} effort={args.effort or 'default'} bytes={len(jpeg)}")
    times = []
    for run in range(args.runs):
        try:
            answer = vision.look(jpeg, args.question, language="українською")
        except VisionError as exc:
            sys.exit(f"vision failed: {exc}")
        times.append(answer.ms)
        print(f"look #{run + 1}: {answer.ms} ms — {answer.text}")
    print(f"look median {statistics.median(times):.0f} ms, max {max(times)} ms")
    line = vision.scene(jpeg, "", language="українською")
    print(f"scene: {line}")
    print(f"scene again (should be «без змін»): {vision.scene(jpeg, line or '', language='українською')}")


if __name__ == "__main__":
    main()
