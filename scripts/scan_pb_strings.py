"""Scan raw protobuf bytes for embedded ASCII strings.

This is schema-free and intended for reconnaissance only.

Usage:
  python scripts/scan_pb_strings.py artifacts/raw/batterystats_xxx.pb --grep screen energy consumer
"""

from __future__ import annotations

import argparse
from pathlib import Path


def extract_ascii_strings(data: bytes, min_len: int = 4) -> list[str]:
    strings: list[str] = []
    buf: list[int] = []

    def flush() -> None:
        nonlocal buf
        if len(buf) >= min_len:
            try:
                strings.append(bytes(buf).decode("ascii"))
            except Exception:
                pass
        buf = []

    for b in data:
        if 32 <= b <= 126:
            buf.append(b)
        else:
            flush()
    flush()

    return strings


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pb", type=Path)
    ap.add_argument("--min-len", type=int, default=4)
    ap.add_argument("--max", type=int, default=500)
    ap.add_argument("--grep", nargs="*", default=[])
    args = ap.parse_args()

    data = args.pb.read_bytes()
    strings = extract_ascii_strings(data, min_len=args.min_len)

    # Deduplicate while preserving order
    seen: set[str] = set()
    uniq: list[str] = []
    for s in strings:
        if s in seen:
            continue
        seen.add(s)
        uniq.append(s)

    if args.grep:
        needles = [n.lower() for n in args.grep]

        def ok(s: str) -> bool:
            s2 = s.lower()
            return any(n in s2 for n in needles)

        uniq = [s for s in uniq if ok(s)]

    for s in uniq[: args.max]:
        print(s)

    if len(uniq) > args.max:
        print(f"... ({len(uniq) - args.max} more)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
