from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd


def _num(df: pd.DataFrame, col: str) -> pd.Series:
    if col not in df.columns:
        return pd.Series([pd.NA] * len(df))
    return pd.to_numeric(df[col], errors="coerce")


def main() -> int:
    parser = argparse.ArgumentParser(description="Quick QC stats for a run CSV (prefer enriched)")
    parser.add_argument("--csv", type=Path, required=True)
    args = parser.parse_args()

    df = pd.read_csv(args.csv)

    print("rows", len(df))
    print("columns", len(df.columns))

    # adb_error
    if "adb_error" in df.columns:
        bad = df["adb_error"].fillna("")
        n_bad = int((bad != "").sum())
        print("adb_error non-empty", n_bad)
        if n_bad:
            print(df.loc[bad != "", ["seq", "ts_pc", "adb_error"]].head(8).to_string(index=False))

    # brightness
    if "brightness" in df.columns:
        b = pd.to_numeric(df["brightness"], errors="coerce")
        print("brightness unique count", int(b.nunique(dropna=True)))
        print("brightness min/median/max", float(b.min()), float(b.median()), float(b.max()))

    # battery stats
    v = _num(df, "battery_voltage_mv")
    cc = _num(df, "charge_counter_uAh")
    level = _num(df, "battery_level")

    if len(df) > 0:
        print("battery_level start/end", level.iloc[0], level.iloc[-1])
        print("charge_counter_uAh start/end", cc.iloc[0], cc.iloc[-1], "delta", cc.iloc[-1] - cc.iloc[0])
        print("voltage_mv start/end", v.iloc[0], v.iloc[-1])

    # thermal
    therm_cols = [c for c in df.columns if c.startswith("thermal_") and c.endswith("_C")]
    print("thermal cols", therm_cols)
    for c in therm_cols:
        s = pd.to_numeric(df[c], errors="coerce")
        if len(s.dropna()) == 0:
            continue
        print(c, "start/end/mean", float(s.iloc[0]), float(s.iloc[-1]), float(s.mean()))

    # CPU columns
    cpu_avg_cols = [c for c in df.columns if c.startswith("cpu_policy") and c.endswith("_avg_power_mW")]
    print("cpu avg power cols", cpu_avg_cols)
    for c in cpu_avg_cols:
        s = pd.to_numeric(df[c], errors="coerce")
        if len(s.dropna()) == 0:
            continue
        # Note: this is conditional average power over the *accounted time_in_state window* for that policy.
        # It is not an interval-average over dt_s, so it can look larger than cpu_total_power_mW.
        print(c, "mean (conditional)", float(s.mean()))

    # Per-policy interval-average power (mW) and duty factor, derived from energy_mJ and dt_s.
    dt_s = _num(df, "dt_s")
    if len(dt_s.dropna()) > 0:
        print(
            "dt_s stats (min/median/max/sum)",
            float(dt_s.min()),
            float(dt_s.median()),
            float(dt_s.max()),
            float(dt_s.sum()),
        )
    if len(dt_s.dropna()) > 0:
        for c in cpu_avg_cols:
            # cpu_policy{p}_avg_power_mW
            stem = c[: -len("_avg_power_mW")]
            e_col = f"{stem}_energy_mJ"
            if e_col not in df.columns:
                continue

            e_mj = _num(df, e_col)
            p_interval_mw = e_mj / dt_s
            if len(p_interval_mw.dropna()) > 0:
                print(f"{stem}_power_mW_over_dt mean", float(p_interval_mw.mean()))

            # duty factor: sum(freq_dt_ms) / (dt_s*1000)
            # We recompute sum_dt_ms by summing all cpu_p{policy}_freq*_dt columns.
            # Example: cpu_p0_freq300000_dt
            policy_prefix = "cpu_p" + stem[len("cpu_policy") :] + "_freq"
            dt_cols = [
                col
                for col in df.columns
                if col.startswith(policy_prefix) and col.endswith("_dt")
            ]
            if dt_cols:
                sum_dt_ms = pd.DataFrame({col: _num(df, col) for col in dt_cols}).sum(axis=1, skipna=True)
                duty = sum_dt_ms / (dt_s * 1000.0)
                duty = duty.where(duty.notna() & (dt_s > 0))
                if len(duty.dropna()) > 0:
                    print(f"{stem}_duty mean", float(duty.mean()))

    # totals
    if "cpu_energy_mJ_total" in df.columns:
        cpu_e = pd.to_numeric(df["cpu_energy_mJ_total"], errors="coerce")
        if "dt_s" in df.columns:
            p = cpu_e / dt_s
            if len(p.dropna()) > 0:
                print("cpu_total_power_mW mean", float(p.mean()))

    # Battery discharge power derived from charge_counter delta (already in enriched as energy per interval)
    if "battery_discharge_energy_mJ" in df.columns and "dt_s" in df.columns:
        be = _num(df, "battery_discharge_energy_mJ")
        bp = be / dt_s
        if len(bp.dropna()) > 0:
            print("batt_discharge_power_mW mean (per-row, unweighted)", float(bp.mean()))
            print("batt_discharge_power_mW median", float(bp.median()))

        total_s = float(dt_s.dropna().sum()) if len(dt_s.dropna()) else 0.0
        total_mj = float(be.dropna().sum()) if len(be.dropna()) else 0.0
        if total_s > 0:
            print("batt_discharge_power_mW mean (total/total)", float(total_mj / total_s))

    # Average discharge current estimate from charge_counter delta over total wall-clock duration
    if len(df) > 1 and "charge_counter_uAh" in df.columns and "dt_s" in df.columns:
        cc0 = cc.iloc[0]
        cc1 = cc.iloc[-1]
        total_s = float(dt_s.dropna().sum()) if len(dt_s.dropna()) else 0.0
        if pd.notna(cc0) and pd.notna(cc1) and total_s > 0:
            delta_uah = float(cc1 - cc0)  # negative for discharge
            avg_current_ma = (-delta_uah) / (total_s / 3600.0) / 1000.0
            print("avg_discharge_current_mA est", float(avg_current_ma))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
