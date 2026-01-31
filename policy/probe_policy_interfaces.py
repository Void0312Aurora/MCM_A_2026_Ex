from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


def _ensure_repo_root_on_sys_path() -> None:
    # Repo root is the parent of this folder (policy/).
    root = Path(__file__).resolve().parents[1]
    root_str = str(root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)


_ensure_repo_root_on_sys_path()

from mp_power.adb import pick_default_serial
from mp_power.adb import resolve_adb
from mp_power.adb import run_adb


@dataclass(frozen=True)
class ProbeResult:
    serial: str
    keywords: list[str]
    candidates: list[str]
    notes: list[str]


def _sanitize_filename(s: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9_.-]+", "_", s.strip())
    return s[:120] if len(s) > 120 else s


def main() -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Probe non-root observable policy/perf services and traces. "
            "Collects dumpsys -l, service list, cmd -l, and best-effort dumpsys for candidate services."
        )
    )
    ap.add_argument("--adb", default=None, help="adb path (optional)")
    ap.add_argument("--serial", default=None, help="device serial (optional)")
    ap.add_argument(
        "--keywords",
        default="power,perf,fps,thermal,mtk,mi,game,boost,hal,hint",
        help="comma-separated keywords to filter dumpsys -l services",
    )
    ap.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="output directory (default: artifacts/android/policy_probe/<timestamp>)",
    )
    ap.add_argument("--timeout", type=float, default=12.0, help="per-command timeout seconds")
    ap.add_argument("--max-services", type=int, default=60, help="max candidate services to dump")
    args = ap.parse_args()

    adb = resolve_adb(args.adb)

    serial = args.serial
    if not serial:
        serial = pick_default_serial(adb, timeout_s=8.0)
    if not serial:
        raise SystemExit("No ADB device found. Ensure wireless debugging is paired/connected, then pass --serial.")

    base = ["-s", serial]

    keywords = [k.strip().lower() for k in str(args.keywords).split(",") if k.strip()]

    ts = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
    out_dir: Path = args.out_dir or (Path("artifacts") / "android" / "policy_probe" / ts)
    out_dir.mkdir(parents=True, exist_ok=True)

    notes: list[str] = []

    # 1) dumpsys -l
    rc, out, err = run_adb(adb, [*base, "shell", "dumpsys", "-l"], timeout_s=float(args.timeout))
    (out_dir / "dumpsys_l.txt").write_text(out + ("\n" + err if err else ""), encoding="utf-8")
    if rc != 0:
        notes.append(f"dumpsys -l failed: {err.strip()}")

    services = [ln.strip() for ln in out.splitlines() if ln.strip()]

    def hit(svc: str) -> bool:
        sl = svc.lower()
        return any(k in sl for k in keywords)

    candidates = sorted({svc for svc in services if hit(svc)})

    # 2) service list (binder services)
    rc, out2, err2 = run_adb(adb, [*base, "shell", "service", "list"], timeout_s=float(args.timeout))
    (out_dir / "service_list.txt").write_text(out2 + ("\n" + err2 if err2 else ""), encoding="utf-8")
    if rc != 0:
        notes.append(f"service list failed: {err2.strip()}")

    # 3) cmd -l (cmdline services)
    rc, out3, err3 = run_adb(adb, [*base, "shell", "cmd", "-l"], timeout_s=float(args.timeout))
    (out_dir / "cmd_l.txt").write_text(out3 + ("\n" + err3 if err3 else ""), encoding="utf-8")
    if rc != 0:
        notes.append(f"cmd -l failed: {err3.strip()}")

    # 4) best-effort dumpsys for candidates
    dumped: list[str] = []
    for svc in candidates[: max(0, int(args.max_services))]:
        fn = _sanitize_filename(f"dumpsys_{svc}.txt")
        rc, o, e = run_adb(adb, [*base, "shell", "dumpsys", svc], timeout_s=float(args.timeout))
        (out_dir / fn).write_text(o + ("\n" + e if e else ""), encoding="utf-8")
        dumped.append(svc)
        if rc != 0:
            # Non-fatal: some services require permissions.
            notes.append(f"dumpsys {svc} rc={rc}: {e.strip()}")

    # 5) Atrace categories listing (if available)
    rc, ao, ae = run_adb(adb, [*base, "shell", "atrace", "--list_categories"], timeout_s=float(args.timeout))
    (out_dir / "atrace_list_categories.txt").write_text(ao + ("\n" + ae if ae else ""), encoding="utf-8")
    if rc != 0:
        notes.append("atrace --list_categories failed (may be unavailable on this build)")

    result = ProbeResult(serial=serial, keywords=keywords, candidates=dumped, notes=notes)
    (out_dir / "policy_probe_summary.json").write_text(
        json.dumps(
            {
                "serial": result.serial,
                "keywords": result.keywords,
                "candidates_dumped": result.candidates,
                "notes": result.notes,
                "out_dir": out_dir.as_posix(),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    print(f"Wrote: {out_dir.as_posix()}")
    print(f"Candidates dumped: {len(dumped)}")
    if notes:
        print("Notes:")
        for n in notes[:12]:
            print("-", n)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
