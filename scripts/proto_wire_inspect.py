"""Schema-free protobuf wire inspector.

Useful when `dumpsys ... --proto` returns binary but the exact top-level message
is unknown or OEM-modified.

This does NOT attempt full decoding. It only parses the wire format and prints
field numbers, wire types, and basic interpretations.

Usage:
  python scripts/proto_wire_inspect.py artifacts/raw/batterystats_xxx.pb --max-depth 2
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path


# Protobuf wire types
WT_VARINT = 0
WT_FIXED64 = 1
WT_LEN = 2
WT_FIXED32 = 5


@dataclass
class Field:
    field_no: int
    wire_type: int
    start: int
    end: int
    value_preview: str
    length: int | None = None


def _read_varint(buf: bytes, i: int) -> tuple[int, int]:
    shift = 0
    out = 0
    while True:
        if i >= len(buf):
            raise ValueError("truncated varint")
        b = buf[i]
        i += 1
        out |= (b & 0x7F) << shift
        if (b & 0x80) == 0:
            return out, i
        shift += 7
        if shift > 70:
            raise ValueError("varint too long")


def _is_printable_ascii(data: bytes) -> bool:
    return all(32 <= b <= 126 for b in data)


def parse_fields(buf: bytes, start: int = 0, end: int | None = None, max_fields: int = 5000) -> list[Field]:
    if end is None:
        end = len(buf)
    i = start
    fields: list[Field] = []

    while i < end and len(fields) < max_fields:
        tag_start = i
        tag, i = _read_varint(buf, i)
        field_no = tag >> 3
        wire_type = tag & 0x7

        if wire_type == WT_VARINT:
            v, i2 = _read_varint(buf, i)
            preview = str(v)
            fields.append(Field(field_no, wire_type, tag_start, i2, preview, None))
            i = i2
        elif wire_type == WT_FIXED64:
            if i + 8 > end:
                raise ValueError("truncated fixed64")
            chunk = buf[i : i + 8]
            preview = chunk.hex()
            fields.append(Field(field_no, wire_type, tag_start, i + 8, preview, None))
            i += 8
        elif wire_type == WT_FIXED32:
            if i + 4 > end:
                raise ValueError("truncated fixed32")
            chunk = buf[i : i + 4]
            preview = chunk.hex()
            fields.append(Field(field_no, wire_type, tag_start, i + 4, preview, None))
            i += 4
        elif wire_type == WT_LEN:
            ln, i2 = _read_varint(buf, i)
            data_start = i2
            data_end = data_start + ln
            if data_end > end:
                raise ValueError("truncated len")
            data = buf[data_start:data_end]
            if ln == 0:
                preview = "<empty>"
            elif _is_printable_ascii(data[: min(60, ln)]):
                s = data[: min(60, ln)].decode("ascii", errors="replace")
                preview = f"\"{s}\""
            else:
                preview = f"<{ln} bytes>"
            fields.append(Field(field_no, wire_type, tag_start, data_end, preview, ln))
            i = data_end
        else:
            raise ValueError(f"unsupported wire type {wire_type} at {tag_start}")

    return fields


def summarize_level(buf: bytes, max_fields: int) -> dict[tuple[int, int], int]:
    counts: dict[tuple[int, int], int] = {}
    for f in parse_fields(buf, 0, None, max_fields=max_fields):
        k = (f.field_no, f.wire_type)
        counts[k] = counts.get(k, 0) + 1
    return counts


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pb", type=Path)
    ap.add_argument("--max-fields", type=int, default=2000)
    ap.add_argument("--max-depth", type=int, default=1)
    ap.add_argument("--max-children", type=int, default=30)
    args = ap.parse_args()

    data = args.pb.read_bytes()

    def walk(buf: bytes, depth: int, prefix: str) -> None:
        fields = parse_fields(buf, 0, None, max_fields=args.max_fields)
        counts = summarize_level(buf, max_fields=args.max_fields)
        print(f"{prefix}fields={len(fields)} unique={len(counts)} size={len(buf)}")
        for (field_no, wire_type), n in sorted(counts.items(), key=lambda x: (x[0][0], x[0][1]))[:200]:
            print(f"{prefix}  {field_no}:{wire_type} x{n}")

        if depth >= args.max_depth:
            return

        # Heuristic: descend into the biggest length-delimited children.
        children = [f for f in fields if f.wire_type == WT_LEN and (f.length or 0) > 0]
        children.sort(key=lambda f: f.length or 0, reverse=True)
        for child in children[: args.max_children]:
            # Extract the payload bytes
            # Re-parse to find the exact span of this child (tag_start..end includes tag+len+payload)
            # We want only payload; recompute quickly.
            # NOTE: This is a cheap approach: locate payload by searching for the preview boundary is unreliable,
            # so just re-parse with offsets.
            tag, j = _read_varint(buf, child.start)
            ln, j2 = _read_varint(buf, j)
            payload = buf[j2 : j2 + ln]
            print(f"{prefix}>> descend field {child.field_no} len={ln} preview={child.value_preview}")
            try:
                walk(payload, depth + 1, prefix + "    ")
            except Exception as e:
                print(f"{prefix}    (failed to parse child as message: {e})")

    walk(data, 1, prefix="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
