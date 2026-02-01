from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd


@dataclass(frozen=True)
class Point:
    csv: Path
    brightness: float
    perfetto_power_mw_mean: float | None
    perfetto_energy_mwh: float | None
    perfetto_discharge_mah: float | None
    perfetto_duration_s: float | None

    perfetto_power_mw_mean_trim: float | None
    perfetto_energy_mwh_trim: float | None
    perfetto_discharge_mah_trim: float | None
    perfetto_duration_s_trim: float | None


def _to_float(v: object) -> float | None:
    try:
        if v is None:
            return None
        # Some values may arrive as numpy scalars / strings; coercing via str keeps it robust.
        f = float(v) if isinstance(v, (int, float)) else float(str(v))
        if f != f:
            return None
        return f
    except Exception:
        return None


def _find_report_dir(csv_path: Path) -> Path:
    stem = csv_path.stem
    report_stem = stem if stem.endswith("_enriched") else stem + "_enriched"
    return Path("artifacts") / "reports" / report_stem


def _read_perfetto_summary(report_dir: Path) -> dict[str, float] | None:
    pf_csv = report_dir / "perfetto_android_power_summary.csv"
    if not pf_csv.exists():
        return None
    try:
        df = pd.read_csv(pf_csv)
        if df.empty:
            return None
        row = df.iloc[0].to_dict()
        out: dict[str, float] = {}
        for k in ["power_mw_mean", "energy_mwh", "discharge_mah", "duration_s"]:
            val = _to_float(row.get(k))
            if val is not None:
                out[k] = val
        return out
    except Exception:
        return None


def _read_perfetto_timeseries(report_dir: Path) -> pd.DataFrame | None:
    ts = report_dir / "perfetto_android_power_timeseries.csv"
    if not ts.exists():
        return None
    try:
        df = pd.read_csv(ts)
    except Exception:
        return None
    if "t_s" not in df.columns:
        return None

    keep = [c for c in ["t_s", "power_mw_calc", "batt.charge_uah"] if c in df.columns]
    df = df[keep].copy()
    for c in keep:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.sort_values("t_s").reset_index(drop=True)
    return df


def _compute_trimmed_from_timeseries(ts: pd.DataFrame, trim_s: float) -> dict[str, float] | None:
    if ts is None or ts.empty:
        return None
    if "t_s" not in ts.columns:
        return None

    t = pd.to_numeric(ts["t_s"], errors="coerce")
    mask = t.notna() & (t >= float(trim_s))
    sub = ts.loc[mask].copy()
    if sub.empty:
        return None

    # Duration
    t2 = pd.to_numeric(sub["t_s"], errors="coerce")
    t2 = t2.dropna()
    if len(t2) < 2:
        return None
    duration_s = float(t2.iloc[-1] - t2.iloc[0])
    if duration_s <= 0:
        return None

    out: dict[str, float] = {"duration_s": duration_s}

    # Discharge mAh from charge counter if available
    if "batt.charge_uah" in sub.columns:
        ch = pd.to_numeric(sub["batt.charge_uah"], errors="coerce").dropna()
        if len(ch) >= 2:
            discharge_mah = float((float(ch.iloc[0]) - float(ch.iloc[-1])) / 1000.0)
            out["discharge_mah"] = discharge_mah

    # Energy + mean power from power_mw_calc if available (dt-weighted trapezoid)
    if "power_mw_calc" in sub.columns:
        p = pd.to_numeric(sub["power_mw_calc"], errors="coerce")
        # Align arrays for trapezoid integration
        t_arr = t2.to_numpy()
        p_arr = pd.to_numeric(sub.loc[t2.index, "power_mw_calc"], errors="coerce").to_numpy()
        # Need consecutive valid pairs
        import numpy as np

        dt = t_arr[1:] - t_arr[:-1]
        p0 = p_arr[:-1]
        p1 = p_arr[1:]
        valid = np.isfinite(dt) & (dt > 0) & np.isfinite(p0) & np.isfinite(p1)
        if valid.any():
            energy_mwh = float(np.nansum(((p0[valid] + p1[valid]) / 2.0) * dt[valid]) / 3600.0)
            out["energy_mwh"] = energy_mwh
            out["power_mw_mean"] = float(energy_mwh * 3600.0 / duration_s)
        else:
            # fallback: simple mean if dt-weighted not possible
            if p.notna().any():
                out["power_mw_mean"] = float(p.dropna().mean())
    return out


def _read_brightness(csv_path: Path) -> float:
    df = pd.read_csv(csv_path, usecols=["brightness"])
    b = pd.to_numeric(df["brightness"], errors="coerce").dropna()
    if b.empty:
        raise SystemExit(f"No brightness column/value in: {csv_path}")
    return float(b.iloc[0])


def main() -> int:
    ap = argparse.ArgumentParser(description="Build S2 brightness curve using Perfetto android.power summaries")
    ap.add_argument("--csv", type=Path, nargs="+", required=True, help="S2 run CSVs (prefer *_enriched.csv)")
    ap.add_argument("--out-dir", type=Path, default=Path("artifacts") / "plots" / "s2_brightness_curve")
    ap.add_argument("--title", default="S2 brightness curve (Perfetto android.power)")
    ap.add_argument(
        "--trim-s",
        type=float,
        default=120.0,
        help="Drop the first N seconds when computing Perfetto power/energy from timeseries (default: 120).",
    )
    args = ap.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)

    points: list[Point] = []
    for p in args.csv:
        brightness = _read_brightness(p)
        report_dir = _find_report_dir(p)
        pf = _read_perfetto_summary(report_dir)
        ts = _read_perfetto_timeseries(report_dir)
        pf_trim = _compute_trimmed_from_timeseries(ts, trim_s=float(args.trim_s)) if ts is not None else None
        points.append(
            Point(
                csv=p,
                brightness=brightness,
                perfetto_power_mw_mean=_to_float(pf.get("power_mw_mean")) if pf else None,
                perfetto_energy_mwh=_to_float(pf.get("energy_mwh")) if pf else None,
                perfetto_discharge_mah=_to_float(pf.get("discharge_mah")) if pf else None,
                perfetto_duration_s=_to_float(pf.get("duration_s")) if pf else None,

                perfetto_power_mw_mean_trim=_to_float(pf_trim.get("power_mw_mean")) if pf_trim else None,
                perfetto_energy_mwh_trim=_to_float(pf_trim.get("energy_mwh")) if pf_trim else None,
                perfetto_discharge_mah_trim=_to_float(pf_trim.get("discharge_mah")) if pf_trim else None,
                perfetto_duration_s_trim=_to_float(pf_trim.get("duration_s")) if pf_trim else None,
            )
        )

    rows = [
        {
            "csv": pt.csv.as_posix(),
            "brightness": pt.brightness,
            "perfetto_power_mw_mean": pt.perfetto_power_mw_mean,
            "perfetto_energy_mwh": pt.perfetto_energy_mwh,
            "perfetto_discharge_mah": pt.perfetto_discharge_mah,
            "perfetto_duration_s": pt.perfetto_duration_s,

            "trim_s": float(args.trim_s),
            "perfetto_power_mw_mean_trim": pt.perfetto_power_mw_mean_trim,
            "perfetto_energy_mwh_trim": pt.perfetto_energy_mwh_trim,
            "perfetto_discharge_mah_trim": pt.perfetto_discharge_mah_trim,
            "perfetto_duration_s_trim": pt.perfetto_duration_s_trim,
        }
        for pt in points
    ]
    out_csv = args.out_dir / f"s2_perfetto_brightness_curve_trim{int(args.trim_s)}s.csv"
    df_out = pd.DataFrame(rows).sort_values("brightness")
    df_out.to_csv(out_csv, index=False, encoding="utf-8")

    fig, ax = plt.subplots(1, 1, figsize=(9.5, 5.5))

    x = pd.to_numeric(df_out["brightness"], errors="coerce")
    y = pd.to_numeric(df_out["perfetto_power_mw_mean_trim"], errors="coerce")

    ax.plot(x, y, marker="o", linewidth=2.0, label=f"perfetto power_mw_mean (t>={int(args.trim_s)}s)")

        # Optional: simple linear fit (only if we have enough distinct x values)
        if y.notna().sum() >= 3:
            fit = df_out.dropna(subset=["brightness", "perfetto_power_mw_mean_trim"])
            if len(fit) >= 3 and fit["brightness"].nunique() >= 3:
                import numpy as np

                coeff = np.polyfit(fit["brightness"].to_numpy(), fit["perfetto_power_mw_mean_trim"].to_numpy(), deg=1)
                xline = np.linspace(float(fit["brightness"].min()), float(fit["brightness"].max()), 100)
                yline = coeff[0] * xline + coeff[1]
                ax.plot(xline, yline, linestyle="--", color="tab:gray", label=f"linear fit: {coeff[0]:.2f} mW/brightness")

    ax.set_title(args.title)
    ax.set_xlabel("brightness (0-255)")
    ax.set_ylabel("battery power (mW)")
    ax.grid(True, alpha=0.3)
    ax.legend(loc="best")

    out_png = args.out_dir / f"s2_perfetto_brightness_curve_trim{int(args.trim_s)}s.png"
    fig.tight_layout()
    fig.savefig(out_png, dpi=170)
    plt.close(fig)

    print(f"Wrote: {out_csv}")
    print(f"Wrote: {out_png}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
