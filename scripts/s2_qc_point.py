"""Extract a single S2 run QC + energy point.

Goal: quantify measurement *resolution / error bounds* for a 9-minute run
without relying on instantaneous current.

- From run CSV: charge_counter delta (uAh) and avg voltage (mV)
- From batterystats proto summary (if present): delta_total_mah and duration

Usage:
  python scripts/s2_qc_point.py --run artifacts/runs/20260131_154944_S2_b90.csv

Outputs to stdout as a small JSON.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path

import pandas as pd


@dataclass
class S2Point:
    run_csv: str
    scenario: str | None
    brightness: int | None

    # CSV-based
    samples: int
    duration_s_from_ts: float | None
    delta_charge_uAh: int | None
    delta_charge_mAh: float | None
    discharge_mAh_abs: float | None
    v_mean_V: float | None
    p_avg_W_from_charge_counter: float | None

    # batterystats-proto-based (if available)
    bs_dir: str | None
    bs_delta_total_mah: float | None
    bs_discharge_mah_abs: float | None
    bs_duration_s: float | None
    bs_avg_current_mA: float | None
    p_avg_W_from_batterystats: float | None

    # simple quantization bound if bs is integer mAh
    bs_quantization_rel_err_max: float | None

    # perfetto android.power (if available)
    pf_dir: str | None
    pf_duration_s: float | None
    pf_discharge_mah: float | None
    pf_energy_mwh: float | None
    pf_power_mw_mean: float | None
    pf_current_ua_mean: float | None

    # preferred
    preferred_source: str
    discharge_mAh_preferred: float | None
    p_avg_W_preferred: float | None


def _find_report_dir(run_csv: Path) -> Path | None:
    stem = run_csv.stem
    report_stem = stem if stem.endswith("_enriched") else stem + "_enriched"
    report_dir = Path("artifacts") / "reports" / report_stem
    if report_dir.exists():
        return report_dir
    return None


def _read_perfetto_summary(report_dir: Path | None) -> dict[str, float] | None:
    if report_dir is None:
        return None
    pf_csv = report_dir / "perfetto_android_power_summary.csv"
    if not pf_csv.exists():
        return None
    try:
        pf = pd.read_csv(pf_csv)
        if pf.empty:
            return None
        row = pf.iloc[0].to_dict()
        out: dict[str, float] = {}
        for k in [
            "duration_s",
            "discharge_mah",
            "energy_mwh",
            "power_mw_mean",
            "current_ua_mean",
        ]:
            v = row.get(k)
            if v is None:
                continue
            try:
                out[k] = float(v)
            except Exception:
                continue
        return out
    except Exception:
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", type=Path, required=True)
    args = ap.parse_args()

    df = pd.read_csv(args.run)
    scenario = None
    if "scenario" in df.columns and len(df) > 0:
        scenario = str(df.loc[0, "scenario"]) if pd.notna(df.loc[0, "scenario"]) else None

    brightness = None
    if "brightness" in df.columns and df["brightness"].notna().any():
        try:
            brightness = int(df["brightness"].dropna().iloc[0])
        except Exception:
            brightness = None

    duration_s = None
    if "ts_pc" in df.columns and df["ts_pc"].notna().any():
        ts = pd.to_datetime(df["ts_pc"], errors="coerce")
        if ts.notna().all():
            duration_s = float((ts.iloc[-1] - ts.iloc[0]).total_seconds())

    delta_uAh = None
    delta_mAh = None
    discharge_abs = None
    if "charge_counter_uAh" in df.columns and df["charge_counter_uAh"].notna().any():
        try:
            cc = df["charge_counter_uAh"].astype("int64")
            delta_uAh = int(cc.iloc[-1] - cc.iloc[0])
            delta_mAh = float(delta_uAh) / 1000.0
            discharge_abs = abs(delta_mAh)
        except Exception:
            pass

    v_mean_V = None
    if "battery_voltage_mv" in df.columns and df["battery_voltage_mv"].notna().any():
        v_mean_V = float(df["battery_voltage_mv"].astype(float).mean()) / 1000.0

    p_avg_charge = None
    if duration_s and duration_s > 0 and discharge_abs is not None and v_mean_V is not None:
        # mAh over duration -> A
        I_A = (discharge_abs / 1000.0) / (duration_s / 3600.0)
        p_avg_charge = I_A * v_mean_V

    report_dir = _find_report_dir(args.run)
    bs_delta_total_mah = None
    bs_discharge_abs = None
    bs_duration_s = None
    bs_avg_current_mA = None
    p_avg_bs = None
    bs_rel_err = None

    if report_dir is not None:
        bs_csv = report_dir / "batterystats_proto_min_summary.csv"
        if bs_csv.exists():
            bs = pd.read_csv(bs_csv)
            m = dict(zip(bs["metric"].astype(str), bs["value"].astype(str)))

            def f(key: str) -> float | None:
                s = m.get(key)
                if s is None:
                    return None
                s = s.strip()
                if not s:
                    return None
                try:
                    return float(s)
                except Exception:
                    return None

            bs_delta_total_mah = f("delta_total_mah")
            bs_discharge_abs = abs(bs_delta_total_mah) if bs_delta_total_mah is not None else None
            bs_duration_s = f("derived_duration_s")
            bs_avg_current_mA = f("derived_avg_current_mA")
            if bs_avg_current_mA is None and bs_delta_total_mah is not None and bs_duration_s and bs_duration_s > 0:
                bs_avg_current_mA = (abs(bs_delta_total_mah) / (bs_duration_s / 3600.0))

            if bs_discharge_abs is not None and bs_duration_s and bs_duration_s > 0 and v_mean_V is not None:
                I_A = (bs_discharge_abs / 1000.0) / (bs_duration_s / 3600.0)
                p_avg_bs = I_A * v_mean_V

            # If bs_delta_total_mah is integer (typical), quantization step is 1 mAh.
            if bs_discharge_abs is not None and bs_discharge_abs > 0:
                # worst-case relative error from rounding to nearest mAh: +/-0.5 mAh
                bs_rel_err = 0.5 / bs_discharge_abs

    pf = _read_perfetto_summary(report_dir)
    pf_duration_s = pf.get("duration_s") if pf else None
    pf_discharge_mah = pf.get("discharge_mah") if pf else None
    pf_energy_mwh = pf.get("energy_mwh") if pf else None
    pf_power_mw_mean = pf.get("power_mw_mean") if pf else None
    pf_current_ua_mean = pf.get("current_ua_mean") if pf else None

    preferred_source = "charge_counter"
    discharge_pref = discharge_abs
    p_avg_pref = p_avg_charge
    if pf_discharge_mah is not None or pf_power_mw_mean is not None:
        preferred_source = "perfetto"
        discharge_pref = pf_discharge_mah
        p_avg_pref = (pf_power_mw_mean / 1000.0) if pf_power_mw_mean is not None else None
    elif bs_discharge_abs is not None:
        preferred_source = "batterystats"
        discharge_pref = bs_discharge_abs
        p_avg_pref = p_avg_bs

    point = S2Point(
        run_csv=str(args.run).replace("\\", "/"),
        scenario=scenario,
        brightness=brightness,
        samples=int(len(df)),
        duration_s_from_ts=duration_s,
        delta_charge_uAh=delta_uAh,
        delta_charge_mAh=delta_mAh,
        discharge_mAh_abs=discharge_abs,
        v_mean_V=v_mean_V,
        p_avg_W_from_charge_counter=p_avg_charge,
        bs_dir=str(report_dir).replace("\\", "/") if report_dir is not None else None,
        bs_delta_total_mah=bs_delta_total_mah,
        bs_discharge_mah_abs=bs_discharge_abs,
        bs_duration_s=bs_duration_s,
        bs_avg_current_mA=bs_avg_current_mA,
        p_avg_W_from_batterystats=p_avg_bs,
        bs_quantization_rel_err_max=bs_rel_err,

        pf_dir=str(report_dir).replace("\\", "/") if report_dir is not None else None,
        pf_duration_s=pf_duration_s,
        pf_discharge_mah=pf_discharge_mah,
        pf_energy_mwh=pf_energy_mwh,
        pf_power_mw_mean=pf_power_mw_mean,
        pf_current_ua_mean=pf_current_ua_mean,

        preferred_source=preferred_source,
        discharge_mAh_preferred=discharge_pref,
        p_avg_W_preferred=p_avg_pref,
    )

    print(json.dumps(asdict(point), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
