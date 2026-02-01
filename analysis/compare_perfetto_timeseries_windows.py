from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

import pandas as pd


@dataclass(frozen=True)
class Window:
    start_s: float
    end_s: float


def read_ts(report_dir: Path) -> pd.DataFrame:
    p = report_dir / "perfetto_android_power_timeseries.csv"
    if not p.exists():
        raise SystemExit(f"Missing perfetto timeseries: {p}")
    df = pd.read_csv(p)
    if "t_s" not in df.columns:
        raise SystemExit(f"perfetto timeseries missing t_s: {p}")

    keep = [c for c in ["t_s", "power_mw_calc", "batt.current_ua", "batt.voltage_uv"] if c in df.columns]
    df = df[keep].copy()
    for c in keep:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["t_s"]).sort_values("t_s").reset_index(drop=True)
    return df


def summarize_window(df: pd.DataFrame, w: Window) -> dict[str, float | int]:
    sub = df[(df["t_s"] >= w.start_s) & (df["t_s"] < w.end_s)].copy()
    out: dict[str, float | int] = {
        "start_s": w.start_s,
        "end_s": w.end_s,
        "n": int(len(sub)),
    }

    def add_stats(col: str, prefix: str) -> None:
        if col not in sub.columns:
            return
        s = pd.to_numeric(sub[col], errors="coerce").dropna()
        if s.empty:
            return
        out[f"{prefix}_mean"] = float(s.mean())
        out[f"{prefix}_p50"] = float(s.quantile(0.50))
        out[f"{prefix}_p95"] = float(s.quantile(0.95))

    add_stats("power_mw_calc", "power_mw")
    add_stats("batt.current_ua", "current_ua")
    add_stats("batt.voltage_uv", "voltage_uv")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Compare Perfetto android.power timeseries in fixed time windows")
    ap.add_argument("--report-dir", type=Path, nargs="+", required=True, help="report dirs containing perfetto_android_power_timeseries.csv")
    ap.add_argument("--windows", type=str, default="0-120,120-300,300-540", help="comma-separated windows like 0-120,120-300")
    ap.add_argument("--out-csv", type=Path, default=Path("artifacts") / "plots" / "perfetto_timeseries_window_compare.csv")
    args = ap.parse_args()

    windows: list[Window] = []
    for part in args.windows.split(","):
        part = part.strip()
        if not part:
            continue
        a, b = part.split("-")
        windows.append(Window(start_s=float(a), end_s=float(b)))
    if not windows:
        raise SystemExit("No windows parsed")

    rows: list[dict] = []
    for rd in args.report_dir:
        df = read_ts(rd)
        label = rd.name
        for w in windows:
            row = {"report_dir": rd.as_posix(), "label": label}
            row.update(summarize_window(df, w))
            rows.append(row)

    out = pd.DataFrame(rows)
    args.out_csv.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(args.out_csv, index=False, encoding="utf-8")
    print(f"Wrote: {args.out_csv}")
    # Also print a compact table
    show_cols = [
        "label",
        "start_s",
        "end_s",
        "n",
        "power_mw_mean",
        "power_mw_p50",
        "power_mw_p95",
        "current_ua_mean",
        "voltage_uv_mean",
    ]
    existing = [c for c in show_cols if c in out.columns]
    if existing:
        print(out[existing].to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
