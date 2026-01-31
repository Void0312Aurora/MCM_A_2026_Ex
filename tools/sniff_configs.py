from __future__ import annotations

import argparse
import binascii
import gzip
from pathlib import Path
import zipfile


def sniff(path: Path, out_root: Path) -> None:
    data = path.read_bytes()
    head = data[:16]
    print(f"\n== {path.name} ==")
    print("size", len(data))
    print("head_hex", binascii.hexlify(head).decode())
    print("head_ascii", "".join(chr(b) if 32 <= b < 127 else "." for b in head))
    print("zip?", zipfile.is_zipfile(path))
    print("gzip?", head[:2] == b"\x1f\x8b")

    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            print("zip_entries", len(names))
            for n in names[:30]:
                info = z.getinfo(n)
                print(" -", n, "size", info.file_size)

            dest = out_root / (path.stem + "__zip")
            dest.mkdir(parents=True, exist_ok=True)
            z.extractall(dest)
            print("extracted_to", dest)
    elif head[:2] == b"\x1f\x8b":
        try:
            raw = gzip.decompress(data)
            dest = out_root / (path.stem + ".gunzip")
            dest.write_bytes(raw)
            print("gunzip_out", dest, "size", len(raw))
        except Exception as e:
            print("gunzip_failed", e)


def main() -> int:
    ap = argparse.ArgumentParser(description="Quick container sniff (zip/gzip) and extract")
    ap.add_argument("root", type=Path, help="Input directory containing candidate files")
    ap.add_argument("--out", type=Path, default=Path("artifacts/android/configs_extracted"))
    ap.add_argument("--names", nargs="*", default=[])
    args = ap.parse_args()

    out_root: Path = args.out
    out_root.mkdir(parents=True, exist_ok=True)

    if args.names:
        candidates = args.names
    else:
        candidates = [
            "vendor__etc__thermal-map.conf",
            "vendor__etc__thermal__thermal.conf",
            "vendor__etc__thermal__thermal_policy_00.conf",
            "odm__etc__thermal-navigation.conf",
            "odm__etc__thermal-video.conf",
        ]

    for name in candidates:
        path = args.root / name
        if not path.exists():
            print("missing", name)
            continue
        sniff(path, out_root)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
