from __future__ import annotations

import argparse
from pathlib import Path

import fitz  # PyMuPDF


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract text from a PDF")
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--pages", type=int, default=3, help="Number of pages from the start to extract")
    parser.add_argument("--out", type=Path, default=None, help="Optional output .txt path")
    args = parser.parse_args()

    pdf_path: Path = args.pdf
    if not pdf_path.exists():
        raise SystemExit(f"PDF not found: {pdf_path}")

    doc = fitz.open(pdf_path)
    page_count = doc.page_count
    n = min(max(args.pages, 1), page_count)

    chunks: list[str] = [f"PAGES: {page_count}\n"]
    for i in range(n):
        text = doc.load_page(i).get_text("text")
        chunks.append(f"\n{'='*20} PAGE {i+1} {'='*20}\n")
        chunks.append(text)

    content = "".join(chunks)

    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(content, encoding="utf-8")
    else:
        print(content)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
