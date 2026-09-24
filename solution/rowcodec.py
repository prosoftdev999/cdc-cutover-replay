from evidence_codec import decode_row_image


def decode_image(types, bitmap_hex, blob_b64):
    return decode_row_image(types, bitmap_hex, blob_b64)


def materialize(columns, base, partial):
    row = list(base) if base is not None else [None] * len(columns)
    for ordinal, value in partial.items():
        row[ordinal] = value
    return row
