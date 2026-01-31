from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import pandas as pd
from perfetto.trace_processor import TraceProcessor


@dataclass(frozen=True)
class BatteryCounterSummary:
    label: str
    trace_path: str
    n_samples: int
    duration_s: float
    sample_period_s_median: float

    charge_start_uah: float | None
    charge_end_uah: float | None
    discharge_mah: float | None

    current_ua_mean: float | None
    current_ua_p50: float | None
    current_ua_p95: float | None

    voltage_v_mean: float | None
    voltage_v_p50: float | None

    power_mw_mean: float | None
    energy_mwh: float | None


def _infer_voltage_scale(voltage_raw: pd.Series) -> float:
    """Infer whether voltage values are in mV or uV.

    Many devices expose ~3700 (mV) even if the track name says *_uv.

    Returns a multiplier to convert raw -> volts.
    """
    v = pd.to_numeric(voltage_raw, errors="coerce")
    med = float(v.dropna().median()) if v.notna().any() else float("nan")

    # Heuristic thresholds:
    # - mV: typically 3000..5000
    # - uV: typically 3_000_000..5_000_000
    if np.isfinite(med) and med < 100_000:
        return 1e-3
    return 1e-6


def _load_batt_counters(tp: TraceProcessor) -> pd.DataFrame:
    df = tp.query(
        """
        select
          c.ts as ts,
          ct.name as name,
          c.value as value
        from counter c
        join counter_track ct on ct.id = c.track_id
        where ct.name glob 'batt.*'
        order by c.ts
        """
    ).as_pandas_dataframe()

    if df.empty:
        return df

    def _last(series: pd.Series) -> float:
        s = pd.to_numeric(series, errors="coerce").dropna()
        return float(s.iloc[-1])

    piv = df.pivot_table(index="ts", columns="name", values="value", aggfunc=_last).reset_index()
    piv = piv.sort_values("ts").reset_index(drop=True)

    t0 = int(piv["ts"].iloc[0])
    piv.insert(1, "t_s", (piv["ts"] - t0) / 1e9)
    return piv


def _write_csv(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Parse Perfetto android.power battery counters from a .pftrace file. "
            "Extracts batt.current_ua / batt.voltage_uv / batt.charge_uah and computes average power + energy."
        )
    )
    parser.add_argument("--trace", type=Path, required=True, help="Input .pftrace path")
    parser.add_argument("--out-dir", type=Path, default=None, help="Output directory (default: alongside trace)")
    parser.add_argument("--label", default="", help="Label to include in outputs")
    parser.add_argument(
        "--no-timeseries",
        action="store_true",
        help="If set, do not write the per-sample timeseries CSV.",
    )
    args = parser.parse_args()

    trace: Path = args.trace
    if not trace.exists():
        raise SystemExit(f"Trace not found: {trace}")

    out_dir: Path = args.out_dir or trace.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    label = args.label or trace.stem

    with TraceProcessor(trace=str(trace)) as tp:
        ts = _load_batt_counters(tp)

    if ts.empty:
        raise SystemExit("No batt.* counter tracks found in trace. Device may not support android.power battery counters.")

    # Ensure expected columns exist.
    charge = ts.get("batt.charge_uah")
    current = ts.get("batt.current_ua")
    voltage_raw = ts.get("batt.voltage_uv")

    voltage_v = None
    if voltage_raw is not None:
        voltage_to_v = _infer_voltage_scale(voltage_raw)
        voltage_v = pd.to_numeric(voltage_raw, errors="coerce") * float(voltage_to_v)

    power_mw = None
    energy_mwh = None
    if current is not None and voltage_v is not None:
        i_a = pd.to_numeric(current, errors="coerce") * 1e-6
        p_w = i_a * voltage_v
        power_mw = p_w * 1e3
        ts["power_mw_calc"] = power_mw

        t = pd.to_numeric(ts["t_s"], errors="coerce")
        p = pd.to_numeric(power_mw, errors="coerce")

        # Trapezoidal integration; result in mWh.
        if len(t) >= 2:
            dt = t.to_numpy()[1:] - t.to_numpy()[:-1]
            p_avg = (p.to_numpy()[1:] + p.to_numpy()[:-1]) / 2.0
            energy_mwh = float(np.nansum(p_avg * dt) / 3600.0)

    duration_s = float(ts["t_s"].iloc[-1] - ts["t_s"].iloc[0]) if len(ts) else 0.0
    dt_med = float(pd.to_numeric(ts["t_s"], errors="coerce").diff().median()) if len(ts) >= 2 else float("nan")

    discharge_mah = None
    charge_start = float(charge.iloc[0]) if charge is not None and pd.notna(charge.iloc[0]) else None
    charge_end = float(charge.iloc[-1]) if charge is not None and pd.notna(charge.iloc[-1]) else None
    if charge_start is not None and charge_end is not None:
        discharge_mah = float((charge_start - charge_end) / 1000.0)

    def _quantile(series: pd.Series | None, q: float) -> float | None:
        if series is None:
            return None
        s = pd.to_numeric(series, errors="coerce").dropna()
        if s.empty:
            return None
        return float(s.quantile(q))

    cur_mean = float(pd.to_numeric(current, errors="coerce").mean()) if current is not None else None
    v_mean = float(pd.to_numeric(voltage_v, errors="coerce").mean()) if voltage_v is not None else None
    p_mean = float(pd.to_numeric(power_mw, errors="coerce").mean()) if power_mw is not None else None

    summary = BatteryCounterSummary(
        label=label,
        trace_path=str(trace),
        n_samples=int(len(ts)),
        duration_s=duration_s,
        sample_period_s_median=dt_med,
        charge_start_uah=charge_start,
        charge_end_uah=charge_end,
        discharge_mah=discharge_mah,
        current_ua_mean=cur_mean,
        current_ua_p50=_quantile(current, 0.50),
        current_ua_p95=_quantile(current, 0.95),
        voltage_v_mean=v_mean,
        voltage_v_p50=_quantile(voltage_v, 0.50) if voltage_v is not None else None,
        power_mw_mean=p_mean,
        energy_mwh=energy_mwh,
    )

    out_json = out_dir / "perfetto_android_power_summary.json"
    out_csv = out_dir / "perfetto_android_power_summary.csv"

    out_json.write_text(json.dumps(asdict(summary), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    pd.DataFrame([asdict(summary)]).to_csv(out_csv, index=False, encoding="utf-8")

    if not args.no_timeseries:
        out_ts = out_dir / "perfetto_android_power_timeseries.csv"
        _write_csv(ts, out_ts)

    print(f"Wrote: {out_json}")
    print(f"Wrote: {out_csv}")
    if not args.no_timeseries:
        print(f"Wrote: {out_dir / 'perfetto_android_power_timeseries.csv'}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
