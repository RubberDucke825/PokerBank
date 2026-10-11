"""Generate config/preflop_charts/hand_ranking.txt.

Ranking = heads-up all-in equity of each of the 169 classes vs one uniformly random hand,
estimated by Monte Carlo (fixed seed, `SAMPLES` runouts per class, using a representative combo).
Run: `python -m pn.profile.gen_hand_ranking [out_path]`.
"""

from __future__ import annotations

import random
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from pn.profile.handeval import evaluate
from pn.ranges import all_classes, card_to_int, class_combos

SAMPLES = 40_000
SEED = 20261001


def _equity(cls: str) -> tuple[str, float]:
    rng = random.Random(f"{SEED}:{cls}")
    combo = class_combos(cls)[0]
    hero = [card_to_int(combo[:2]), card_to_int(combo[2:])]
    deck = [c for c in range(52) if c not in hero]
    total = 0.0
    for _ in range(SAMPLES):
        draw = rng.sample(deck, 7)
        board = draw[2:]
        h = evaluate(hero + board)
        v = evaluate(draw[:2] + board)
        total += 1.0 if h > v else (0.5 if h == v else 0.0)
    return cls, total / SAMPLES


def generate() -> list[tuple[str, float]]:
    with ProcessPoolExecutor() as ex:
        res = list(ex.map(_equity, all_classes()))
    return sorted(res, key=lambda kv: kv[1], reverse=True)


def main() -> None:
    root = Path(__file__).resolve().parents[3]
    out = (
        Path(sys.argv[1]) if len(sys.argv) > 1 else root / "config/preflop_charts/hand_ranking.txt"
    )
    ranked = generate()
    lines = [
        "# 169 preflop classes, best first. Columns: rank class equity_vs_random_hu",
        f"# Monte Carlo, {SAMPLES} runouts/class, seed {SEED} (pn.profile.gen_hand_ranking).",
    ]
    lines += [f"{i + 1} {cls} {eq:.4f}" for i, (cls, eq) in enumerate(ranked)]
    out.write_text("\n".join(lines) + "\n")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
