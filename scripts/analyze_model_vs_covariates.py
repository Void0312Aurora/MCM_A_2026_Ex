from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd


def _num(df: pd.DataFrame, col: str) -> pd.Series:
    return pd.to_numeric(df.get(col, pd.Series([np.nan] * len(df))), errors="coerce")


def _pearson(x: np.ndarray, y: np.ndarray) -> float:
    if len(x) < 3:
        return float("nan")
    x = x.astype(float)
    y = y.astype(float)
    mx = np.mean(x)
    my = np.mean(y)
    vx = np.mean((x - mx) ** 2)
    vy = np.mean((y - my) ** 2)
    if vx <= 0 or vy <= 0:
        return float("nan")
    return float(np.mean((x - mx) * (y - my)) / np.sqrt(vx * vy))


def corr_table(df: pd.DataFrame, resid_col: str, cov_cols: list[str]) -> pd.DataFrame:
    rows = []
    r = _num(df, resid_col).to_numpy(float)
    for c in cov_cols:
        v = _num(df, c).to_numpy(float)
        m = np.isfinite(r) & np.isfinite(v)
        rows.append({"covariate": c, "n": int(np.sum(m)), "pearson_r": _pearson(v[m], r[m])})
    return pd.DataFrame(rows).sort_values("pearson_r", key=lambda s: np.abs(s), ascending=False)


def _load_eval(path: Path, tag: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    df = df.copy()
    df["model_tag"] = tag
    df["resid_mW"] = _num(df, "p_meas_mean_mW") - _num(df, "p_pred_mean_mW")
    df["abs_err_mW"] = np.abs(df["resid_mW"].to_numpy(float))
    return df


def _try_plots(out_dir: Path, df: pd.DataFrame, model_tag: str) -> None:
    try:
        import matplotlib.pyplot as plt
    except Exception:
        return

    covs = ["battery_level0_pct", "battery_voltage0_mV", "thermal_cpu0_C", "thermal_batt0_C"]
    for cov in covs:
        if cov not in df.columns:
            continue
        x = _num(df, cov).to_numpy(float)
        y = _num(df, "resid_mW").to_numpy(float)
        m = np.isfinite(x) & np.isfinite(y)
        if int(np.sum(m)) < 5:
            continue
        fig, ax = plt.subplots(figsize=(6.5, 4.0), dpi=160)
        ax.scatter(x[m], y[m], s=26, alpha=0.85)
        ax.axhline(0, color="black", linewidth=1.0, alpha=0.5)
        ax.set_xlabel(cov)
        ax.set_ylabel("residual (meas - pred) mW")
        ax.set_title(f"Residual vs {cov} ({model_tag})")
        ax.grid(True, alpha=0.25)
        fig.tight_layout()
        fig.savefig(out_dir / f"resid_vs_{cov}_{model_tag}.png")
        plt.close(fig)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Analyze whether model residuals correlate with start-state covariates, and compare raw/adjusted means vs model predictions."
        )
    )
    ap.add_argument("--qc-run-summary", type=Path, default=Path("artifacts/qc/qc_run_summary.csv"))
    ap.add_argument("--cov-adj", type=Path, default=Path("artifacts/qc/cov_adj/scenario_covariate_adjusted.csv"))
    ap.add_argument("--eval-v2", type=Path, default=Path("artifacts/models/eval_run_metrics_v2.csv"))
    ap.add_argument("--eval-v2-2state", type=Path, default=Path("artifacts/models/eval_run_metrics_v2_2state.csv"))
    ap.add_argument("--out-dir", type=Path, default=Path("artifacts/qc/model_vs_cov"))
    ap.add_argument("--emit-plots", action="store_true")
    args = ap.parse_args()

    qc = pd.read_csv(args.qc_run_summary)
    cov_adj = pd.read_csv(args.cov_adj) if args.cov_adj.exists() else pd.DataFrame()

    v2 = _load_eval(args.eval_v2, "v2_1state")
    v2_2 = _load_eval(args.eval_v2_2state, "v2_2state")

    # Merge start-state covariates
    keep_cols = [
        "run_name",
        "scenario",
        "qc_keep",
        "qc_reject_reasons",
        "battery_level0_pct",
        "battery_voltage0_mV",
        "thermal_cpu0_C",
        "thermal_batt0_C",
        "thermal_status0",
        "battery_plugged0",
    ]
    keep_cols = [c for c in keep_cols if c in qc.columns]
    qc_small = qc[keep_cols].copy()

    all_eval = pd.concat([v2, v2_2], ignore_index=True)
    merged = all_eval.merge(qc_small, on=["run_name", "scenario"], how="left")

    # Summary error tables
    def summarize(sub: pd.DataFrame, label: str) -> dict:
        r = _num(sub, "resid_mW").to_numpy(float)
        m = np.isfinite(r)
        if int(np.sum(m)) == 0:
            return {"subset": label, "n": 0, "mae_mW": np.nan, "rmse_mW": np.nan, "bias_mW": np.nan}
        r = r[m]
        return {
            "subset": label,
            "n": int(len(r)),
            "mae_mW": float(np.mean(np.abs(r))),
            "rmse_mW": float(np.sqrt(np.mean(r**2))),
            "bias_mW": float(np.mean(r)),
        }

    rows = []
    for tag in ["v2_1state", "v2_2state"]:
        sub = merged[merged["model_tag"] == tag]
        rows.append(summarize(sub, f"{tag}:all"))
        if "qc_keep" in sub.columns:
            sub_keep = sub[pd.to_numeric(sub["qc_keep"], errors="coerce").fillna(0).astype(int) == 1]
            rows.append(summarize(sub_keep, f"{tag}:qc_keep"))

    err_summary = pd.DataFrame(rows)

    # Correlations
    covs = ["battery_level0_pct", "battery_voltage0_mV", "thermal_cpu0_C", "thermal_batt0_C"]
    corr_rows = []
    for tag in ["v2_1state", "v2_2state"]:
        sub = merged[merged["model_tag"] == tag]
        t_all = corr_table(sub, "resid_mW", covs)
        t_all["subset"] = f"{tag}:all"
        corr_rows.append(t_all)
        if "qc_keep" in sub.columns:
            sub_keep = sub[pd.to_numeric(sub["qc_keep"], errors="coerce").fillna(0).astype(int) == 1]
            t_keep = corr_table(sub_keep, "resid_mW", covs)
            t_keep["subset"] = f"{tag}:qc_keep"
            corr_rows.append(t_keep)

    corr_summary = pd.concat(corr_rows, ignore_index=True) if corr_rows else pd.DataFrame()

    # Scenario-level compare: raw mean (from cov_adj), adjusted mean, predicted mean (avg p_pred_mean_mW over runs)
    scen_pred = (
        merged.groupby(["model_tag", "scenario"], as_index=False)
        .agg(
            n_runs=("run_name", "count"),
            pred_mean_mW=("p_pred_mean_mW", "mean"),
            meas_mean_mW=("p_meas_mean_mW", "mean"),
            mae_mW=("abs_err_mW", "mean"),
        )
        .copy()
    )

    if not cov_adj.empty:
        scen_pred = scen_pred.merge(cov_adj[["scenario", "raw_mean", "adjusted_mean", "adjustment_delta"]], on="scenario", how="left")
        scen_pred["pred_minus_adjusted_mW"] = scen_pred["pred_mean_mW"] - scen_pred["adjusted_mean"]

    args.out_dir.mkdir(parents=True, exist_ok=True)
    merged.to_csv(args.out_dir / "run_level_model_vs_covariates.csv", index=False, encoding="utf-8")
    err_summary.to_csv(args.out_dir / "model_error_summary.csv", index=False, encoding="utf-8")
    corr_summary.to_csv(args.out_dir / "residual_covariate_correlations.csv", index=False, encoding="utf-8")
    scen_pred.to_csv(args.out_dir / "scenario_level_pred_vs_adjusted.csv", index=False, encoding="utf-8")

    # Small markdown summary
    md = []
    md.append("# Model vs Start-State Covariates Analysis")
    md.append("")
    md.append("## Error summary")
    md.append("")
    md.append("```text")
    md.append(err_summary.to_csv(index=False))
    md.append("```")
    md.append("")
    md.append("## Residual-covariate correlations (Pearson r)")
    md.append("")
    md.append("Interpretation: if residual correlates with a covariate, model is not fully accounting for it (or proxy is noisy).")
    md.append("")
    md.append("```text")
    md.append(corr_summary.to_csv(index=False))
    md.append("```")

    out_md = args.out_dir / "model_vs_covariates.md"
    out_md.write_text("\n".join(md), encoding="utf-8")

    if args.emit_plots:
        for tag in ["v2_1state", "v2_2state"]:
            sub = merged[merged["model_tag"] == tag].copy()
            _try_plots(args.out_dir, sub, tag)

    print(f"Wrote: {args.out_dir / 'model_vs_covariates.md'}")
    print(f"Wrote: {args.out_dir / 'model_error_summary.csv'}")
    print(f"Wrote: {args.out_dir / 'residual_covariate_correlations.csv'}")
    print(f"Wrote: {args.out_dir / 'scenario_level_pred_vs_adjusted.csv'}")
    if args.emit_plots:
        print(f"Wrote: residual scatter plots under {args.out_dir}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
