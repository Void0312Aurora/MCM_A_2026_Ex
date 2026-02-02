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
    p = X.shape[1]
    A = X.T @ X + ridge * np.eye(p)
    b = X.T @ y
    return np.linalg.solve(A, b)


def _read_run_temp_means(runs_dir: Path, run_name: str) -> dict:
    path = runs_dir / f"{run_name}_enriched.csv"
    if not path.exists():
        return {}

    # Chunked mean to avoid large memory usage
    cols = ["thermal_cpu_C", "thermal_battery_C"]
    sums = {c: 0.0 for c in cols}
    counts = {c: 0 for c in cols}

    try:
        for chunk in pd.read_csv(path, usecols=lambda c: c in cols, chunksize=50_000):
            for c in cols:
                if c not in chunk.columns:
                    continue
                v = pd.to_numeric(chunk[c], errors="coerce").to_numpy(float)
                m = np.isfinite(v)
                if int(np.sum(m)) == 0:
                    continue
                sums[c] += float(np.sum(v[m]))
                counts[c] += int(np.sum(m))
    except Exception:
        return {}

    out = {}
    if counts["thermal_cpu_C"] > 0:
        out["thermal_cpu_mean_C"] = sums["thermal_cpu_C"] / counts["thermal_cpu_C"]
    if counts["thermal_battery_C"] > 0:
        out["thermal_batt_mean_C"] = sums["thermal_battery_C"] / counts["thermal_battery_C"]
    return out


def _attach_temp_means(df: pd.DataFrame, runs_dir: Path) -> pd.DataFrame:
    rows = []
    for rn in df["run_name"].astype(str).unique():
        d = _read_run_temp_means(runs_dir, rn)
        if not d:
            continue
        d["run_name"] = rn
        rows.append(d)
    if not rows:
        return df
    means = pd.DataFrame(rows)
    return df.merge(means, on="run_name", how="left")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Test physical leakage and background baseline correction terms (no generic regression on many covariates). "
            "Uses LOSO by scenario against existing eval predictions."
        )
    )
    ap.add_argument("--eval-run-metrics", type=Path, default=Path("artifacts/models/eval_run_metrics_v2.csv"))
    ap.add_argument("--qc-run-summary", type=Path, default=Path("artifacts/qc/qc_run_summary.csv"))
    ap.add_argument("--runs-dir", type=Path, default=Path("artifacts/runs"))
    ap.add_argument("--out-dir", type=Path, default=Path("artifacts/qc/phys_leak_bg"))
    ap.add_argument("--use-only-qc-keep", action="store_true")

    ap.add_argument("--tref-C", type=float, default=40.0)
    ap.add_argument("--ridge", type=float, default=1e-6)
    ap.add_argument("--gamma-min", type=float, default=0.0)
    ap.add_argument("--gamma-max", type=float, default=0.06)
    ap.add_argument("--gamma-steps", type=int, default=31)
    ap.add_argument(
        "--temp-source",
        choices=["start", "mean"],
        default="mean",
        help="Use start temperatures (t0) or run-mean temperatures for leakage term.",
    )

    ap.add_argument(
        "--terms",
        type=str,
        default="i2r,leak,base",
        help="Comma-separated terms to include: i2r, leak, base. Default includes all.",
    )

    ap.add_argument(
        "--clip-frac-of-meas",
        type=float,
        default=0.0,
        help=(
            "Optional safety clip: cap predicted additive correction to at most clip_frac * p_meas. "
            "Set 0 to disable. Useful to prevent over-correction when leakage/background is not identifiable."
        ),
    )

    args = ap.parse_args()

    eval_df = pd.read_csv(args.eval_run_metrics)
    qc = pd.read_csv(args.qc_run_summary)

    cols = [
        "run_name",
        "scenario",
        "qc_keep",
        "battery_level0_pct",
        "thermal_cpu0_C",
        "thermal_batt0_C",
        "perfetto_current_mean_uA",
        "perfetto_voltage_mean_V",
    ]
    cols = [c for c in cols if c in qc.columns]
    df = eval_df.merge(qc[cols], on=["run_name", "scenario"], how="left")

    if args.use_only_qc_keep and "qc_keep" in df.columns:
        df = df[pd.to_numeric(df["qc_keep"], errors="coerce").fillna(0).astype(int) == 1].copy()

    # Optionally add run-mean temperature columns
    if args.temp_source == "mean":
        df = _attach_temp_means(df, args.runs_dir)

    p_meas = _num(df["p_meas_mean_mW"])
    p_pred = _num(df["p_pred_mean_mW"])
    resid_mW = p_meas - p_pred

    # Basic covariates for physics terms
    i_uA = _num(df.get("perfetto_current_mean_uA", pd.Series([np.nan] * len(df))))
    i_A = np.abs(i_uA) / 1e6
    i2 = i_A * i_A

    v_V = _num(df.get("perfetto_voltage_mean_V", pd.Series([np.nan] * len(df))))
    soc = _num(df.get("battery_level0_pct", pd.Series([np.nan] * len(df)))) / 100.0

    if args.temp_source == "mean" and "thermal_cpu_mean_C" in df.columns:
        t_cpu = _num(df["thermal_cpu_mean_C"])
        t_batt = _num(df.get("thermal_batt_mean_C", pd.Series([np.nan] * len(df))))
    else:
        t_cpu = _num(df.get("thermal_cpu0_C", pd.Series([np.nan] * len(df))))
        t_batt = _num(df.get("thermal_batt0_C", pd.Series([np.nan] * len(df))))

    include = {t.strip().lower() for t in str(args.terms).split(",") if t.strip()}

    # Mask for fit/apply: require base fields and discharging
    m = np.isfinite(resid_mW) & np.isfinite(v_V) & np.isfinite(i2) & (i2 > 1e-8) & np.isfinite(soc) & np.isfinite(t_cpu)

    # We only allow additive loss terms (>=0), so fit to positive residual portion.
    yW = np.maximum(0.0, resid_mW / 1000.0)

    scenarios = sorted(df.loc[m, "scenario"].astype(str).unique())

    gammas = np.linspace(float(args.gamma_min), float(args.gamma_max), int(args.gamma_steps))

    # Build design matrix with a given gamma
    def design(mask: np.ndarray, gamma: float) -> np.ndarray:
        cols_X = []

        # i2r: R0 + R1*(1-SOC) + R2*max(0, Tcpu-Tref)
        if "i2r" in include:
            cols_X.append(i2[mask])
            cols_X.append(i2[mask] * (1.0 - soc[mask]))
            dt = np.maximum(0.0, t_cpu[mask] - float(args.tref_C))
            cols_X.append(i2[mask] * dt)

        # leak: P_leak = a * V * exp(gamma*(Tcpu - Tref))
        if "leak" in include:
            dt = (t_cpu[mask] - float(args.tref_C)).astype(float)
            cols_X.append(v_V[mask] * np.exp(float(gamma) * dt))

        # base background: constant W
        if "base" in include:
            cols_X.append(np.ones(int(np.sum(mask)), dtype=float))

        if not cols_X:
            return np.zeros((int(np.sum(mask)), 0), dtype=float)
        return np.column_stack(cols_X)

    # LOSO evaluation
    p_add_W = np.full(len(df), np.nan)
    fold_rows = []

    for scen in scenarios:
        test = m & (df["scenario"].astype(str) == scen).to_numpy()
        train = m & (df["scenario"].astype(str) != scen).to_numpy()

        best_beta = None
        best_gamma = 0.0
        best_rmse = float("inf")

        for gamma in gammas:
            Xtr = design(train, float(gamma))
            ytr = yW[train]
            if Xtr.shape[1] == 0:
                continue
            if len(ytr) < (Xtr.shape[1] + 1):
                continue

            beta = _ols_beta(Xtr, ytr, ridge=float(args.ridge))
            beta = np.maximum(beta, 0.0)

            # Training fit error (on positive residual target)
            r = (ytr - Xtr @ beta) * 1000.0
            rmse = float(np.sqrt(np.mean(r * r)))

            if rmse < best_rmse:
                best_rmse = rmse
                best_beta = beta
                best_gamma = float(gamma)

        if best_beta is None:
            p_add_W[test] = 0.0
            fold_rows.append({"scenario": scen, "gamma": np.nan, "p": 0, "n_train": int(np.sum(train)), "train_rmse_pos_mW": np.nan})
            continue

        Xt = design(test, float(best_gamma))
        yhat = Xt @ best_beta
        p_add_W[test] = np.maximum(0.0, yhat)

        row = {
            "scenario": scen,
            "gamma": float(best_gamma),
            "p": int(len(best_beta)),
            "n_train": int(np.sum(train)),
            "train_rmse_pos_mW": float(best_rmse),
        }
        for k, v in enumerate(best_beta):
            row[f"beta_{k}"] = float(v)
        fold_rows.append(row)

    # Optional safety clip (helps for poorly-identified terms like leak/base)
    clip = float(args.clip_frac_of_meas)
    if clip > 0:
        p_meas_W = p_meas / 1000.0
        cap = np.maximum(0.0, clip * p_meas_W)
        p_add_W = np.minimum(p_add_W, cap)

    p_pred_corr = p_pred + 1000.0 * p_add_W
    resid_corr = p_meas - p_pred_corr

    base = _metrics(resid_mW)
    corr = _metrics(resid_corr)

    # Correlations (should reduce if terms explain the effect)
    corr_table = []
    for cov_name, cov in [
        ("thermal_cpu", t_cpu),
        ("thermal_batt", t_batt),
        ("soc", soc * 100.0),
        ("voltage", v_V),
        ("current_uA", i_uA),
    ]:
        corr_table.append({"covariate": cov_name, "r_before": _pearson(cov, resid_mW), "r_after": _pearson(cov, resid_corr)})

    args.out_dir.mkdir(parents=True, exist_ok=True)

    out_df = df.copy()
    out_df["resid_mW"] = resid_mW
    out_df["p_add_W"] = p_add_W
    out_df["p_pred_mean_mW_corr_phys"] = p_pred_corr
    out_df["resid_corr_mW"] = resid_corr
    out_df.to_csv(args.out_dir / "run_level_phys_terms.csv", index=False, encoding="utf-8")

    pd.DataFrame([{"metric": "base", **base}, {"metric": "phys_corrected", **corr}]).to_csv(
        args.out_dir / "phys_terms_summary.csv", index=False, encoding="utf-8"
    )
    pd.DataFrame(corr_table).to_csv(args.out_dir / "phys_terms_residual_correlations.csv", index=False, encoding="utf-8")
    if fold_rows:
        pd.DataFrame(fold_rows).to_csv(args.out_dir / "phys_terms_fold_params.csv", index=False, encoding="utf-8")

    md = []
    md.append("# Physical Terms Test: i2r + leakage + base (LOSO)")
    md.append("")
    md.append(f"Source eval: `{args.eval_run_metrics.as_posix()}`")
    md.append(f"QC keep only: {bool(args.use_only_qc_keep)}")
    md.append(f"temp_source: {args.temp_source}")
    md.append(f"terms: {sorted(include)}")
    md.append(f"gamma grid: [{args.gamma_min}, {args.gamma_max}] steps={args.gamma_steps}")
    md.append("")
    md.append("## Error summary")
    md.append("")
    md.append("```text")
    md.append(pd.read_csv(args.out_dir / "phys_terms_summary.csv").to_csv(index=False))
    md.append("```")
    md.append("")
    md.append("## Residual correlations")
    md.append("")
    md.append("```text")
    md.append(pd.read_csv(args.out_dir / "phys_terms_residual_correlations.csv").to_csv(index=False))
    md.append("```")

    (args.out_dir / "phys_terms.md").write_text("\n".join(md), encoding="utf-8")

    print(f"Wrote: {args.out_dir / 'phys_terms.md'}")
    print(f"Wrote: {args.out_dir / 'run_level_phys_terms.csv'}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
