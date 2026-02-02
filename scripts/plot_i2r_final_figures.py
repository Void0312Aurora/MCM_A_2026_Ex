from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd


def _num(s: pd.Series) -> np.ndarray:
    return pd.to_numeric(s, errors="coerce").to_numpy(float)


def _scatter(ax, x, y, title: str, xlabel: str, ylabel: str):
    m = np.isfinite(x) & np.isfinite(y)
    x = x[m]
    y = y[m]
    ax.scatter(x, y, s=28, alpha=0.8)
    ax.axhline(0.0, color="#888888", linewidth=1)
    ax.set_title(title)
    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)


def _bar_compare(ax, labels, before, after, title: str, xlabel: str, ylabel: str):
    order = np.argsort(np.nan_to_num(before, nan=-1))[::-1]
    labels = [labels[i] for i in order]
    before = before[order]
    after = after[order]

    x = np.arange(len(labels))
    w = 0.42
    ax.bar(x - w / 2, before, width=w, label="base")
    ax.bar(x + w / 2, after, width=w, label="i2r")
    ax.set_title(title)
    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    ax.set_xticks(x)
    ax.set_xticklabels(labels, rotation=45, ha="right")
    ax.legend(loc="best")


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate figures for the final I^2R internal resistance correction report.")
    ap.add_argument(
        "--run-level-csv",
        type=Path,
        default=Path("artifacts/qc/i2r_final/run_level_i2r_correction.csv"),
    )
    ap.add_argument("--out-dir", type=Path, default=Path("artifacts/reports/final_i2r/figures"))
    ap.add_argument("--use-only-qc-keep", action="store_true")

    args = ap.parse_args()

    df = pd.read_csv(args.run_level_csv)
    if args.use_only_qc_keep and "qc_keep" in df.columns:
        df = df[pd.to_numeric(df["qc_keep"], errors="coerce").fillna(0).astype(int) == 1].copy()

    # Lazily import matplotlib so the script still works for non-plot use
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    args.out_dir.mkdir(parents=True, exist_ok=True)

    resid = _num(df["resid_mW"]) if "resid_mW" in df.columns else np.full(len(df), np.nan)
    resid_corr = _num(df["resid_corr_mW"]) if "resid_corr_mW" in df.columns else np.full(len(df), np.nan)

    t_cpu0 = _num(df["thermal_cpu0_C"]) if "thermal_cpu0_C" in df.columns else np.full(len(df), np.nan)
    i_uA = _num(df["perfetto_current_mean_uA"]) if "perfetto_current_mean_uA" in df.columns else np.full(len(df), np.nan)
    v_V = _num(df["perfetto_voltage_mean_V"]) if "perfetto_voltage_mean_V" in df.columns else np.full(len(df), np.nan)
    soc0 = _num(df["battery_level0_pct"]) if "battery_level0_pct" in df.columns else np.full(len(df), np.nan)

    # 1) Residual vs temperature
    fig, axes = plt.subplots(1, 2, figsize=(11.5, 4.2), constrained_layout=True)
    _scatter(axes[0], t_cpu0, resid, "Residual vs Tcpu0 (base)", "Tcpu0 (°C)", "resid (mW)")
    _scatter(axes[1], t_cpu0, resid_corr, "Residual vs Tcpu0 (with I²R)", "Tcpu0 (°C)", "resid (mW)")
    fig.savefig(args.out_dir / "residual_vs_tcpu0.png", dpi=160)
    plt.close(fig)

    # 2) Residual vs current
    fig, axes = plt.subplots(1, 2, figsize=(11.5, 4.2), constrained_layout=True)
    _scatter(axes[0], i_uA, resid, "Residual vs current (base)", "|I| (µA)", "resid (mW)")
    _scatter(axes[1], i_uA, resid_corr, "Residual vs current (with I²R)", "|I| (µA)", "resid (mW)")
    fig.savefig(args.out_dir / "residual_vs_current.png", dpi=160)
    plt.close(fig)

    # 3) Residual vs voltage
    fig, axes = plt.subplots(1, 2, figsize=(11.5, 4.2), constrained_layout=True)
    _scatter(axes[0], v_V, resid, "Residual vs voltage (base)", "V (V)", "resid (mW)")
    _scatter(axes[1], v_V, resid_corr, "Residual vs voltage (with I²R)", "V (V)", "resid (mW)")
    fig.savefig(args.out_dir / "residual_vs_voltage.png", dpi=160)
    plt.close(fig)

    # 4) Residual vs SOC0
    fig, axes = plt.subplots(1, 2, figsize=(11.5, 4.2), constrained_layout=True)
    _scatter(axes[0], soc0, resid, "Residual vs SOC0 (base)", "SOC0 (%)", "resid (mW)")
    _scatter(axes[1], soc0, resid_corr, "Residual vs SOC0 (with I²R)", "SOC0 (%)", "resid (mW)")
    fig.savefig(args.out_dir / "residual_vs_soc0.png", dpi=160)
    plt.close(fig)

    # 5) Per-scenario MAE comparison
    if "scenario" in df.columns:
        rows = []
        for scen, g in df.groupby(df["scenario"].astype(str)):
            r = _num(g["resid_mW"]) if "resid_mW" in g.columns else np.array([])
            rc = _num(g["resid_corr_mW"]) if "resid_corr_mW" in g.columns else np.array([])
            r = r[np.isfinite(r)]
            rc = rc[np.isfinite(rc)]
            rows.append(
                {
                    "scenario": scen,
                    "n": int(len(g)),
                    "mae_base_mW": float(np.mean(np.abs(r))) if len(r) else np.nan,
                    "mae_i2r_mW": float(np.mean(np.abs(rc))) if len(rc) else np.nan,
                }
            )

        per = pd.DataFrame(rows).sort_values("mae_base_mW", ascending=False)
        per.to_csv(args.out_dir / "per_scenario_mae.csv", index=False, encoding="utf-8")

        fig, ax = plt.subplots(1, 1, figsize=(12.5, 5.2), constrained_layout=True)
        _bar_compare(
            ax,
            labels=per["scenario"].tolist(),
            before=per["mae_base_mW"].to_numpy(float),
            after=per["mae_i2r_mW"].to_numpy(float),
            title="Per-scenario MAE (base vs I²R)",
            xlabel="scenario",
            ylabel="MAE (mW)",
        )
        fig.savefig(args.out_dir / "per_scenario_mae.png", dpi=160)
        plt.close(fig)

    print(f"Wrote figures to: {args.out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
