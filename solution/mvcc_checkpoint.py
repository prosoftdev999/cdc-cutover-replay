from __future__ import annotations

import copy
import json
from pathlib import Path

from evidence_codec import (
    iter_redo_operations,
    load_page_copies,
    load_redo_blocks,
    load_transaction_history,
    load_undo_records,
    parse_page_payload,
)


def _usable_pages(path: Path, capture_lsn: int):
    selected = {}
    for rec in load_page_copies(path):
        if not rec["checksum_ok"] or rec["page_lsn"] > capture_lsn:
            continue
        old = selected.get(rec["page_no"])
        if old is None or rec["page_lsn"] > old["page_lsn"]:
            selected[rec["page_no"]] = rec
    return {page_no: rec["payload"] for page_no, rec in selected.items()}


def _active_transactions(path: Path, view_lsn: int):
    return {
        rec["trx_id"]
        for rec in load_transaction_history(path)
        if rec["begin_lsn"] <= view_lsn and (rec["end_lsn"] == 0 or rec["end_lsn"] > view_lsn)
    }


def _visible(trx_id: int, view: dict) -> bool:
    if trx_id < int(view["up_limit_id"]):
        return True
    if trx_id >= int(view["low_limit_id"]):
        return False
    return trx_id not in view["_active_trx_ids"]


def _visible_version(head: dict, undo: dict, view: dict):
    row = dict(head["row"])
    trx_id = int(head["trx_id"])
    deleted = bool(head["delete_mark"])
    roll_ptr = int(head["roll_ptr"])
    seen = set()
    while not _visible(trx_id, view):
        if roll_ptr == 0 or roll_ptr in seen or roll_ptr not in undo:
            raise ValueError("broken undo chain before visible version")
        seen.add(roll_ptr)
        rec = undo[roll_ptr]
        row.update(rec["patch"])
        trx_id = rec["prev_trx_id"]
        deleted = rec["prev_delete"]
        roll_ptr = rec["prev_roll_ptr"]
    return None if deleted else row


def _redo_chains(root: Path):
    manifest = json.loads((root / "snapshots" / "redo_manifest.json").read_text())
    if int(manifest.get("format_version", 0)) != 2:
        raise ValueError("unsupported redo manifest")
    by_sequence = {}
    for name in manifest["mirror_files"]:
        for block in load_redo_blocks(root / "snapshots" / name):
            key = (
                block["sequence"], block["start_lsn"], block["end_lsn"],
                block["prev_crc32"], block["payload_crc32"], block["payload"],
            )
            by_sequence.setdefault(block["sequence"], {})[key] = block

    first = int(manifest["first_sequence"])
    last = int(manifest["last_sequence"])
    recovery_lsn = int(manifest["recovery_lsn"])
    chains = []

    def walk(sequence, expected_lsn, prev_crc, chain):
        if sequence > last:
            if expected_lsn - 1 == recovery_lsn:
                chains.append(chain)
            return
        for block in by_sequence.get(sequence, {}).values():
            if block["start_lsn"] != expected_lsn or block["prev_crc32"] != prev_crc:
                continue
            if block["end_lsn"] < block["start_lsn"] or block["end_lsn"] > recovery_lsn:
                continue
            walk(sequence + 1, block["end_lsn"] + 1, block["payload_crc32"], chain + [block])

    walk(first, int(manifest["start_lsn"]), int(manifest["base_prev_crc32"]), [])
    if not chains:
        raise ValueError("no complete redo branch reaches recovery_lsn")
    return chains


def recover_checkpoint_candidates(root: Path, physical: dict, layouts: dict):
    manifest_path = root / "snapshots" / "mvcc_manifest.json"
    if not manifest_path.exists():
        return [copy.deepcopy(physical)]
    manifest = json.loads(manifest_path.read_text())
    if int(manifest.get("format_version", 0)) != 1:
        raise ValueError("unsupported MVCC manifest")

    page_layouts = {
        int(tag): layouts[object_id]
        for tag, object_id in manifest["objects"].items()
    }
    page_bytes = _usable_pages(
        root / "snapshots" / manifest["page_capture"],
        int(manifest["capture_lsn"]),
    )
    base_pages = {
        page_no: parse_page_payload(payload, page_layouts)
        for page_no, payload in page_bytes.items()
    }
    expected_pages = {int(page_no) for page_no in manifest["pages"]}
    if not expected_pages <= set(base_pages):
        raise ValueError(f"missing usable MVCC pages: {sorted(expected_pages - set(base_pages))}")

    undo = load_undo_records(root / "snapshots" / manifest["undo_capture"])
    view = dict(manifest["read_view"])
    view["_active_trx_ids"] = _active_transactions(
        root / "snapshots" / manifest["trx_history"], int(view["view_lsn"])
    )
    objects = {int(tag): object_id for tag, object_id in manifest["objects"].items()}

    states = {}
    for chain in _redo_chains(root):
        pages = copy.deepcopy(base_pages)
        page_order = None
        seen_mtrs = None
        try:
            page_order = {page_no: list(page.keys()) for page_no, page in pages.items()}
            seen_mtrs = set()

            def field_type(page_no, slot, ordinal):
                order = page_order.get(int(page_no))
                page = pages.get(int(page_no))
                if page is None or order is None or int(slot) >= len(order):
                    raise ValueError(f"redo targets unavailable page/slot {page_no}:{slot}")
                object_tag, _ = order[int(slot)]
                columns, types = page_layouts[int(object_tag)]
                if int(ordinal) >= len(columns):
                    raise ValueError("redo field ordinal out of range")
                return types[int(ordinal)]

            for block in chain:
                for op in iter_redo_operations(block["payload"], field_type):
                    if op["kind"] == "mtr_begin":
                        if op["mtr_id"] in seen_mtrs:
                            raise ValueError(f"duplicate mini-transaction {op['mtr_id']}")
                        seen_mtrs.add(op["mtr_id"])
                        continue

                    page_no = op["page_no"]
                    order = page_order.get(page_no)
                    page = pages.get(page_no)
                    if page is None or order is None:
                        raise ValueError(f"redo targets unavailable page {page_no}")

                    if op["kind"] == "reorg":
                        if op["count"] != len(order):
                            raise ValueError("redo reorganization slot count mismatch")
                        perm = op["permutation"]
                        if sorted(perm) != list(range(len(order))):
                            raise ValueError("invalid redo permutation")
                        page_order[page_no] = [order[i] for i in perm]
                        continue

                    if op["kind"] != "patch" or op["slot"] >= len(order):
                        raise ValueError("invalid redo patch")
                    key = order[op["slot"]]
                    object_tag, pk = key
                    columns, _ = page_layouts[int(object_tag)]
                    patch = {columns[ordinal]: value for ordinal, value in op["fields"]}
                    if "id" in patch and int(patch["id"]) != int(pk):
                        raise ValueError("redo patch changes primary key")
                    head = page[key]
                    head["row"].update(patch)
                    head["trx_id"] = op["trx_id"]
                    head["roll_ptr"] = op["roll_ptr"]
                    head["delete_mark"] = op["delete_mark"]

            updates = []
            for page_no in sorted(expected_pages):
                for (tag, pk), head in pages[page_no].items():
                    updates.append((objects[tag], pk, _visible_version(head, undo, view)))
        except (KeyError, ValueError):
            continue
        signature = tuple(sorted(
            (object_id, pk, None if row is None else tuple(sorted(row.items())))
            for object_id, pk, row in updates
        ))
        states.setdefault(signature, updates)

    if not states:
        raise ValueError("no redo branch is coherent with the saved MVCC evidence")

    candidates = []
    for updates in states.values():
        state = copy.deepcopy(physical)
        for object_id, pk, row in updates:
            if row is None:
                state[object_id].pop(pk, None)
            else:
                state[object_id][pk] = row
        candidates.append(state)
    return candidates
