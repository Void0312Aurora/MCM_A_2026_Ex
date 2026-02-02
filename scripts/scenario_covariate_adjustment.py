from __future__ import annotations

import argparse
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd


def _huber_weights(r: np.ndarray, c: float) -> np.ndarray:
    ar = np.abs(r)
    w = np.ones_like(ar)
    mask = ar > c
    w[mask] = c / ar[mask]
    return w


def fit_huber_irls(X: np.ndarray, y: np.ndarray, *, c: float = 1.5, iters: int = 30) -> np.ndarray:
    """Huber IRLS. X should include intercept if desired."""
    beta = np.linalg.lstsq(X, y, rcond=None)[0]
    for _ in range(iters):
        r = y - X @ beta
        mad = np.median(np.abs(r - np.median(r)))
        s = 1.4826 * mad if mad > 0 else (np.std(r) if np.std(r) > 0 else 1.0)
        u = r / s
        w = _huber_weights(u, c)
        W = np.sqrt(w)
        Xw = X * W[:, None]
        yw = y * W
        beta_new = np.linalg.lstsq(Xw, yw, rcond=None)[0]
        if np.max(np.abs(beta_new - beta)) < 1e-9:
            beta = beta_new
            break
        beta = beta_new
    return beta


def _as_num(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s, errors="coerce")


def _pick_ref(series: pd.Series, user_val: float) -> float:
    if np.isfinite(user_val):
        return float(user_val)
    v = _as_num(series).dropna().to_numpy(float)
    if len(v) == 0:
        return float("nan")
    return float(np.median(v))


def _scenario_filter(s: pd.Series, prefixes: list[str]) -> pd.Series:
    ss = s.astype(str)
    if not prefixes:
        return pd.Series([True] * len(ss), index=ss.index)
    m = pd.Series([False] * len(ss), index=ss.index)
    for p in prefixes:
        m |= ss.str.startswith(p)
    return m


def build_design_matrix(
    df: pd.DataFrame,
    *,
    scenario_col: str,
    covariates: list[str],
    drop_first: bool,
) -> tuple[np.ndarray, list[str]]:
    """Return X and column names.

    X = [intercept] + scenario dummies (+ optional drop_first) + covariates.
    """
    scen = df[scenario_col].astype(str)
    dummies = pd.get_dummies(scen, prefix="scen", drop_first=drop_first)

    Xdf = pd.DataFrame({"intercept": 1.0}, index=df.index)
    Xdf = pd.concat([Xdf, dummies], axis=1)

    for c in covariates:
        if c not in df.columns:
            continue
        Xdf[c] = _as_num(df[c])

    return Xdf.to_numpy(float), list(Xdf.columns)


def scenario_raw_stats(df: pd.DataFrame, scenario_col: str, y_col: str) -> pd.DataFrame:
    g = df.dropna(subset=[scenario_col, y_col]).groupby(df[scenario_col].astype(str))
    out = g[y_col].agg(["count", "mean", "std", "min", "max"]).reset_index()
    out = out.rename(
        columns={
            scenario_col: "scenario",
            "count": "n",
            "mean": "raw_mean",
            "std": "raw_std",
            "min": "raw_min",
            "max": "raw_max",
        }
    )
    out["raw_cv"] = out["raw_std"] / out["raw_mean"]
    out["raw_ratio_max_min"] = out["raw_max"] / out["raw_min"]
    return out


def adjusted_means(
    df: pd.DataFrame,
    *,
    scenario_col: str,
    y_col: str,
    covariates: list[str],
    ref: dict[str, float],
    huber_c: float,
    huber_iters: int,
    drop_first: bool,
) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """Fit ANCOVA-like model and compute adjusted mean per scenario at ref covariates."""

    # Build full matrix, then filter complete rows
    X, colnames = build_design_matrix(df, scenario_col=scenario_col, covariates=covariates, drop_first=drop_first)
    y = _as_num(df[y_col]).to_numpy(float)

    m = np.isfinite(y)
    for j in range(X.shape[1]):
        m &= np.isfinite(X[:, j])

    Xf = X[m]
    yf = y[m]
    dff = df.loc[m].copy()

    # Need at least p+1 rows
    if len(dff) < (Xf.shape[1] + 1):
        raise RuntimeError(f"Not enough rows to fit: n={len(dff)} p={Xf.shape[1]}")

    beta = fit_huber_irls(Xf, yf, c=float(huber_c), iters=int(huber_iters))

    # Prepare reference covariate vector; scenario dummies depend on scenario
    scen_values = sorted(dff[scenario_col].astype(str).unique())

    # Identify which columns correspond to scenario dummies
    scen_cols = [c for c in colnames if c.startswith("scen_")]

    # Determine baseline scenario when drop_first=True: the missing dummy category
    baseline = None
    if drop_first:
        # pandas get_dummies drop_first drops the first category in sorted order by default
        baseline = scen_values[0] if scen_values else None

    rows = []
    for scen in scen_values:
        x = np.zeros(len(colnames), dtype=float)
        x[colnames.index("intercept")] = 1.0

        # scenario dummy columns
        if scen_cols:
            if drop_first and scen == baseline:
                pass
            else:
                col = f"scen_{scen}"
                if col in colnames:
                    x[colnames.index(col)] = 1.0

        # covariates
        for c in covariates:
            if c in colnames and c in ref and np.isfinite(ref[c]):
                x[colnames.index(c)] = float(ref[c])

        rows.append({"scenario": scen, "adjusted_mean": float(x @ beta)})

    adj = pd.DataFrame(rows)

    coef = pd.DataFrame({"term": colnames, "coef": beta})

    # Per-row predictions and residuals (for post-fit spread)
    y_hat = X @ beta
    resid = y - y_hat
    df_pred = df.copy()
    df_pred["y"] = y
    df_pred["y_hat"] = y_hat
    df_pred["resid"] = resid

    meta = {"baseline_scenario": baseline, "n_fit": int(len(dff)), "p": int(len(colnames))}
    return adj, coef, meta


def _try_emit_plots(out_dir: Path, merged: pd.DataFrame) -> None:
    try:
        import matplotlib.pyplot as plt
    except Exception:
        return

    dfp = merged.dropna(subset=["raw_mean", "adjusted_mean"]).copy()
    if dfp.empty:
        return

    dfp = dfp.sort_values("raw_mean")

    fig, ax = plt.subplots(figsize=(10, max(4.0, 0.25 * len(dfp))), dpi=160)
    y = np.arange(len(dfp))
    ax.scatter(dfp["raw_mean"], y, s=20, label="raw mean")
    ax.scatter(dfp["adjusted_mean"], y, s=20, label="adjusted mean")
    for i, (_, r) in enumerate(dfp.iterrows()):
        ax.plot([r["raw_mean"], r["adjusted_mean"]], [i, i], color="gray", alpha=0.25, linewidth=1.0)

    ax.set_yticks(y)
    ax.set_yticklabels(dfp["scenario"].astype(str).tolist())
    ax.set_xlabel("power mean (mW)")
    ax.set_title("Scenario means: raw vs covariate-adjusted")
    ax.grid(True, axis="x", alpha=0.25)
    ax.legend(loc="best")
    fig.tight_layout()
    fig.savefig(out_dir / "scenario_raw_vs_adjusted.png")
    plt.close(fig)

    # delta plot
    dfp["delta"] = dfp["raw_mean"] - dfp["adjusted_mean"]
    dfp2 = dfp.sort_values("delta")
    fig, ax = plt.subplots(figsize=(10, max(4.0, 0.25 * len(dfp2))), dpi=160)
    y = np.arange(len(dfp2))
    ax.barh(y, dfp2["delta"], color="#4C78A8")
    ax.set_yticks(y)
    ax.set_yticklabels(dfp2["scenario"].astype(str).tolist())
    ax.set_xlabel("raw - adjusted (mW)")
    ax.set_title("How much start-state shifts each scenario mean")
    ax.grid(True, axis="x", alpha=0.25)
    fig.tight_layout()
    fig.savefig(out_dir / "scenario_adjustment_delta.png")
    plt.close(fig)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Covariate-adjust scenario mean power using an ANCOVA-like regression: "
            "power_mean ~ scenario + (soc, voltage, temperatures). Produces tables and plots for triage." 
        )
    )
    ap.add_argument("--qc-run-summary", type=Path, default=Path("artifacts/qc/qc_run_summary.csv"))
    ap.add_argument("--out-dir", type=Path, default=Path("artifacts/qc/cov_adj"))

    ap.add_argument("--use-only-qc-keep", action="store_true")
    ap.add_argument("--require-perfetto", action="store_true")

    ap.add_argument("--scenario-prefix", action="append", default=[], help="Only include scenarios starting with this prefix; can repeat")

    ap.add_argument("--y-col", type=str, default="perfetto_power_mean_mW")
    ap.add_argument("--scenario-col", type=str, default="scenario")

    ap.add_argument(
        "--covariate",
        action="append",
        default=[],
        help="Covariate column in qc_run_summary.csv; can repeat (default includes SOC/voltage/thermal cpu/batt when present)",
    )

    ap.add_argument("--ref-soc", type=float, default=np.nan)
    ap.add_argument("--ref-voltage-V", type=float, default=np.nan)
    ap.add_argument("--ref-thermal-cpu", type=float, default=np.nan)
    ap.add_argument("--ref-thermal-batt", type=float, default=np.nan)

    ap.add_argument("--drop-first", action="store_true", help="Drop the first scenario dummy to avoid collinearity")
    ap.add_argument("--huber-c", type=float, default=1.5)
    ap.add_argument("--huber-iters", type=int, default=30)
    ap.add_argument("--emit-plots", action="store_true")

    args = ap.parse_args()

    df = pd.read_csv(args.qc_run_summary)
    if df.empty:
        print("Empty qc_run_summary.")
        return 1

    if args.use_only_qc_keep and "qc_keep" in df.columns:
        df = df[pd.to_numeric(df["qc_keep"], errors="coerce").fillna(0).astype(int) == 1].copy()

    if args.require_perfetto and "has_perfetto" in df.columns:
        df = df[pd.to_numeric(df["has_perfetto"], errors="coerce").fillna(0).astype(int) == 1].copy()

    df = df[_scenario_filter(df[args.scenario_col], list(args.scenario_prefix))].copy()

    # Default covariates
    cov = list(args.covariate)
    if not cov:
        for c in ["battery_level0_pct", "battery_voltage0_mV", "thermal_cpu0_C", "thermal_batt0_C"]:
            if c in df.columns:
                cov.append(c)

    # Convert voltage mV -> V as separate covariate for stability
    df = df.copy()
    if "battery_voltage0_mV" in cov:
        df["voltage_V"] = _as_num(df["battery_voltage0_mV"]) / 1000.0
        cov = ["voltage_V" if c == "battery_voltage0_mV" else c for c in cov]

    # Build reference state
    ref: dict[str, float] = {}
    if "battery_level0_pct" in df.columns and "battery_level0_pct" in cov:
        ref["battery_level0_pct"] = _pick_ref(df["battery_level0_pct"], args.ref_soc)
    if "voltage_V" in df.columns and "voltage_V" in cov:
        ref["voltage_V"] = _pick_ref(df["voltage_V"], args.ref_voltage_V)
    if "thermal_cpu0_C" in df.columns and "thermal_cpu0_C" in cov:
        ref["thermal_cpu0_C"] = _pick_ref(df["thermal_cpu0_C"], args.ref_thermal_cpu)
    if "thermal_batt0_C" in df.columns and "thermal_batt0_C" in cov:
        ref["thermal_batt0_C"] = _pick_ref(df["thermal_batt0_C"], args.ref_thermal_batt)

    # Raw stats
    raw = scenario_raw_stats(df, args.scenario_col, args.y_col)

    # Adjusted means
    try:
        adj, coef, meta = adjusted_means(
            df,
            scenario_col=args.scenario_col,
            y_col=args.y_col,
            covariates=cov,
            ref=ref,
            huber_c=float(args.huber_c),
            huber_iters=int(args.huber_iters),
            drop_first=bool(args.drop_first),
        )
    except RuntimeError as e:
        print(str(e))
        print("Try --drop-first or reduce covariates/prefix filtering.")
        return 2

    merged = raw.merge(adj, on="scenario", how="left")
    merged["adjustment_delta"] = merged["raw_mean"] - merged["adjusted_mean"]

    # Residual spread per scenario from fitted model (optional; uses meta only indirectly)
    # For quick triage, we approximate by comparing raw spread; residuals are more work and less stable here.

    args.out_dir.mkdir(parents=True, exist_ok=True)
    merged.to_csv(args.out_dir / "scenario_covariate_adjusted.csv", index=False, encoding="utf-8")
    coef.to_csv(args.out_dir / "scenario_covariate_adjusted_coeffs.csv", index=False, encoding="utf-8")

    md = []
    md.append("# Scenario Covariate Adjustment Report")
    md.append("")
    md.append(f"Source: `{args.qc_run_summary.as_posix()}`")
    md.append(f"Rows: {len(df)}; scenarios: {df[args.scenario_col].astype(str).nunique()}")
    md.append(f"Fit: n_fit={meta['n_fit']} p={meta['p']} drop_first={bool(args.drop_first)}")
    if meta.get("baseline_scenario"):
        md.append(f"Baseline scenario (dropped dummy): `{meta['baseline_scenario']}`")
    md.append("")
    md.append("## Covariates")
    md.append("")
    for c in cov:
        md.append(f"- {c}")
    md.append("")
    md.append("## Reference state")
    md.append("")
    if ref:
        for k, v in ref.items():
            md.append(f"- {k}: {v:.4f}" if k == "voltage_V" else f"- {k}: {v:.2f}")
    else:
        md.append("- (none)")
    md.append("")
    md.append("## Biggest adjustments (|raw - adjusted|)")
    md.append("")
    top = merged.dropna(subset=["adjustment_delta"]).copy()
    top["abs_delta"] = np.abs(top["adjustment_delta"].to_numpy(float))
    top = top.sort_values("abs_delta", ascending=False).head(15)
    cols = ["scenario", "n", "raw_mean", "adjusted_mean", "adjustment_delta", "raw_ratio_max_min"]
    cols = [c for c in cols if c in top.columns]
    md.append("```text")
    md.append(top[cols].to_csv(index=False))
    md.append("```")

    (args.out_dir / "scenario_covariate_adjusted.md").write_text("\n".join(md), encoding="utf-8")

    if args.emit_plots:
        _try_emit_plots(args.out_dir, merged)

    print(f"Wrote: {args.out_dir / 'scenario_covariate_adjusted.csv'}")
    print(f"Wrote: {args.out_dir / 'scenario_covariate_adjusted_coeffs.csv'}")
    print(f"Wrote: {args.out_dir / 'scenario_covariate_adjusted.md'}")
    if args.emit_plots:
        print(f"Wrote: {args.out_dir / 'scenario_raw_vs_adjusted.png'}")
        print(f"Wrote: {args.out_dir / 'scenario_adjustment_delta.png'}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
