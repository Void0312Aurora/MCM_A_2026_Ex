from __future__ import annotations

from pathlib import Path
from pprint import pprint

import numpy as np
import pandas as pd


def summarize(df: pd.DataFrame) -> dict:
    out: dict[str, object] = {"rows": int(len(df))}

    for col in ["battery_plugged", "battery_status", "battery_updates_stopped", "display_state"]:
        if col in df.columns:
            out[col] = df[col].astype(str).value_counts(dropna=False).to_dict()

    if "dt_s" in df.columns:
        dt = pd.to_numeric(df["dt_s"], errors="coerce").dropna()
        if len(dt):
            out["dt_sum_s"] = float(dt.sum())
            out["dt_median_s"] = float(dt.median())

    for col in [
        "brightness",
        "battery_level",
        "battery_voltage_mv",
        "thermal_skin_C",
        "thermal_battery_C",
        "thermal_cpu_C",
        "screen_power_mW_est",
    ]:
        if col in df.columns:
            s = pd.to_numeric(df[col], errors="coerce").dropna()
            if len(s):
                out[f"{col}_mean"] = float(s.mean())

    if "cpu_energy_mJ_total" in df.columns and "dt_s" in df.columns:
        e = pd.to_numeric(df["cpu_energy_mJ_total"], errors="coerce")
        dt = pd.to_numeric(df["dt_s"], errors="coerce")
        p = (e / dt).replace([np.inf, -np.inf], np.nan).dropna()
        if len(p):
            out["cpu_power_mW_mean_over_dt"] = float(p.mean())

    return out


def main() -> int:
    old_path = Path("artifacts/runs/20260201_182508_S2_b30_enriched.csv")
    new_path = Path("artifacts/runs/20260201_193915_S2_b30_1_enriched.csv")

    old = pd.read_csv(old_path)
    new = pd.read_csv(new_path)

    print("OLD b30:")
    pprint(summarize(old), sort_dicts=False)
    print("\nNEW b30:")
    pprint(summarize(new), sort_dicts=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
