from __future__ import annotations

import base64
import binascii
import json
import struct
import zlib
from pathlib import Path

# This module decodes individual captured records. It does not assemble source
# history, maintain recovery state, choose redo branches, decide visibility, or build output.

FRAME_MAGIC = b"BLEV"
FRAME_HEADER = struct.Struct("<4sBBHIII")
PAGE_HEADER = struct.Struct("<4sHH")
PAGE_COPY_HEADER = struct.Struct("<IBBQII")
ROW_HEADER = struct.Struct("<BBQIH")
UNDO_HEADER = struct.Struct("<4sHH")
UNDO_RECORD_HEADER = struct.Struct("<IQBBIH")
TRX_HISTORY_HEADER = struct.Struct("<4sHH")
TRX_HISTORY_RECORD = struct.Struct("<QQQB3s")
REDO_FILE_HEADER = struct.Struct("<4sHH")
REDO_BLOCK_HEADER = struct.Struct("<IQQIII")
REDO_MTR_HEADER = struct.Struct("<IH")
REDO_PATCH_HEADER = struct.Struct("<BIHQIBB")
REDO_REORG_HEADER = struct.Struct("<BIH")


def decode_scalar(type_code: int, data: bytes, offset: int):
    if type_code == 8:
        return struct.unpack_from("<q", data, offset)[0], offset + 8
    if type_code == 2:
        return struct.unpack_from("<h", data, offset)[0], offset + 2
    if type_code in (245, 253):
        size = struct.unpack_from("<H", data, offset)[0]
        offset += 2
        raw = data[offset:offset + size]
        if len(raw) != size:
            raise ValueError("truncated variable-width value")
        text = raw.decode()
        return (json.loads(text) if type_code == 245 else text), offset + size
    raise ValueError(f"unsupported type {type_code}")


def decode_row_image(types, bitmap_hex: str, blob_b64: str):
    bitmap = int(bitmap_hex, 16)
    data = base64.b64decode(blob_b64)
    if len(data) < 8:
        raise ValueError("short row image")
    nullmask = struct.unpack_from("<Q", data, 0)[0]
    offset = 8
    values = {}
    for ordinal, type_code in enumerate(types):
        if not ((bitmap >> ordinal) & 1):
            continue
        if (nullmask >> ordinal) & 1:
            values[ordinal] = None
            continue
        values[ordinal], offset = decode_scalar(type_code, data, offset)
    if offset != len(data):
        raise ValueError("row image has trailing bytes")
    return values


def iter_valid_capture_events(path: Path):
    data = path.read_bytes()
    cursor = 0
    while True:
        offset = data.find(FRAME_MAGIC, cursor)
        if offset < 0:
            return
        if offset + FRAME_HEADER.size + 4 > len(data):
            return
        try:
            magic, version, flags, header_len, source_seq, payload_len, payload_crc = FRAME_HEADER.unpack_from(data, offset)
        except struct.error:
            return
        end = offset + FRAME_HEADER.size + payload_len + 4
        if version != 1 or flags != 0 or header_len != FRAME_HEADER.size or end > len(data):
            cursor = offset + 1
            continue
        payload = data[offset + FRAME_HEADER.size:offset + FRAME_HEADER.size + payload_len]
        frame_crc = struct.unpack_from("<I", data, offset + FRAME_HEADER.size + payload_len)[0]
        if (binascii.crc32(payload) & 0xFFFFFFFF) != payload_crc:
            cursor = offset + 1
            continue
        if (binascii.crc32(data[offset:offset + FRAME_HEADER.size] + payload) & 0xFFFFFFFF) != frame_crc:
            cursor = offset + 1
            continue
        try:
            event = json.loads(payload)
        except Exception:
            cursor = offset + 1
            continue
        if isinstance(event, dict) and {"binlog_file", "start_pos", "end_pos", "event_type"} <= set(event):
            yield event
        cursor = end


def _decode_full_row(columns, types, data: bytes):
    if len(data) < 8:
        raise ValueError("short physical row")
    nullmask = struct.unpack_from("<Q", data, 0)[0]
    offset = 8
    row = {}
    for ordinal, (name, type_code) in enumerate(zip(columns, types)):
        if (nullmask >> ordinal) & 1:
            row[name] = None
        else:
            row[name], offset = decode_scalar(type_code, data, offset)
    if offset != len(data):
        raise ValueError("physical row has trailing bytes")
    return row


def parse_page_payload(payload: bytes, layouts):
    if len(payload) < 2:
        raise ValueError("short page payload")
    count = struct.unpack_from("<H", payload, 0)[0]
    offset = 2
    rows = {}
    for _ in range(count):
        if offset + ROW_HEADER.size > len(payload):
            raise ValueError("truncated page row header")
        object_tag, flags, trx_id, roll_ptr, row_len = ROW_HEADER.unpack_from(payload, offset)
        offset += ROW_HEADER.size
        end = offset + row_len
        if end > len(payload):
            raise ValueError("truncated page row")
        if int(object_tag) not in layouts:
            raise ValueError(f"unknown object tag {object_tag}")
        columns, types = layouts[int(object_tag)]
        row = _decode_full_row(columns, types, payload[offset:end])
        offset = end
        if row.get("id") is None:
            raise ValueError("physical row is missing primary key")
        key = (int(object_tag), int(row["id"]))
        if key in rows:
            raise ValueError(f"duplicate row head {key}")
        rows[key] = {
            "trx_id": int(trx_id),
            "delete_mark": bool(flags & 1),
            "roll_ptr": int(roll_ptr),
            "row": row,
        }
    if offset != len(payload):
        raise ValueError("page payload has trailing bytes")
    return rows


def load_page_copies(path: Path):
    data = path.read_bytes()
    if len(data) < PAGE_HEADER.size:
        raise ValueError("short page capture")
    magic, version, count = PAGE_HEADER.unpack_from(data, 0)
    if magic != b"IPG2" or version != 2:
        raise ValueError("unsupported page capture")
    offset = PAGE_HEADER.size
    out = []
    for _ in range(count):
        if offset + PAGE_COPY_HEADER.size > len(data):
            raise ValueError("truncated page-copy header")
        page_no, copy_kind, reserved, page_lsn, size, checksum = PAGE_COPY_HEADER.unpack_from(data, offset)
        offset += PAGE_COPY_HEADER.size
        end = offset + size
        if end > len(data):
            raise ValueError("truncated page-copy payload")
        payload = data[offset:end]
        offset = end
        if reserved != 0 or copy_kind not in (0, 1):
            continue
        out.append({
            "page_no": int(page_no),
            "copy_kind": int(copy_kind),
            "page_lsn": int(page_lsn),
            "checksum_ok": (zlib.crc32(payload) & 0xFFFFFFFF) == int(checksum),
            "payload": payload,
        })
    if offset != len(data):
        raise ValueError("page capture has trailing bytes")
    return out


def load_undo_records(path: Path):
    data = path.read_bytes()
    if len(data) < UNDO_HEADER.size:
        raise ValueError("short undo capture")
    magic, version, count = UNDO_HEADER.unpack_from(data, 0)
    if magic != b"IUN1" or version != 1:
        raise ValueError("unsupported undo capture")
    offset = UNDO_HEADER.size
    out = {}
    for _ in range(count):
        if offset + UNDO_RECORD_HEADER.size > len(data):
            raise ValueError("truncated undo header")
        ptr, prev_trx_id, prev_delete, reserved, prev_roll_ptr, patch_len = UNDO_RECORD_HEADER.unpack_from(data, offset)
        offset += UNDO_RECORD_HEADER.size
        end = offset + patch_len
        if end > len(data):
            raise ValueError("truncated undo patch")
        if reserved != 0 or prev_delete not in (0, 1):
            raise ValueError("invalid undo record")
        patch = json.loads(data[offset:end])
        offset = end
        if int(ptr) in out:
            raise ValueError(f"duplicate undo pointer {ptr}")
        out[int(ptr)] = {
            "prev_trx_id": int(prev_trx_id),
            "prev_delete": bool(prev_delete),
            "prev_roll_ptr": int(prev_roll_ptr),
            "patch": patch,
        }
    if offset != len(data):
        raise ValueError("undo capture has trailing bytes")
    return out


def load_transaction_history(path: Path):
    data = path.read_bytes()
    if len(data) < TRX_HISTORY_HEADER.size:
        raise ValueError("short transaction history")
    magic, version, count = TRX_HISTORY_HEADER.unpack_from(data, 0)
    if magic != b"ITH1" or version != 1:
        raise ValueError("unsupported transaction history")
    offset = TRX_HISTORY_HEADER.size
    out = []
    for _ in range(count):
        if offset + TRX_HISTORY_RECORD.size > len(data):
            raise ValueError("truncated transaction-history record")
        trx_id, begin_lsn, end_lsn, outcome, reserved = TRX_HISTORY_RECORD.unpack_from(data, offset)
        offset += TRX_HISTORY_RECORD.size
        if reserved != b"\x00\x00\x00" or outcome not in (1, 2, 3):
            raise ValueError("invalid transaction-history record")
        out.append({"trx_id": int(trx_id), "begin_lsn": int(begin_lsn), "end_lsn": int(end_lsn), "outcome": int(outcome)})
    if offset != len(data):
        raise ValueError("transaction history has trailing bytes")
    return out


def load_redo_blocks(path: Path):
    data = path.read_bytes()
    if len(data) < REDO_FILE_HEADER.size:
        raise ValueError("short redo mirror")
    magic, version, count = REDO_FILE_HEADER.unpack_from(data, 0)
    if magic != b"IRL2" or version != 2:
        raise ValueError("unsupported redo mirror")
    offset = REDO_FILE_HEADER.size
    out = []
    for _ in range(count):
        if offset + REDO_BLOCK_HEADER.size > len(data):
            raise ValueError("truncated redo block header")
        sequence, start_lsn, end_lsn, prev_crc, size, payload_crc = REDO_BLOCK_HEADER.unpack_from(data, offset)
        offset += REDO_BLOCK_HEADER.size
        end = offset + size
        if end > len(data):
            raise ValueError("truncated redo payload")
        payload = data[offset:end]
        offset = end
        if (zlib.crc32(payload) & 0xFFFFFFFF) != payload_crc:
            continue
        out.append({
            "sequence": int(sequence),
            "start_lsn": int(start_lsn),
            "end_lsn": int(end_lsn),
            "prev_crc32": int(prev_crc),
            "payload_crc32": int(payload_crc),
            "payload": payload,
        })
    if offset != len(data):
        raise ValueError("redo mirror has trailing bytes")
    return out


def iter_redo_operations(payload: bytes, field_type):
    """Yield decoded redo operations without maintaining recovery state.

    ``field_type(page_no, slot, ordinal)`` supplies the type code for a patch
    field. The caller owns slot identity, page ordering, mini-transaction
    deduplication, and application of the decoded operations.
    """
    if len(payload) < 2:
        raise ValueError("short redo payload")
    mtr_count = struct.unpack_from("<H", payload, 0)[0]
    offset = 2
    for _ in range(mtr_count):
        if offset + REDO_MTR_HEADER.size > len(payload):
            raise ValueError("truncated redo mini-transaction header")
        mtr_id, op_count = REDO_MTR_HEADER.unpack_from(payload, offset)
        offset += REDO_MTR_HEADER.size
        yield {"kind": "mtr_begin", "mtr_id": int(mtr_id)}
        for _ in range(op_count):
            if offset >= len(payload):
                raise ValueError("truncated redo operation")
            op_type = payload[offset]
            if op_type == 1:
                if offset + REDO_PATCH_HEADER.size > len(payload):
                    raise ValueError("truncated redo slot patch")
                _, page_no, slot, trx_id, roll_ptr, delete_mark, field_count = REDO_PATCH_HEADER.unpack_from(payload, offset)
                offset += REDO_PATCH_HEADER.size
                fields = []
                for _ in range(field_count):
                    if offset >= len(payload):
                        raise ValueError("truncated redo field")
                    ordinal = int(payload[offset])
                    offset += 1
                    type_code = int(field_type(int(page_no), int(slot), ordinal))
                    value, offset = decode_scalar(type_code, payload, offset)
                    fields.append((ordinal, value))
                yield {
                    "kind": "patch",
                    "mtr_id": int(mtr_id),
                    "page_no": int(page_no),
                    "slot": int(slot),
                    "trx_id": int(trx_id),
                    "roll_ptr": int(roll_ptr),
                    "delete_mark": bool(delete_mark),
                    "fields": fields,
                }
            elif op_type == 2:
                if offset + REDO_REORG_HEADER.size > len(payload):
                    raise ValueError("truncated redo reorganization")
                _, page_no, count = REDO_REORG_HEADER.unpack_from(payload, offset)
                offset += REDO_REORG_HEADER.size
                need = 2 * int(count)
                if offset + need > len(payload):
                    raise ValueError("truncated redo reorganization permutation")
                perm = list(struct.unpack_from("<" + "H" * int(count), payload, offset))
                offset += need
                yield {
                    "kind": "reorg",
                    "mtr_id": int(mtr_id),
                    "page_no": int(page_no),
                    "count": int(count),
                    "permutation": [int(x) for x in perm],
                }
            else:
                raise ValueError(f"unknown redo operation {op_type}")
    if offset != len(payload):
        raise ValueError("redo payload has trailing bytes")

