from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import pandas as pd

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt


@dataclass(frozen=True)
class SanityReport:
    label: str
    timeseries_path: str

    n: int
    duration_s: float

    dt_s_p50: float
    dt_s_p95: float
    dt_s_max: float
    n_gaps_gt_1s: int

    charge_start_uah: float | None
    charge_end_uah: float | None
    charge_delta_uah: float | None

    current_ua_mean: float | None
    current_ua_p50: float | None
    current_ua_mean_from_charge_delta: float | None

    inferred_state: str

    suggested_current_sign: int | None

    corr_current_vs_dcharge: float | None
    rmse_current_vs_dcharge_ua: float | None

    charge_delta_from_current_uah: float | None
    charge_delta_mismatch_uah: float | None
    charge_delta_from_neg_current_uah: float | None
    charge_delta_mismatch_neg_uah: float | None

    energy_mwh_from_power: float | None
    energy_mwh_from_charge_vmean: float | None
    energy_mwh_mismatch: float | None


def _to_num(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s, errors="coerce")


def _trapz(y: np.ndarray, x: np.ndarray) -> float:
    if len(x) < 2:
        return float("nan")
    # NumPy 2.x removed np.trapz; use np.trapezoid.
    return float(np.trapezoid(y, x))


def main() -> int:
    ap = argparse.ArgumentParser(description="Sanity-check perfetto android.power battery counters timeseries")
    ap.add_argument("--timeseries", type=Path, required=True, help="perfetto_android_power_timeseries.csv")
    ap.add_argument("--out-dir", type=Path, required=True)
    ap.add_argument("--label", type=str, default="")
    args = ap.parse_args()

    ts_path: Path = args.timeseries
    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    df = pd.read_csv(ts_path)
    if "t_s" not in df.columns:
        raise SystemExit("timeseries missing t_s")

    t = _to_num(df["t_s"]).to_numpy()
    if not np.isfinite(t).any():
        raise SystemExit("t_s not finite")

    # drop leading/trailing NaNs in time
    good_t = np.isfinite(t)
    df = df.loc[good_t].copy()
    t = _to_num(df["t_s"]).to_numpy()

    # basic dt
    dt = np.diff(t)
    dt_f = dt[np.isfinite(dt)]
    dt_p50 = float(np.nanmedian(dt_f)) if len(dt_f) else float("nan")
    dt_p95 = float(np.nanpercentile(dt_f, 95)) if len(dt_f) else float("nan")
    dt_max = float(np.nanmax(dt_f)) if len(dt_f) else float("nan")
    n_gaps = int(np.sum(dt_f > 1.0)) if len(dt_f) else 0

    duration_s = float(t[-1] - t[0]) if len(t) else 0.0

    # charge + current
    charge = _to_num(df["batt.charge_uah"]) if "batt.charge_uah" in df.columns else None
    current = _to_num(df["batt.current_ua"]) if "batt.current_ua" in df.columns else None
    voltage_raw = _to_num(df["batt.voltage_uv"]) if "batt.voltage_uv" in df.columns else None

    charge_start = float(charge.dropna().iloc[0]) if charge is not None and charge.notna().any() else None
    charge_end = float(charge.dropna().iloc[-1]) if charge is not None and charge.notna().any() else None
    charge_delta = (charge_end - charge_start) if (charge_start is not None and charge_end is not None) else None

    cur_mean = float(current.mean()) if current is not None and current.notna().any() else None
    cur_p50 = float(current.quantile(0.5)) if current is not None and current.notna().any() else None

    cur_mean_from_charge = None
    if charge_delta is not None and duration_s > 1e-9:
        # I(uA) = dQ(uAh)/dt(s) * 3600
        cur_mean_from_charge = float(charge_delta / duration_s * 3600.0)

    # Infer state from charge trend first (most robust)
    inferred = "unknown"
    if charge_delta is not None:
        if charge_delta > 0:
            inferred = "charging_or_charge_counter_increasing"
        elif charge_delta < 0:
            inferred = "discharging_or_charge_counter_decreasing"
        else:
            inferred = "flat_charge"

    # Current vs d(charge)/dt sanity: I(uA) ≈ dQ(uAh)/dt(s) * 3600
    corr = None
    rmse = None
    suggested_sign: int | None = None

    charge_delta_from_current_uah = None
    charge_delta_mismatch = None
    charge_delta_from_neg_current_uah = None
    charge_delta_mismatch_neg = None

    if charge is not None and current is not None and charge.notna().any() and current.notna().any() and len(df) >= 3:
        q = charge.to_numpy(dtype=float)
        i = current.to_numpy(dtype=float)
        # derivative in uAh/s -> uA
        dq = np.diff(q)
        dt = np.diff(t)
        with np.errstate(divide="ignore", invalid="ignore"):
            i_from_dq = dq / dt * 3600.0

        # align to midpoints
        i_mid = i[1:]
        ok = np.isfinite(i_from_dq) & np.isfinite(i_mid)
        if int(np.sum(ok)) >= 10:
            corr = float(np.corrcoef(i_from_dq[ok], i_mid[ok])[0, 1])
            rmse = float(np.sqrt(np.mean((i_from_dq[ok] - i_mid[ok]) ** 2)))

        # Integrate current to predict delta charge: ΔQ(uAh) = ∫ I(uA) dt / 3600
        ok_i = np.isfinite(i) & np.isfinite(t)
        if int(np.sum(ok_i)) >= 2:
            q_from_i = _trapz(i[ok_i] / 3600.0, t[ok_i])
            charge_delta_from_current_uah = q_from_i
            if charge_delta is not None:
                charge_delta_mismatch = float(charge_delta_from_current_uah - charge_delta)

            q_from_neg_i = _trapz((-i[ok_i]) / 3600.0, t[ok_i])
            charge_delta_from_neg_current_uah = q_from_neg_i
            if charge_delta is not None:
                charge_delta_mismatch_neg = float(charge_delta_from_neg_current_uah - charge_delta)

            # Suggest which sign convention matches the charge counter trend.
            if charge_delta is not None and charge_delta_mismatch is not None and charge_delta_mismatch_neg is not None:
                suggested_sign = 1 if abs(charge_delta_mismatch) <= abs(charge_delta_mismatch_neg) else -1

    # Energy cross-check
    energy_from_power = None
    if "power_mw_calc" in df.columns:
        p_mw = _to_num(df["power_mw_calc"]).to_numpy(dtype=float)
        ok_p = np.isfinite(p_mw) & np.isfinite(t)
        if int(np.sum(ok_p)) >= 2:
            # mW*s -> mJ ; mWh = (mW*s)/3600
            energy_from_power = _trapz(p_mw[ok_p], t[ok_p]) / 3600.0

    energy_from_charge_vmean = None
    energy_mismatch = None
    if charge_delta is not None and voltage_raw is not None and voltage_raw.notna().any():
        v = voltage_raw.to_numpy(dtype=float)
        # infer scale like parse script
        v_med = float(np.nanmedian(v))
        v_to_v = 1e-3 if np.isfinite(v_med) and v_med < 100_000 else 1e-6
        v_v = v * v_to_v
        v_mean = float(np.nanmean(v_v))
        # uAh * V = uWh; /1000 -> mWh
        energy_from_charge_vmean = float(charge_delta * v_mean / 1000.0)
        if energy_from_power is not None and np.isfinite(energy_from_power):
            energy_mismatch = float(energy_from_power - energy_from_charge_vmean)

    label = args.label or ts_path.parent.name

    rep = SanityReport(
        label=label,
        timeseries_path=str(ts_path),
        n=int(len(df)),
        duration_s=duration_s,
        dt_s_p50=dt_p50,
        dt_s_p95=dt_p95,
        dt_s_max=dt_max,
        n_gaps_gt_1s=n_gaps,
        charge_start_uah=charge_start,
        charge_end_uah=charge_end,
        charge_delta_uah=charge_delta,
        current_ua_mean=cur_mean,
        current_ua_p50=cur_p50,
        current_ua_mean_from_charge_delta=cur_mean_from_charge,
        inferred_state=inferred,

        suggested_current_sign=suggested_sign,
        corr_current_vs_dcharge=corr,
        rmse_current_vs_dcharge_ua=rmse,
        charge_delta_from_current_uah=charge_delta_from_current_uah,
        charge_delta_mismatch_uah=charge_delta_mismatch,
        charge_delta_from_neg_current_uah=charge_delta_from_neg_current_uah,
        charge_delta_mismatch_neg_uah=charge_delta_mismatch_neg,
        energy_mwh_from_power=energy_from_power,
        energy_mwh_from_charge_vmean=energy_from_charge_vmean,
        energy_mwh_mismatch=energy_mismatch,
    )

    (out_dir / "perfetto_battery_sanity.json").write_text(json.dumps(asdict(rep), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # Plot
    fig, axes = plt.subplots(3, 1, figsize=(12, 10), sharex=True)

    axes[0].plot(t, _to_num(df.get("batt.current_ua", pd.Series([np.nan] * len(df)))).to_numpy(), label="batt.current_ua")
    axes[0].set_ylabel("uA")
    axes[0].grid(True, alpha=0.3)
    axes[0].legend(loc="best")

    if charge is not None:
        axes[1].plot(t, charge.to_numpy(), label="batt.charge_uah")
        if charge_start is not None:
            axes[1].plot(t, (charge.to_numpy() - charge_start), label="charge_delta_uAh", alpha=0.6)
    axes[1].set_ylabel("uAh")
    axes[1].grid(True, alpha=0.3)
    axes[1].legend(loc="best")

    if "power_mw_calc" in df.columns:
        axes[2].plot(t, _to_num(df["power_mw_calc"]).to_numpy(), label="power_mw_calc")
    axes[2].set_ylabel("mW")
    axes[2].set_xlabel("t (s)")
    axes[2].grid(True, alpha=0.3)
    axes[2].legend(loc="best")

    fig.suptitle(label)
    fig.tight_layout()
    fig.savefig(out_dir / "perfetto_battery_sanity.png", dpi=170)
    plt.close(fig)

    print(f"Wrote: {out_dir / 'perfetto_battery_sanity.json'}")
    print(f"Wrote: {out_dir / 'perfetto_battery_sanity.png'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
