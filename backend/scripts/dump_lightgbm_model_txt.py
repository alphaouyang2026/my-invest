"""Show what LightGBM actually serialises into `model.txt`, and what does not.

Ticket 07 stores the trained model as LightGBM's native text format and binds a
sha256 to it, while separately promising that `deterministic=true` +
`force_row_wise=true` make results reproducible across thread counts. Those two
promises do not compose, and this script is the evidence:

    predictions bit-identical : True
    model.txt identical       : False
    diff                      : [num_threads: 1] -> [num_threads: 4]

`model.txt` has three sections. The header and the `Tree=N` blocks — the model
itself — are identical across thread counts. The trailing `parameters:` block is
a verbatim dump of the training parameter dictionary, and it carries runtime
settings (`num_threads`, `num_machines`, `local_listen_port`, `gpu_*`) alongside
genuine research parameters (`learning_rate`, the four seeds, `lambda_l2`). Only
the runtime half differs, and that alone changes the file's checksum.

So a file-level checksum is an integrity check, not a model identity. Anything
that wants "is this the same model" has to hash the file with the runtime
parameter lines excluded.

Run it against the project venv, which pins lightgbm 4.7.0:

    backend/.venv/Scripts/python.exe backend/scripts/dump_lightgbm_model_txt.py
    backend/.venv/Scripts/python.exe backend/scripts/dump_lightgbm_model_txt.py --out var/tmp

Nothing is written unless `--out` is given: the point is the diff, and dropping
model files into the working tree is a side effect nobody asked for.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
from pathlib import Path

import lightgbm as lgb
import numpy as np

# Deliberately tiny. The whole file has to stay readable in one screen, and the
# tree contents are not what this script is about.
ROWS, FEATURES, ROUNDS = 60, 3, 2

#: Everything ticket 07 pins for reproducibility, minus `num_threads` — which is
#: the variable under test here.
BASE_PARAMS = {
    "objective": "mse",
    "verbosity": -1,
    "deterministic": True,
    "force_row_wise": True,
    "num_leaves": 3,
    "min_data_in_leaf": 5,
    "seed": 1,
    "bagging_seed": 1,
    "feature_fraction_seed": 1,
    "data_random_seed": 1,
}


def train(num_threads: int) -> tuple[str, np.ndarray]:
    """Train on identical data with one thread-count difference."""
    # Seeded inside, not hoisted: both calls must see byte-identical input, and
    # sharing a generator across them would not.
    rng = np.random.default_rng(0)
    features = rng.normal(size=(ROWS, FEATURES))
    labels = features[:, 0] * 2.0 + rng.normal(scale=0.1, size=ROWS)
    booster = lgb.train(
        {**BASE_PARAMS, "num_threads": num_threads},
        lgb.Dataset(features, label=labels),
        num_boost_round=ROUNDS,
    )
    return booster.model_to_string(), booster.predict(features)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--out",
        type=Path,
        help="directory to write model_threads{1,4}.txt into (default: write nothing)",
    )
    parser.add_argument(
        "--threads",
        type=int,
        nargs=2,
        default=(1, 4),
        metavar=("A", "B"),
        help="the two thread counts to compare (default: 1 4)",
    )
    args = parser.parse_args()

    first, second = args.threads
    text_a, pred_a = train(first)
    text_b, pred_b = train(second)

    print(f"lightgbm {lgb.__version__}")
    print(f"predictions bit-identical : {np.array_equal(pred_a, pred_b)}")
    print(f"model.txt identical       : {text_a == text_b}")
    print(f"sha256 (num_threads={first})    : {sha256(text_a)}")
    print(f"sha256 (num_threads={second})    : {sha256(text_b)}")

    diff = list(
        difflib.unified_diff(
            text_a.splitlines(),
            text_b.splitlines(),
            fromfile=f"num_threads={first}",
            tofile=f"num_threads={second}",
            lineterm="",
            n=1,
        )
    )
    print(f"\n=== diff: num_threads={first} vs {second} ===")
    print("\n".join(diff) if diff else "(identical)")

    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        for threads, text in ((first, text_a), (second, text_b)):
            path = args.out / f"model_threads{threads}.txt"
            path.write_text(text, encoding="utf-8")
            print(f"\nwritten: {path} ({len(text)} bytes)")

    print(f"\n=== full model.txt (num_threads={first}) ===")
    print(text_a)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
