from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd


def _num(s: pd.Series) -> np.ndarray:
    return pd.to_numeric(s, errors="coerce").to_numpy(float)


def _fit_ridge(X: np.ndarray, y: np.ndarray, alpha: float) -> np.ndarray:
    # Closed-form ridge: (X^T X + alpha I)^-1 X^T y
    p = X.shape[1]
    A = X.T @ X + alpha * np.eye(p)
    b = X.T @ y
    return np.linalg.solve(A, b)


def _metrics(resid: np.ndarray) -> dict:
    m = np.isfinite(resid)
    r = resid[m]
    if len(r) == 0:
        return {"n": 0, "mae_mW": np.nan, "rmse_mW": np.nan, "bias_mW": np.nan}
    return {
        "n": int(len(r)),
        "mae_mW": float(np.mean(np.abs(r))),
        "rmse_mW": float(np.sqrt(np.mean(r**2))),
        "bias_mW": float(np.mean(r)),
    }


def _scenario_metrics(df: pd.DataFrame, resid_col: str) -> pd.DataFrame:
    out_rows = []
    for scen, g in df.groupby(df["scenario"].astype(str)):
        r = pd.to_numeric(g[resid_col], errors="coerce").to_numpy(float)
        m = np.isfinite(r)
        r = r[m]
        if len(r) == 0:
            continue
        out_rows.append(
            {
                "scenario": str(scen),
                "n": int(len(r)),
                "mae_mW": float(np.mean(np.abs(r))),
                "rmse_mW": float(np.sqrt(np.mean(r**2))),
                "bias_mW": float(np.mean(r)),
            }
        )
    if not out_rows:
        return pd.DataFrame()
    return pd.DataFrame(out_rows).sort_values("rmse_mW", ascending=False)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Fit a simple residual correction model using LOSO by scenario: "
            "(meas - pred) ~ start-state covariates, trained on other scenarios, then applied to held-out scenario. "
            "This is a low-cost way to absorb start-state effects without re-testing."
        )
    )
    ap.add_argument("--eval-run-metrics", type=Path, default=Path("artifacts/models/eval_run_metrics_v2.csv"))
    ap.add_argument("--qc-run-summary", type=Path, default=Path("artifacts/qc/qc_run_summary.csv"))
    ap.add_argument("--out-dir", type=Path, default=Path("artifacts/qc/resid_correction"))
    ap.add_argument("--use-only-qc-keep", action="store_true")
    ap.add_argument("--alpha", type=float, default=1e-3, help="Ridge regularization strength")
    ap.add_argument("--covariate", action="append", default=[], help="Covariates to use (default: SOC/voltage/thermal cpu/batt when present)")
    args = ap.parse_args()

    eval_df = pd.read_csv(args.eval_run_metrics)
    qc = pd.read_csv(args.qc_run_summary)

    # Merge start-state covariates
    base_cols = [
        "run_name",
        "scenario",
        "qc_keep",
        "battery_level0_pct",
        "battery_voltage0_mV",
        "thermal_cpu0_C",
        "thermal_batt0_C",
    ]
    base_cols = [c for c in base_cols if c in qc.columns]
    df = eval_df.merge(qc[base_cols], on=["run_name", "scenario"], how="left")

    if args.use_only_qc_keep and "qc_keep" in df.columns:
        df = df[pd.to_numeric(df["qc_keep"], errors="coerce").fillna(0).astype(int) == 1].copy()

    # Residual from existing out-of-fold predictions
    df["resid_mW"] = _num(df["p_meas_mean_mW"]) - _num(df["p_pred_mean_mW"])

    # Default covariates
    cov = list(args.covariate)
    if not cov:
        for c in ["battery_level0_pct", "battery_voltage0_mV", "thermal_cpu0_C", "thermal_batt0_C"]:
            if c in df.columns:
                cov.append(c)

    # Convert voltage to V for numeric stability
    df = df.copy()
    if "battery_voltage0_mV" in cov:
        df["voltage_V"] = pd.to_numeric(df["battery_voltage0_mV"], errors="coerce") / 1000.0
        cov = ["voltage_V" if c == "battery_voltage0_mV" else c for c in cov]

    # Prepare design matrix helper
    def design(sub: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
        cols = ["intercept"] + cov
        X = np.column_stack([np.ones(len(sub))] + [pd.to_numeric(sub[c], errors="coerce").to_numpy(float) for c in cov])
        y = pd.to_numeric(sub["resid_mW"], errors="coerce").to_numpy(float)
        m = np.isfinite(y)
        for j in range(X.shape[1]):
            m &= np.isfinite(X[:, j])
        return X[m], y[m]

    scenarios = sorted(df["scenario"].astype(str).unique())

    corrected_rows = []
    for scen in scenarios:
        train = df[df["scenario"].astype(str) != scen]
        test = df[df["scenario"].astype(str) == scen]

        Xtr, ytr = design(train)
        if len(ytr) < (Xtr.shape[1] + 1):
            # Not enough data to fit; skip correction for this scenario
            df.loc[test.index, "resid_hat_mW"] = 0.0
            df.loc[test.index, "resid_corr_mW"] = df.loc[test.index, "resid_mW"]
            continue

        beta = _fit_ridge(Xtr, ytr, float(args.alpha))

        # Predict residual for test
        Xt = np.column_stack([np.ones(len(test))] + [pd.to_numeric(test[c], errors="coerce").to_numpy(float) for c in cov])
        rhat = Xt @ beta
        df.loc[test.index, "resid_hat_mW"] = rhat
        df.loc[test.index, "resid_corr_mW"] = df.loc[test.index, "resid_mW"] - rhat

        corrected_rows.append({"scenario": scen, "n_train": int(len(ytr)), "beta": beta.tolist()})

    base = _metrics(pd.to_numeric(df["resid_mW"], errors="coerce").to_numpy(float))
    corr = _metrics(pd.to_numeric(df["resid_corr_mW"], errors="coerce").to_numpy(float))

    # Build corrected prediction at run-level mean
    df["p_pred_mean_mW_corr"] = pd.to_numeric(df["p_pred_mean_mW"], errors="coerce") + pd.to_numeric(df["resid_hat_mW"], errors="coerce")
    df["resid_corr_check_mW"] = pd.to_numeric(df["p_meas_mean_mW"], errors="coerce") - pd.to_numeric(df["p_pred_mean_mW_corr"], errors="coerce")

    # Scenario-level metrics
    scen_base = _scenario_metrics(df, "resid_mW")
    scen_corr = _scenario_metrics(df, "resid_corr_mW")

    summary = pd.DataFrame(
        [
            {"metric": "base", **base},
            {"metric": "corrected", **corr},
        ]
    )

    args.out_dir.mkdir(parents=True, exist_ok=True)
    df.to_csv(args.out_dir / "run_level_residual_correction.csv", index=False, encoding="utf-8")
    summary.to_csv(args.out_dir / "residual_correction_summary.csv", index=False, encoding="utf-8")

    if not scen_base.empty:
        scen_base.to_csv(args.out_dir / "scenario_metrics_base.csv", index=False, encoding="utf-8")
    if not scen_corr.empty:
        scen_corr.to_csv(args.out_dir / "scenario_metrics_corrected.csv", index=False, encoding="utf-8")

    md = []
    md.append("# Residual Correction (LOSO) Report")
    md.append("")
    md.append(f"Source eval: `{args.eval_run_metrics.as_posix()}`")
    md.append(f"QC keep only: {bool(args.use_only_qc_keep)}")
    md.append(f"Covariates: {cov}")
    md.append("")
    md.append("## Summary")
    md.append("")
    md.append("```text")
    md.append(summary.to_csv(index=False))
    md.append("```")

    if not scen_base.empty and not scen_corr.empty:
        md.append("")
        md.append("## Worst scenarios by RMSE (base vs corrected)")
        md.append("")
        md.append("```text")
        topn = 10
        b = scen_base.head(topn).copy()
        b.insert(0, "metric", "base")
        c = scen_corr.head(topn).copy()
        c.insert(0, "metric", "corrected")
        md.append(pd.concat([b, c], ignore_index=True).to_csv(index=False))
        md.append("```")

    (args.out_dir / "residual_correction.md").write_text("\n".join(md), encoding="utf-8")

    print(f"Wrote: {args.out_dir / 'residual_correction.md'}")
    print(f"Wrote: {args.out_dir / 'residual_correction_summary.csv'}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
