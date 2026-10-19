"""Run the full pipeline, stages 0-6, then the audit.

    uv run python run_all.py            # run everything (idempotent; caches make it cheap)
    uv run python run_all.py 3          # run from stage 3 onward
    uv run python run_all.py 3 5        # run stages 3..5 only

Every stage is independently re-runnable and reads only the artifact before it.
"""
from __future__ import annotations

import importlib
import sys
import time

STAGES = [
    ("0", "pipeline.stage0_parse"),
    ("1", "pipeline.stage1_score"),          # also emits data/01-recruiters.jsonl
    ("2", "pipeline.stage2_boards"),
    ("3", "pipeline.stage3_fetch"),
    ("4", "pipeline.stage4_filter"),
    ("5", "pipeline.stage5_score_roles"),
    ("6", "pipeline.stage6_assemble"),
]


def main() -> int:
    lo = sys.argv[1] if len(sys.argv) > 1 else "0"
    hi = sys.argv[2] if len(sys.argv) > 2 else "6"
    for num, mod in STAGES:
        if not (lo <= num <= hi):
            continue
        print(f"\n{'='*70}\n=== STAGE {num}  ({mod})\n{'='*70}")
        t = time.time()
        importlib.import_module(mod).main()
        print(f"--- stage {num} done in {time.time() - t:.1f}s")
    print(f"\n{'='*70}\n=== AUDIT\n{'='*70}")
    from pipeline import audit
    return audit.main()


if __name__ == "__main__":
    sys.exit(main())
