# tools/

This folder contains **non-protocol** utilities (debug/recon helpers) that are not part of the main MP_power data pipeline.

If you're looking for the standard workflow, use the scripts in `scripts/` (e.g. `pipeline_run.py`).

Utilities currently here:
- `scan_pb_strings.py`: schema-free ASCII string scan for protobuf blobs
- `proto_wire_inspect.py`: schema-free protobuf wire-format inspector
- `sniff_configs.py`: quick container sniff (zip/gzip) + extract
- `extract_pdf_text.py`: extract first N pages of text from a PDF (requires PyMuPDF)
- `clean_artifacts.ps1`: delete disposable outputs under `artifacts/` (runs/reports/plots/raw/traces) so you can re-run experiments from scratch

Cleaning examples (PowerShell):
- Keep pulled `artifacts/android/` but wipe all run outputs: `powershell -NoProfile -ExecutionPolicy Bypass -File tools/clean_artifacts.ps1`
- Wipe *everything* under `artifacts/` (including pulled android configs/overlays): `powershell -NoProfile -ExecutionPolicy Bypass -File tools/clean_artifacts.ps1 -AllAndroid`
