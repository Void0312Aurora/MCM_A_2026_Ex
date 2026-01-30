from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd


def summarize(path: Path) -> dict[str, object]:
    df = pd.read_csv(path)

    def num(col: str) -> pd.Series:
        if col not in df.columns:
            return pd.Series([pd.NA] * len(df))
        return pd.to_numeric(df[col], errors="coerce")

    dt_s = num("dt_s")
    discharge_mj = num("battery_discharge_energy_mJ")
    voltage_mv = num("battery_voltage_mv")
    brightness = num("brightness")
    charge_counter_uah = num("charge_counter_uAh")

    d_uah = charge_counter_uah.diff()
    # discharge => negative deltas
    nonzero = d_uah[(d_uah.notna()) & (d_uah != 0)]
    neg = nonzero[nonzero < 0]

    total_s = float(dt_s.dropna().sum()) if len(dt_s.dropna()) else 0.0
    total_mj = float(discharge_mj.dropna().sum()) if len(discharge_mj.dropna()) else 0.0
    mean_power_total_mw = (total_mj / total_s) if total_s > 0 else float("nan")

    p_row = discharge_mj / dt_s
    mean_power_row_mw = float(p_row.dropna().mean()) if len(p_row.dropna()) else float("nan")

    avg_current_ma = float("nan")
    if len(charge_counter_uah.dropna()) and total_s > 0:
        delta_uah = float(charge_counter_uah.iloc[-1] - charge_counter_uah.iloc[0])
        avg_current_ma = (-delta_uah) / (total_s / 3600.0) / 1000.0

    # charge_counter resolution / quantization diagnostics
    nonzero_frac = float((d_uah.notna() & (d_uah != 0)).mean()) if len(d_uah) else float("nan")
    neg_frac = float((d_uah.notna() & (d_uah < 0)).mean()) if len(d_uah) else float("nan")
    neg_median = float(neg.median()) if len(neg) else float("nan")
    neg_mean = float(neg.mean()) if len(neg) else float("nan")
    neg_min = float(neg.min()) if len(neg) else float("nan")

    return {
        "file": path.as_posix(),
        "rows": int(len(df)),
        "brightness_unique": int(brightness.nunique(dropna=True)) if len(brightness.dropna()) else 0,
        "brightness": float(brightness.dropna().iloc[0]) if len(brightness.dropna()) else float("nan"),
        "dt_sum_s": total_s,
        "discharge_sum_mJ": total_mj,
        "mean_power_mW_total": mean_power_total_mw,
        "mean_power_mW_row": mean_power_row_mw,
        "avg_current_mA": avg_current_ma,
        "mean_voltage_mV": float(voltage_mv.dropna().mean()) if len(voltage_mv.dropna()) else float("nan"),
        "nonnull_frac_discharge": float(discharge_mj.notna().mean()),
        "nonnull_frac_dt": float(dt_s.notna().mean()),
        "d_uAh_nonzero_frac": nonzero_frac,
        "d_uAh_negative_frac": neg_frac,
        "d_uAh_neg_median": neg_median,
        "d_uAh_neg_mean": neg_mean,
        "d_uAh_neg_min": neg_min,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Summarize one or more run CSVs (prefer enriched)")
    ap.add_argument("--csv", type=Path, nargs="+", required=True)
    args = ap.parse_args()

    rows = []
    for p in args.csv:
        rows.append(summarize(p))

    out = pd.DataFrame(rows)
    # prettier numeric output
    with pd.option_context("display.max_columns", 200, "display.width", 160):
        print(out.to_string(index=False, float_format=lambda x: f"{x:.3f}"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
