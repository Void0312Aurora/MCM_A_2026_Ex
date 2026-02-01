from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

import pandas as pd


def _parse_ts(s: str) -> datetime | None:
    s = (s or "").strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def add_time(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    if "ts_pc" in out.columns:
        ts = out["ts_pc"].astype(str).map(_parse_ts)
        if hasattr(ts, "dt") and ts.notna().any():
            out["ts"] = ts
            out["t_s"] = (ts - ts.iloc[0]).dt.total_seconds()
            return out

    if "dt_s" in out.columns:
        dt = pd.to_numeric(out["dt_s"], errors="coerce").fillna(0.0)
        out["t_s"] = dt.cumsum()
        return out

    out["t_s"] = range(len(out))
    return out


def is_state_like(s: pd.Series) -> bool:
    if s.isna().all():
        return False
    # Treat bool/object columns as state-like
    if s.dtype == "object":
        return True
    # Numeric but low-cardinality (e.g., 0/1, small enums)
    uniq = s.dropna().unique()
    return len(uniq) <= 6


def find_transitions(df: pd.DataFrame, col: str) -> pd.DataFrame:
    s = df[col]
    # Normalize to string for stable comparisons (keeps NaN as 'nan')
    s_norm = s.astype(str)
    changed = s_norm.ne(s_norm.shift(1))
    idx = df.index[changed].tolist()
    rows = []
    for i in idx:
        prev = s_norm.iloc[i - 1] if i > 0 else "<START>"
        cur = s_norm.iloc[i]
        rows.append(
            {
                "row": int(i),
                "t_s": float(df["t_s"].iloc[i]) if "t_s" in df.columns else float(i),
                "prev": prev,
                "cur": cur,
            }
        )
    return pd.DataFrame(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description="Find state/policy-like column transitions in a run CSV")
    ap.add_argument("--csv", type=Path, required=True)
    ap.add_argument("--out-dir", type=Path, default=Path("artifacts") / "plots" / "state_transitions")
    ap.add_argument("--include", type=str, default="", help="comma-separated extra columns to always include")
    ap.add_argument("--max-cols", type=int, default=60)
    args = ap.parse_args()

    df = pd.read_csv(args.csv)
    df = add_time(df)

    always = [c.strip() for c in args.include.split(",") if c.strip()]
    candidates: list[str] = []
    for c in df.columns:
        if c in {"run_id", "scenario", "note"}:
            continue
        if c in {"seq", "ts_pc", "ts", "t_s"}:
            continue
        if c in always:
            candidates.append(c)
            continue
        s = df[c]
        if is_state_like(s):
            candidates.append(c)

    # Prefer obvious policy-ish names
    def score(name: str) -> tuple[int, str]:
        n = name.lower()
        pri = 0
        for kw in [
            "thermal_status",
            "low_power",
            "saver",
            "doze",
            "idle",
            "mode",
            "policy",
            "display_state",
            "plug",
            "powered",
            "updates_stopped",
            "wifi",
            "cell",
        ]:
            if kw in n:
                pri -= 10
        return (pri, name)

    candidates = sorted(dict.fromkeys(candidates), key=score)[: args.max_cols]

    args.out_dir.mkdir(parents=True, exist_ok=True)
    stem = args.csv.stem
    out_csv = args.out_dir / f"{stem}_state_columns.csv"
    out_xlsx = None

    # Summary table: nunique + value counts (top few)
    rows = []
    for c in candidates:
        s = df[c]
        s_norm = s.astype(str)
        vc = s_norm.value_counts(dropna=False).head(5).to_dict()
        rows.append({"col": c, "nunique": int(s_norm.nunique(dropna=False)), "top_values": vc})
    pd.DataFrame(rows).to_csv(out_csv, index=False, encoding="utf-8")

    # Transitions file per column (only when it actually changes)
    trans_rows = []
    for c in candidates:
        tdf = find_transitions(df, c)
        if len(tdf) <= 1:
            continue
        tdf.insert(0, "col", c)
        trans_rows.append(tdf)
    out_trans = args.out_dir / f"{stem}_transitions.csv"
    if trans_rows:
        pd.concat(trans_rows, ignore_index=True).to_csv(out_trans, index=False, encoding="utf-8")
    else:
        pd.DataFrame(columns=["col", "row", "t_s", "prev", "cur"]).to_csv(out_trans, index=False, encoding="utf-8")

    print(f"Wrote: {out_csv}")
    print(f"Wrote: {out_trans}")

    # Print a short, human-readable highlight for the most relevant columns
    highlight = [
        "thermal_status",
        "battery_updates_stopped",
        "display_state",
        "battery_plugged",
        "battery_status",
        "battery_ac_powered",
        "battery_usb_powered",
        "battery_wireless_powered",
    ]
    present = [c for c in highlight if c in df.columns]
    if present:
        print("\nHighlights (unique values):")
        for c in present:
            vals = df[c].astype(str).unique().tolist()
            vals = vals[:10]
            print(f"- {c}: {vals}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
