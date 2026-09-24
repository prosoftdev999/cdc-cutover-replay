from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = Path(os.environ.get("TASK_DATA_ROOT", "/app/data"))
OUT = Path(os.environ.get("TASK_OUTPUT", "/app/output/recovered_cdc.json"))
sys.path[:0] = [str(DATA), str(HERE)]

from frames import load_streams
from replay import Engine, initial_physical_candidates


def main():
    checkpoint = json.loads((DATA / "connector_checkpoint.json").read_text())
    streams = load_streams(DATA, checkpoint["checkpoint_source_uuid"])
    survivors = {}
    rejected = []
    for index, physical in enumerate(initial_physical_candidates(DATA)):
        try:
            document = Engine(DATA, streams, physical=physical).run().output()
        except ValueError as exc:
            rejected.append((index, str(exc)))
            continue
        key = json.dumps(document, sort_keys=True, separators=(",", ":"))
        survivors.setdefault(key, document)
    if len(survivors) != 1:
        raise ValueError(
            f"expected one feed consistent with all evidence, found {len(survivors)}; "
            f"rejected candidates={rejected}"
        )
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(next(iter(survivors.values())), indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
