from __future__ import annotations

import json

from evidence_codec import iter_valid_capture_events


def file_no(name: str) -> int:
    return int(name.rsplit(".", 1)[1])


def event_coord(event):
    return file_no(event["binlog_file"]), int(event["end_pos"])


def load_streams(root, checkpoint_source_uuid):
    seen = {}
    for path in sorted((root / "capture").glob("*/*.bin")):
        for event in iter_valid_capture_events(path):
            source = event.get("source_uuid", checkpoint_source_uuid)
            key = (
                source,
                event["binlog_file"],
                int(event["start_pos"]),
                int(event["end_pos"]),
            )
            canonical = json.dumps(event, sort_keys=True, separators=(",", ":"))
            if key in seen and seen[key][0] != canonical:
                raise ValueError(f"conflicting valid event at {key}")
            seen[key] = (canonical, event)

    streams = {}
    for (source, *_), (_, event) in seen.items():
        streams.setdefault(source, []).append(event)
    for events in streams.values():
        events.sort(
            key=lambda e: (
                file_no(e["binlog_file"]),
                int(e["start_pos"]),
                int(e["end_pos"]),
            )
        )
    return streams


def parse_gtid_set(text: str):
    out = {}
    for part in text.split(",") if text else []:
        bits = part.strip().split(":")
        values = out.setdefault(bits[0], set())
        for token in bits[1:]:
            if "-" in token:
                first, last = token.split("-", 1)
                values.update(range(int(first), int(last) + 1))
            elif token:
                values.add(int(token))
    return out


def gtid_in_set(gtid: str, parsed) -> bool:
    sid, number = gtid.rsplit(":", 1)
    return int(number) in parsed.get(sid, set())
