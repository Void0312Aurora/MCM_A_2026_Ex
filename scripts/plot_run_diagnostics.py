from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd


def _parse_ts(s: str) -> datetime | None:
    s = (s or "").strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _to_num(df: pd.DataFrame, col: str) -> pd.Series:
    if col not in df.columns:
        return pd.Series([pd.NA] * len(df))
    return pd.to_numeric(df[col], errors="coerce")


@dataclass(frozen=True)
class RunInfo:
    path: Path
    label: str
    brightness: float | None


def _infer_label(df: pd.DataFrame, fallback: str) -> tuple[str, float | None]:
    scenario = None
    if "scenario" in df.columns and len(df):
        scenario = str(df["scenario"].iloc[0])

    brightness = None
    if "brightness" in df.columns:
        b = pd.to_numeric(df["brightness"], errors="coerce").dropna()
        if len(b):
            brightness = float(b.iloc[0])

    label = scenario or fallback
    if brightness is not None and ("b" not in label):
        label = f"{label}_b{int(brightness)}"

    return label, brightness


def add_time_index(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    if "ts_pc" in out.columns:
        ts = out["ts_pc"].astype(str).map(_parse_ts)
        if hasattr(ts, "dt") and ts.notna().any():
            out["ts"] = ts
            out["t_s"] = (ts - ts.iloc[0]).dt.total_seconds()
            out = out.set_index("ts", drop=False)
            return out

    # Fallback to cumulative dt_s
    dt_s = _to_num(out, "dt_s").fillna(0.0)
    out["t_s"] = dt_s.cumsum()
    return out


def add_derived(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()

    out["battery_voltage_mv"] = _to_num(out, "battery_voltage_mv")
    out["charge_counter_uAh"] = _to_num(out, "charge_counter_uAh")
    out["dt_s"] = _to_num(out, "dt_s")
    out["battery_discharge_energy_mJ"] = _to_num(out, "battery_discharge_energy_mJ")
    out["cpu_energy_mJ_total"] = _to_num(out, "cpu_energy_mJ_total")
    out["brightness"] = _to_num(out, "brightness")

    out["batteryproperties_current_now_uA"] = _to_num(out, "batteryproperties_current_now_uA")
    out["batteryproperties_current_average_uA"] = _to_num(out, "batteryproperties_current_average_uA")

    # From charge_counter deltas (can be extremely quantized)
    d_uah = out["charge_counter_uAh"].diff()
    dt = out["t_s"].diff()
    out["d_charge_uAh"] = d_uah
    out["dt_from_ts_s"] = dt
    out["batt_discharge_current_mA_cc"] = (-d_uah * 3.6) / dt
    out["batt_discharge_power_mW_cc"] = out["batt_discharge_current_mA_cc"] * out["battery_voltage_mv"] / 1000.0

    # From per-row energy (preferred if present)
    out["batt_discharge_power_mW_energy"] = out["battery_discharge_energy_mJ"] / out["dt_s"]

    # From instantaneous current (uA) if available. Convention: many devices report discharge as negative.
    # Convert uA*mV to mW: (uA * mV) / 1e6
    out["batt_discharge_power_mW_current_now"] = (-out["batteryproperties_current_now_uA"]) * out["battery_voltage_mv"] / 1e6
    out["batt_discharge_power_mW_current_avg"] = (-out["batteryproperties_current_average_uA"]) * out["battery_voltage_mv"] / 1e6

    # CPU power (from enriched energy)
    out["cpu_power_mW_total"] = out["cpu_energy_mJ_total"] / out["dt_s"]

    return out


def _rolling_mean(s: pd.Series, df: pd.DataFrame, seconds: int) -> pd.Series:
    if "ts" in df.columns and isinstance(df.index, pd.DatetimeIndex):
        return s.rolling(f"{seconds}s", min_periods=1).mean()
    # fallback: sample-count rolling
    n = max(1, int(seconds / 3))
    return s.rolling(n, min_periods=1).mean()


def plot_single_run(df: pd.DataFrame, info: RunInfo, out_path: Path, rolling_s: int) -> None:
    cols = set(df.columns)

    fig, axes = plt.subplots(6, 1, figsize=(12, 16), sharex=True)
    x = df["t_s"] if "t_s" in cols else range(len(df))

    # 1) charge_counter (step-like)
    if "charge_counter_uAh" in cols:
        axes[0].step(x, df["charge_counter_uAh"], where="post", label="charge_counter_uAh")
    axes[0].set_ylabel("uAh")
    axes[0].set_title(info.label)
    axes[0].grid(True, alpha=0.3)
    axes[0].legend(loc="best")

    # 2) d_charge per sample
    if "d_charge_uAh" in cols:
        axes[1].plot(x, df["d_charge_uAh"], label="d_charge_uAh", color="tab:purple")
    axes[1].axhline(0, color="black", linewidth=0.8)
    axes[1].set_ylabel("uAh/step")
    axes[1].grid(True, alpha=0.3)
    axes[1].legend(loc="best")

    # 3) discharge power (raw)
    if "batt_discharge_power_mW_energy" in cols and df["batt_discharge_power_mW_energy"].notna().any():
        axes[2].plot(x, df["batt_discharge_power_mW_energy"], label="P_discharge (mW) from energy", color="tab:green", alpha=0.7)
        axes[2].plot(
            x,
            _rolling_mean(df["batt_discharge_power_mW_energy"], df, rolling_s),
            label=f"P_discharge rolling mean ({rolling_s}s)",
            color="tab:green",
            linewidth=2.0,
        )
    if "batt_discharge_power_mW_cc" in cols and df["batt_discharge_power_mW_cc"].notna().any():
        axes[2].plot(x, df["batt_discharge_power_mW_cc"], label="P_discharge (mW) from charge_counter", color="tab:olive", alpha=0.35)

    # If current_now/current_average exist, plot them (often much smoother than charge_counter deltas)
    if "batt_discharge_power_mW_current_now" in cols and df["batt_discharge_power_mW_current_now"].notna().any():
        axes[2].plot(
            x,
            df["batt_discharge_power_mW_current_now"],
            label="P_discharge (mW) from current_now",
            color="tab:cyan",
            alpha=0.35,
        )
        axes[2].plot(
            x,
            _rolling_mean(df["batt_discharge_power_mW_current_now"], df, rolling_s),
            label=f"P_current_now rolling mean ({rolling_s}s)",
            color="tab:cyan",
            linewidth=2.0,
        )
    if "batt_discharge_power_mW_current_avg" in cols and df["batt_discharge_power_mW_current_avg"].notna().any():
        axes[2].plot(
            x,
            df["batt_discharge_power_mW_current_avg"],
            label="P_discharge (mW) from current_average",
            color="tab:blue",
            alpha=0.25,
        )
    axes[2].set_ylabel("mW")
    axes[2].grid(True, alpha=0.3)
    axes[2].legend(loc="best", ncols=2)

    # 4) CPU power
    if "cpu_power_mW_total" in cols and df["cpu_power_mW_total"].notna().any():
        axes[3].plot(x, df["cpu_power_mW_total"], label="cpu_power_mW_total", color="tab:blue", alpha=0.7)
        axes[3].plot(
            x,
            _rolling_mean(df["cpu_power_mW_total"], df, rolling_s),
            label=f"cpu_power rolling mean ({rolling_s}s)",
            color="tab:blue",
            linewidth=2.0,
        )
    axes[3].set_ylabel("mW")
    axes[3].grid(True, alpha=0.3)
    axes[3].legend(loc="best")

    # 5) voltage / thermal
    if "battery_voltage_mv" in cols and df["battery_voltage_mv"].notna().any():
        axes[4].plot(x, df["battery_voltage_mv"], label="battery_voltage_mv", color="tab:red")
    thermal_cols = [c for c in df.columns if c.startswith("thermal_") and c.endswith("_C")]
    if thermal_cols:
        for c in thermal_cols:
            axes[4].plot(x, df[c], label=c, alpha=0.6)
    axes[4].set_ylabel("mV / C")
    axes[4].grid(True, alpha=0.3)
    axes[4].legend(loc="best", ncols=2)

    # 6) brightness / dt
    if "brightness" in cols and df["brightness"].notna().any():
        axes[5].plot(x, df["brightness"], label="brightness", color="tab:orange")
    if "dt_s" in cols and df["dt_s"].notna().any():
        ax2 = axes[5].twinx()
        ax2.plot(x, df["dt_s"], label="dt_s", color="tab:gray", alpha=0.4)
        ax2.set_ylabel("dt_s")
        ax2.legend(loc="upper right")
    axes[5].set_ylabel("brightness")
    axes[5].grid(True, alpha=0.3)
    axes[5].legend(loc="upper left")

    axes[-1].set_xlabel("t (s)")
    fig.tight_layout()
    fig.savefig(out_path, dpi=170)
    plt.close(fig)


def plot_overlay_power(runs: list[tuple[pd.DataFrame, RunInfo]], out_path: Path, rolling_s: int) -> None:
    fig, axes = plt.subplots(3, 1, figsize=(12, 10), sharex=True)

    for df, info in runs:
        x = df["t_s"]
        # Prefer current_now if present; fallback to energy-derived
        if "batt_discharge_power_mW_current_now" in df.columns and df["batt_discharge_power_mW_current_now"].notna().any():
            p = df["batt_discharge_power_mW_current_now"]
            axes[0].plot(x, _rolling_mean(p, df, rolling_s), label=f"{info.label} (current_now)")
        else:
            p = df["batt_discharge_power_mW_energy"]
            if p.notna().any():
                axes[0].plot(x, _rolling_mean(p, df, rolling_s), label=f"{info.label} (energy)")

        cpu = df["cpu_power_mW_total"]
        if cpu.notna().any():
            axes[1].plot(x, _rolling_mean(cpu, df, rolling_s), label=info.label)

        if "thermal_battery_C" in df.columns and pd.to_numeric(df["thermal_battery_C"], errors="coerce").notna().any():
            axes[2].plot(x, df["thermal_battery_C"], label=info.label)

    axes[0].set_ylabel(f"P_discharge rolling mean (mW, {rolling_s}s)")
    axes[1].set_ylabel(f"CPU power rolling mean (mW, {rolling_s}s)")
    axes[2].set_ylabel("thermal_battery_C")
    axes[2].set_xlabel("t (s)")

    for ax in axes:
        ax.grid(True, alpha=0.3)
        ax.legend(loc="best", ncols=2)

    fig.tight_layout()
    fig.savefig(out_path, dpi=170)
    plt.close(fig)


def _cumulative_discharge_uAh(df: pd.DataFrame) -> pd.Series:
    if "charge_counter_uAh" not in df.columns:
        return pd.Series([pd.NA] * len(df))
    cc = pd.to_numeric(df["charge_counter_uAh"], errors="coerce")
    if cc.isna().all():
        return pd.Series([pd.NA] * len(df))
    # Positive when discharging
    cum = (cc.iloc[0] - cc)
    # Guard against small positive blips / NaNs
    return cum


def _step_events(df: pd.DataFrame, info: RunInfo) -> pd.DataFrame:
    out = pd.DataFrame()
    if "d_charge_uAh" not in df.columns:
        return out

    d = pd.to_numeric(df["d_charge_uAh"], errors="coerce")
    t_s = pd.to_numeric(df["t_s"], errors="coerce")
    mask = d.notna() & (d != 0) & t_s.notna()
    if not mask.any():
        return out

    out["t_s"] = t_s[mask].to_numpy()
    out["d_charge_uAh"] = d[mask].to_numpy()
    out["d_charge_uAh_abs"] = out["d_charge_uAh"].abs()
    # convention: negative means discharge step
    out["is_discharge_step"] = out["d_charge_uAh"] < 0

    # Make sure constant metadata columns are properly repeated (avoid empty cells in CSV)
    out.insert(0, "label", [info.label] * len(out))
    out.insert(1, "brightness", [info.brightness] * len(out))
    return out


def plot_cumulative_discharge_normalized(
    runs: list[tuple[pd.DataFrame, RunInfo]],
    out_path: Path,
    points: tuple[float, ...] = (0.25, 0.5, 0.75),
) -> pd.DataFrame:
    rows: list[dict[str, object]] = []

    fig, ax = plt.subplots(1, 1, figsize=(12, 6))
    for df, info in runs:
        cum = _cumulative_discharge_uAh(df)
        t_s = pd.to_numeric(df["t_s"], errors="coerce")
        if cum.isna().all() or t_s.isna().all():
            continue

        # Normalize time and discharge to [0,1]
        t0 = float(t_s.dropna().iloc[0])
        t1 = float(t_s.dropna().iloc[-1])
        dt = max(1e-9, (t1 - t0))
        tn = (t_s - t0) / dt

        final = float(pd.to_numeric(cum, errors="coerce").dropna().iloc[-1])
        if abs(final) < 1e-9:
            continue
        dn = cum / final

        ax.step(tn, dn, where="post", label=info.label)

        # Quantify how early the discharge accumulates
        row: dict[str, object] = {
            "label": info.label,
            "brightness": info.brightness,
            "duration_s": float(t1 - t0),
            "final_discharge_uAh": float(final),
            "n_steps": int(pd.to_numeric(df["d_charge_uAh"], errors="coerce").fillna(0).ne(0).sum())
            if "d_charge_uAh" in df.columns
            else 0,
        }
        for p in points:
            # discharge fraction at time fraction p
            # take last sample with tn<=p
            m = (tn.notna()) & (dn.notna()) & (tn <= p)
            if m.any():
                row[f"discharge_frac_at_time_{p:.2f}"] = float(dn[m].iloc[-1])
            else:
                row[f"discharge_frac_at_time_{p:.2f}"] = float("nan")
        rows.append(row)

    ax.set_xlabel("Normalized time")
    ax.set_ylabel("Normalized cumulative discharge (from charge_counter)")
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.grid(True, alpha=0.3)
    ax.legend(loc="best", ncols=2)
    fig.tight_layout()
    fig.savefig(out_path, dpi=170)
    plt.close(fig)

    return pd.DataFrame(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description="Plot diagnostics curves for one or more run CSVs")
    ap.add_argument("--csv", type=Path, nargs="+", required=True, help="Input run CSV(s), prefer *_enriched.csv")
    ap.add_argument("--out-dir", type=Path, default=Path("artifacts") / "plots" / "run_diagnostics")
    ap.add_argument("--rolling-s", type=int, default=60, help="Rolling mean window (seconds)")
    args = ap.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)

    loaded: list[tuple[pd.DataFrame, RunInfo]] = []
    step_events: list[pd.DataFrame] = []
    for p in args.csv:
        df = pd.read_csv(p)
        label, brightness = _infer_label(df, p.stem)
        df = add_time_index(df)
        df = add_derived(df)
        info = RunInfo(path=p, label=label, brightness=brightness)
        loaded.append((df, info))

        ev = _step_events(df, info)
        if len(ev):
            step_events.append(ev)

        out_path = args.out_dir / f"{label}_diagnostics.png"
        plot_single_run(df, info, out_path, rolling_s=args.rolling_s)
        print(f"Wrote: {out_path}")

    # One overlay chart for quick comparison
    overlay_path = args.out_dir / "overlay_power_cpu_thermal.png"
    plot_overlay_power(loaded, overlay_path, rolling_s=args.rolling_s)
    print(f"Wrote: {overlay_path}")

    # Normalized cumulative discharge + step event export
    cum_path = args.out_dir / "overlay_cumulative_discharge_normalized.png"
    summary = plot_cumulative_discharge_normalized(loaded, cum_path)
    print(f"Wrote: {cum_path}")

    if len(summary):
        summary_path = args.out_dir / "cumulative_discharge_summary.csv"
        summary.to_csv(summary_path, index=False, encoding="utf-8")
        print(f"Wrote: {summary_path}")

    if step_events:
        events = pd.concat(step_events, ignore_index=True)
        events_path = args.out_dir / "charge_counter_step_events.csv"
        events.to_csv(events_path, index=False, encoding="utf-8")
        print(f"Wrote: {events_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
