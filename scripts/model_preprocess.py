from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class RunPaths:
    run_csv: Path
    report_dir: Path | None


def _infer_run_id_from_filename(path: Path) -> str | None:
    m = re.match(r"^(\d{8}_\d{6})_.*_enriched\.csv$", path.name)
    if not m:
        return None
    return m.group(1)


def _find_report_dir(run_csv: Path, reports_root: Path) -> Path | None:
    run_id = _infer_run_id_from_filename(run_csv)
    if not run_id:
        return None
    matches = sorted(reports_root.glob(f"{run_id}_*"))
    for m in matches:
        if m.is_dir() and (m / "perfetto_android_power_timeseries.csv").exists():
            return m
    return None


def _load_scenario_params(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_csv(path)
    if "scenario" not in df.columns:
        return pd.DataFrame()
    df = df.copy()
    df["scenario"] = df["scenario"].astype(str)
    return df


def _apply_scenario_params(out: pd.DataFrame, scenario_params: pd.DataFrame) -> pd.DataFrame:
    if scenario_params.empty:
        return out

    sp = scenario_params.set_index("scenario")
    scen = out["scenario"].astype(str)

    def _map_col(col: str, default: float | int | str | None) -> pd.Series:
        if col not in sp.columns:
            return pd.Series([default] * len(out), index=out.index)
        vals = scen.map(sp[col])
        return vals.fillna(default)

    out = out.copy()
    out["wifi_on"] = pd.to_numeric(_map_col("wifi_on", 1), errors="coerce").fillna(1).astype(int)
    out["cellular_on"] = pd.to_numeric(_map_col("cellular_on", 1), errors="coerce").fillna(1).astype(int)
    out["gps_on_cfg"] = pd.to_numeric(_map_col("gps_on", np.nan), errors="coerce")
    out["screen_on_cfg"] = pd.to_numeric(_map_col("screen_on", np.nan), errors="coerce")
    out["brightness_target"] = pd.to_numeric(_map_col("brightness_target", np.nan), errors="coerce")
    out["cpu_test"] = pd.to_numeric(_map_col("cpu_test", 0), errors="coerce").fillna(0).astype(int)
    return out


def _load_perfetto_power(report_dir: Path) -> pd.DataFrame:
    ts_path = report_dir / "perfetto_android_power_timeseries.csv"
    pf = pd.read_csv(ts_path)
    if "t_s" not in pf.columns or "power_mw_calc" not in pf.columns:
        raise ValueError(f"Perfetto timeseries missing required columns: {ts_path}")
    pf = pf[["t_s", "power_mw_calc"]].copy()
    pf["t_s"] = pd.to_numeric(pf["t_s"], errors="coerce")
    pf["power_mw_calc"] = pd.to_numeric(pf["power_mw_calc"], errors="coerce")
    pf = pf.dropna().sort_values("t_s").reset_index(drop=True)
    return pf


def _interp1d(x: np.ndarray, y: np.ndarray, xq: np.ndarray) -> np.ndarray:
    # Numpy-only linear interpolation with edge hold.
    if len(x) < 2:
        return np.full_like(xq, np.nan, dtype=float)
    return np.interp(xq, x, y, left=float(y[0]), right=float(y[-1]))


def _make_model_input(
    run_csv: Path,
    report_dir: Path | None,
    scenario_default: str | None,
    scenario_params: pd.DataFrame,
) -> pd.DataFrame:
    df = pd.read_csv(run_csv)

    # Basic time axis
    if "dt_s" in df.columns:
        df["dt_s"] = pd.to_numeric(df["dt_s"], errors="coerce").fillna(0.0)
    else:
        df["dt_s"] = 0.0
    df["t_s"] = df["dt_s"].cumsum() - df["dt_s"]

    # Observations
    soc_pct = (
        pd.to_numeric(df["battery_level"], errors="coerce")
        if "battery_level" in df.columns
        else pd.Series([np.nan] * len(df), index=df.index)
    )
    voltage_mV = (
        pd.to_numeric(df["battery_voltage_mv"], errors="coerce")
        if "battery_voltage_mv" in df.columns
        else pd.Series([np.nan] * len(df), index=df.index)
    )

    # Battery temperature: prefer thermal_battery_C, else battery_temp_deciC/10
    if "thermal_battery_C" in df.columns:
        t_batt = pd.to_numeric(df["thermal_battery_C"], errors="coerce")
    else:
        t_batt = pd.Series([np.nan] * len(df), index=df.index)

    if t_batt.isna().all():
        if "battery_temp_deciC" in df.columns:
            t_batt = pd.to_numeric(df["battery_temp_deciC"], errors="coerce") / 10.0
        else:
            t_batt = pd.Series([np.nan] * len(df), index=df.index)

    if "thermal_cpu_C" in df.columns:
        t_cpu = pd.to_numeric(df["thermal_cpu_C"], errors="coerce")
    else:
        t_cpu = pd.Series([np.nan] * len(df), index=df.index)

    brightness = (
        pd.to_numeric(df["brightness"], errors="coerce")
        if "brightness" in df.columns
        else pd.Series([np.nan] * len(df), index=df.index)
    )
    display_state = df["display_state"] if "display_state" in df.columns else pd.Series([np.nan] * len(df), index=df.index)

    # CPU power proxy from enriched energy
    cpu_energy_mJ = (
        pd.to_numeric(df["cpu_energy_mJ_total"], errors="coerce")
        if "cpu_energy_mJ_total" in df.columns
        else pd.Series([np.nan] * len(df), index=df.index)
    )
    cpu_power_mW = cpu_energy_mJ / df["dt_s"].replace(0.0, np.nan)

    # Screen power estimate. Note: for most scenarios we assume screen is OFF; for S2 we assume screen is ON.
    screen_power_mW_est = (
        pd.to_numeric(df["screen_power_mW_est"], errors="coerce")
        if "screen_power_mW_est" in df.columns
        else pd.Series([np.nan] * len(df), index=df.index)
    )

    # Perfetto total power aligned to sampling instants
    power_total_mW = pd.Series([np.nan] * len(df))
    if report_dir is not None:
        pf = _load_perfetto_power(report_dir)
        power_total_mW = pd.Series(
            _interp1d(pf["t_s"].to_numpy(), pf["power_mw_calc"].to_numpy(), df["t_s"].to_numpy()),
            index=df.index,
        )

    scenario = df.get("scenario")
    if scenario is None:
        scenario = pd.Series([scenario_default] * len(df), index=df.index)
    scenario_s = scenario.astype(str)

    out = pd.DataFrame(
        {
            "t_s": df["t_s"],
            "dt_s": df["dt_s"],
            "soc_pct": soc_pct,
            "voltage_mV": voltage_mV,
            "temperature_C": t_batt,
            "temperature_cpu_C": t_cpu,
            "brightness": brightness,
            "display_state": display_state,
            "power_total_mW": power_total_mW,
            "power_cpu_mW": cpu_power_mW,
            "power_screen_mW": screen_power_mW_est,
            "charge_counter_uAh": pd.to_numeric(df["charge_counter_uAh"], errors="coerce")
            if "charge_counter_uAh" in df.columns
            else pd.Series([np.nan] * len(df), index=df.index),
            "scenario": scenario,
            "run_id": df["run_id"] if "run_id" in df.columns else pd.Series([np.nan] * len(df), index=df.index),
        }
    )

    run_name = run_csv.stem.replace("_enriched", "")
    out["run_name"] = run_name

    # Convenience flags
    out["is_gps_on"] = out["scenario"].astype(str).str.contains(r"\bS4-1\b", regex=True).astype(int)

    # Apply scenario-level configuration (from run.log summarized in configs/scenario_params.csv)
    out = _apply_scenario_params(out, scenario_params)

    # If scenario config explicitly provides gps_on, prefer it
    if "gps_on_cfg" in out.columns and out["gps_on_cfg"].notna().any():
        out["is_gps_on"] = pd.to_numeric(out["gps_on_cfg"], errors="coerce").fillna(out["is_gps_on"]).astype(int)

    # Decide when the screen is considered ON for modeling.
    # User protocol: only S2 scenarios are screen ON; others are screen OFF.
    is_s2 = out["scenario"].astype(str).str.startswith("S2")
    ds = out["display_state"].astype(str).str.upper().str.strip()
    screen_on = is_s2 | ds.eq("ON")

    # If scenario config explicitly provides screen_on, prefer it
    if "screen_on_cfg" in out.columns and out["screen_on_cfg"].notna().any():
        cfg = pd.to_numeric(out["screen_on_cfg"], errors="coerce")
        screen_on = cfg.fillna(screen_on.astype(int)).astype(int).astype(bool)

    out["brightness_norm"] = (out["brightness"].clip(lower=0) / 255.0).where(out["brightness"].notna(), 0.0)
    out.loc[~screen_on, "brightness_norm"] = 0.0

    # Force screen power proxy to 0 when screen is OFF (even if brightness setting is non-zero)
    out["power_screen_mW"] = out["power_screen_mW"].where(screen_on, 0.0)

    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Build model input CSVs by aligning Perfetto power to enriched samples")
    ap.add_argument(
        "--runs-dir",
        type=Path,
        default=Path("artifacts/runs"),
        help="Directory containing *_enriched.csv",
    )
    ap.add_argument(
        "--reports-dir",
        type=Path,
        default=Path("artifacts/reports"),
        help="Directory containing per-run report folders with perfetto_android_power_timeseries.csv",
    )
    ap.add_argument(
        "--out-dir",
        type=Path,
        default=Path("artifacts/models"),
        help="Output directory for *_model_input.csv and all_runs_model_input.csv",
    )
    ap.add_argument(
        "--pattern",
        default="*_enriched.csv",
        help="Glob pattern under --runs-dir",
    )
    ap.add_argument(
        "--scenario-default",
        default=None,
        help="Fallback scenario label when missing in run CSV",
    )
    ap.add_argument(
        "--scenario-params",
        type=Path,
        default=Path("configs/scenario_params.csv"),
        help="Scenario-level experiment parameters (wifi/cellular/gps/screen/etc)",
    )

    args = ap.parse_args()

    scenario_params = _load_scenario_params(args.scenario_params)

    run_csvs = sorted(args.runs_dir.glob(args.pattern))
    if not run_csvs:
        raise SystemExit(f"No run CSVs found: {args.runs_dir}/{args.pattern}")

    args.out_dir.mkdir(parents=True, exist_ok=True)

    all_rows: list[pd.DataFrame] = []
    for run_csv in run_csvs:
        # Data curation: S2_b30 (20260201_182508) was confirmed deprecated; use S2_b30_1 instead.
        if "20260201_182508_S2_b30" in run_csv.name:
            print(f"Skip deprecated run: {run_csv.name}")
            continue

        report_dir = _find_report_dir(run_csv, args.reports_dir)
        df = _make_model_input(run_csv, report_dir, args.scenario_default, scenario_params)

        out_path = args.out_dir / f"{run_csv.stem.replace('_enriched', '')}_model_input.csv"
        df.to_csv(out_path, index=False, encoding="utf-8")
        all_rows.append(df)

        tag = "OK" if df["power_total_mW"].notna().any() else "NO_PERFETTO"
        print(f"Wrote: {out_path} ({len(df)} rows) [{tag}]")

    all_df = pd.concat(all_rows, ignore_index=True)
    all_path = args.out_dir / "all_runs_model_input.csv"
    all_df.to_csv(all_path, index=False, encoding="utf-8")
    print(f"Wrote: {all_path} ({len(all_df)} rows)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
