from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd


def _num(s: pd.Series) -> np.ndarray:
    return pd.to_numeric(s, errors="coerce").to_numpy(float)


def _metrics(resid_mW: np.ndarray) -> dict:
    m = np.isfinite(resid_mW)
    r = resid_mW[m]
    if len(r) == 0:
        return {"n": 0, "mae_mW": np.nan, "rmse_mW": np.nan, "bias_mW": np.nan}
    return {
        "n": int(len(r)),
        "mae_mW": float(np.mean(np.abs(r))),
        "rmse_mW": float(np.sqrt(np.mean(r**2))),
        "bias_mW": float(np.mean(r)),
    }


def _pearson(x: np.ndarray, y: np.ndarray) -> float:
    m = np.isfinite(x) & np.isfinite(y)
    if int(np.sum(m)) < 3:
        return float("nan")
    x = x[m].astype(float)
    y = y[m].astype(float)
    x = x - np.mean(x)
    y = y - np.mean(y)
    den = np.sqrt(np.mean(x * x) * np.mean(y * y))
    return float(np.mean(x * y) / den) if den > 0 else float("nan")


def _ols_beta(X: np.ndarray, y: np.ndarray, ridge: float = 1e-6) -> np.ndarray:
    # (X^T X + ridge I)^{-1} X^T y
    p = X.shape[1]
    A = X.T @ X + ridge * np.eye(p)
    b = X.T @ y
    return np.linalg.solve(A, b)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Test a physical correction term based on internal resistance loss: P_loss = I^2 * R_int. "
            "Fits R_int (simple parametric form) and evaluates by LOSO across scenarios using existing artifacts."
        )
    )
    ap.add_argument("--eval-run-metrics", type=Path, default=Path("artifacts/models/eval_run_metrics_v2.csv"))
    ap.add_argument("--qc-run-summary", type=Path, default=Path("artifacts/qc/qc_run_summary.csv"))
    ap.add_argument("--out-dir", type=Path, default=Path("artifacts/qc/i2r_loss"))

    ap.add_argument("--use-only-qc-keep", action="store_true")
    ap.add_argument("--tref-C", type=float, default=40.0)
    ap.add_argument("--ridge", type=float, default=1e-6)
    ap.add_argument(
        "--fit-scale",
        action="store_true",
        help=(
            "Fit a nonnegative scalar s per fold on training data and apply P_loss <- s*P_loss. "
            "This helps avoid double-counting when the base model already captures part of I^2R."
        ),
    )

    ap.add_argument(
        "--model",
        choices=["R0", "R0_Rsoc", "R0_Rsoc_Rtpos"],
        default="R0_Rsoc_Rtpos",
        help=(
            "R_int parameterization: "
            "R0 (constant), R0+R1*(1-SOC), or add R2*max(0,Tcpu-Tref)."
        ),
    )

    args = ap.parse_args()

    eval_df = pd.read_csv(args.eval_run_metrics)
    qc = pd.read_csv(args.qc_run_summary)

    # Merge needed covariates + perfetto current/voltage
    cols = [
        "run_name",
        "scenario",
        "qc_keep",
        "battery_level0_pct",
        "thermal_cpu0_C",
        "perfetto_current_mean_uA",
        "perfetto_voltage_mean_V",
    ]
    cols = [c for c in cols if c in qc.columns]
    df = eval_df.merge(qc[cols], on=["run_name", "scenario"], how="left")

    if args.use_only_qc_keep and "qc_keep" in df.columns:
        df = df[pd.to_numeric(df["qc_keep"], errors="coerce").fillna(0).astype(int) == 1].copy()

    # Base residual (meas - pred)
    p_meas = _num(df["p_meas_mean_mW"])
    p_pred = _num(df["p_pred_mean_mW"])
    resid = p_meas - p_pred

    # Current (A) and I^2
    i_uA = _num(df.get("perfetto_current_mean_uA", pd.Series([np.nan] * len(df))))
    i_A = np.abs(i_uA) / 1e6
    i2 = i_A * i_A

    soc = _num(df.get("battery_level0_pct", pd.Series([np.nan] * len(df)))) / 100.0
    t_cpu = _num(df.get("thermal_cpu0_C", pd.Series([np.nan] * len(df))))

    # Build design matrix for P_loss (W): y = I^2 * R_int
    # Use only points with finite needed fields and discharging current
    m = np.isfinite(resid) & np.isfinite(i2) & (i2 > 1e-8)

    # Physical constraint: I^2R only adds power -> fit on positive residual part
    yW = np.maximum(0.0, resid / 1000.0)

    # scenario list for LOSO
    scenarios = sorted(df.loc[m, "scenario"].astype(str).unique())

    # Columns depend on model
    def design(mask: np.ndarray) -> np.ndarray:
        X_cols = [i2[mask]]
        if args.model in ("R0_Rsoc", "R0_Rsoc_Rtpos"):
            X_cols.append(i2[mask] * (1.0 - soc[mask]))
        if args.model == "R0_Rsoc_Rtpos":
            dt = np.maximum(0.0, t_cpu[mask] - float(args.tref_C))
            X_cols.append(i2[mask] * dt)
        return np.column_stack(X_cols)

    # Fit + apply LOSO
    beta_rows = []
    p_loss_hat_W = np.full(len(df), np.nan)

    for scen in scenarios:
        test_mask = m & (df["scenario"].astype(str) == scen).to_numpy()
        train_mask = m & (df["scenario"].astype(str) != scen).to_numpy()

        Xtr = design(train_mask)
        ytr = yW[train_mask]

        if len(ytr) < (Xtr.shape[1] + 1):
            # Not enough data; predict zero loss
            p_loss_hat_W[test_mask] = 0.0
            continue

        beta = _ols_beta(Xtr, ytr, ridge=float(args.ridge))
        # Enforce non-negative parameters (physical)
        beta = np.maximum(beta, 0.0)

        # Optional scaling: use training residuals (not clipped) to avoid systematic over-correction.
        # s = argmin ||resid_W - s*(X beta)||^2, s>=0
        s = 1.0
        if args.fit_scale:
            residW_tr = (resid[train_mask] / 1000.0).astype(float)
            yhat_tr = (Xtr @ beta).astype(float)
            den = float(np.dot(yhat_tr, yhat_tr))
            if den > 0:
                s = float(np.dot(yhat_tr, residW_tr) / den)
                s = max(0.0, s)

        Xt = design(test_mask)
        yhat = (Xt @ beta) * float(s)
        p_loss_hat_W[test_mask] = np.maximum(0.0, yhat)

        beta_rows.append(
            {
                "scenario": scen,
                "scale_s": float(s),
                **{f"beta_{k}": float(v) for k, v in enumerate(beta)},
            }
        )

    # Corrected predictions
    p_pred_corr = p_pred + 1000.0 * p_loss_hat_W
    resid_corr = p_meas - p_pred_corr

    # Summaries
    base = _metrics(resid)
    corr = _metrics(resid_corr)

    # Correlations with start-state proxies (should drop if i2r captures it)
    corr_table = []
    for cov_name, cov in [
        ("thermal_cpu0_C", t_cpu),
        ("battery_level0_pct", soc * 100.0),
        ("perfetto_voltage_mean_V", _num(df.get("perfetto_voltage_mean_V", pd.Series([np.nan] * len(df))))),
        ("perfetto_current_mean_uA", i_uA),
    ]:
        corr_table.append(
            {
                "covariate": cov_name,
                "r_before": _pearson(cov, resid),
                "r_after": _pearson(cov, resid_corr),
            }
        )

    args.out_dir.mkdir(parents=True, exist_ok=True)

    out_df = df.copy()
    out_df["resid_mW"] = resid
    out_df["p_loss_hat_W"] = p_loss_hat_W
    out_df["p_pred_mean_mW_corr_i2r"] = p_pred_corr
    out_df["resid_corr_mW"] = resid_corr
    out_df.to_csv(args.out_dir / "run_level_i2r_correction.csv", index=False, encoding="utf-8")

    pd.DataFrame([{"metric": "base", **base}, {"metric": "i2r_corrected", **corr}]).to_csv(
        args.out_dir / "i2r_correction_summary.csv", index=False, encoding="utf-8"
    )
    pd.DataFrame(corr_table).to_csv(args.out_dir / "i2r_residual_correlations.csv", index=False, encoding="utf-8")

    if beta_rows:
        pd.DataFrame(beta_rows).to_csv(args.out_dir / "i2r_beta_by_heldout_scenario.csv", index=False, encoding="utf-8")

    md = []
    md.append("# I^2 R_int Loss Term Test (LOSO)")
    md.append("")
    md.append(f"Source eval: `{args.eval_run_metrics.as_posix()}`")
    md.append(f"QC keep only: {bool(args.use_only_qc_keep)}")
    md.append(f"R_int model: `{args.model}` (Tref={float(args.tref_C):.1f}C); fit_scale={bool(args.fit_scale)}")
    md.append("")
    md.append("## Error summary")
    md.append("")
    md.append("```text")
    md.append(pd.read_csv(args.out_dir / "i2r_correction_summary.csv").to_csv(index=False))
    md.append("```")
    md.append("")
    md.append("## Residual correlations")
    md.append("")
    md.append("```text")
    md.append(pd.read_csv(args.out_dir / "i2r_residual_correlations.csv").to_csv(index=False))
    md.append("```")

    (args.out_dir / "i2r_correction.md").write_text("\n".join(md), encoding="utf-8")

    print(f"Wrote: {args.out_dir / 'i2r_correction.md'}")
    print(f"Wrote: {args.out_dir / 'run_level_i2r_correction.csv'}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
