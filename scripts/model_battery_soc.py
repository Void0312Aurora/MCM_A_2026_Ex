from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import pandas as pd


@dataclass
class ModelParams:
    # Power model
    p_base_mW: float
    k_screen_mW_per_norm: float
    k_cpu: float
    k_gps_mW: float
    leak_baseline_mW: float
    leak_coeff_per_C: float
    temp_ref_C: float

    # Battery capacity (charge) model
    c_eff_mAh: float


def _ridge_fit(X: np.ndarray, y: np.ndarray, alpha: float) -> np.ndarray:
    # Solve (X^T X + alpha I) beta = X^T y
    n_features = X.shape[1]
    A = X.T @ X + alpha * np.eye(n_features)
    b = X.T @ y
    return np.linalg.solve(A, b)


def _col_as_numeric_series(df: pd.DataFrame, col: str, default: float = 0.0) -> pd.Series:
    s = df[col] if col in df.columns else pd.Series(default, index=df.index)
    return pd.to_numeric(s, errors="coerce")


def _build_design(df: pd.DataFrame) -> tuple[np.ndarray, np.ndarray, list[str], float]:
    # Target: measured total power from Perfetto aligned
    y = pd.to_numeric(df["power_total_mW"], errors="coerce").to_numpy(dtype=float)

    screen_p = _col_as_numeric_series(df, "power_screen_mW", default=0.0).fillna(0.0).to_numpy(dtype=float)
    cpu_p = _col_as_numeric_series(df, "power_cpu_mW", default=0.0).fillna(0.0).to_numpy(dtype=float)
    gps_on = _col_as_numeric_series(df, "is_gps_on", default=0.0).fillna(0.0).to_numpy(dtype=float)
    temp = _col_as_numeric_series(df, "temperature_cpu_C", default=np.nan).ffill().bfill()
    temp = temp.fillna(temp.median() if temp.notna().any() else 40.0).to_numpy(dtype=float)

    # Features are chosen to be mechanistic + interpretable and easy to justify in the report.
    # Power model:
    # P = p_base + k_screen * brightness_norm + k_cpu * cpu_power_proxy + k_gps * gps_on + leak_baseline * exp(leak_coeff*(T-temp_ref))
    # We keep the exponential term linearized by fitting leak_baseline and leak_coeff around temp_ref.
    # In practice we fit: leak = leak_baseline * exp(leak_coeff*(T-temp_ref))
    # This is nonlinear; we approximate with first-order: leak ≈ leak_baseline + leak_slope*(T-temp_ref).
    # Here: leak_slope will be fit as a linear coefficient.

    # We'll fit linear model in parameters:
    # y = b0 + b_screen*screen_p + b_cpu*cpu_p + b_gps*gps_on + b_temp*max(0, temp - temp_ref)
    # Using a one-sided temperature term prevents "cooler" runs from artificially reducing predicted power,
    # which otherwise can flip A/B effects (e.g., GPS ON vs OFF).
    temp_ref = float(np.nanmedian(temp))
    temp_excess = np.clip(temp - temp_ref, 0.0, None).astype(float)

    X = np.column_stack(
        [
            np.ones_like(y),
            screen_p,
            cpu_p,
            gps_on,
            temp_excess,
        ]
    ).astype(float)
    cols = ["intercept", "screen_power_mW", "cpu_power_mW", "gps_on", "temp_excess_C"]
    return X, y, cols, temp_ref


def fit_power_model(all_df: pd.DataFrame, alpha: float = 1000.0) -> tuple[ModelParams, pd.DataFrame]:
    df = all_df.copy()
    df = df[df["dt_s"].notna() & (df["dt_s"] > 0)]
    df = df[df["power_total_mW"].notna()]

    X, y, cols, temp_ref = _build_design(df)

    beta = _ridge_fit(X, y, alpha=alpha)

    params = ModelParams(
        p_base_mW=float(beta[0]),
        k_screen_mW_per_norm=float(beta[1]),
        k_cpu=float(beta[2]),
        k_gps_mW=float(beta[3]),
        leak_baseline_mW=0.0,
        leak_coeff_per_C=float(beta[4]),
        temp_ref_C=float(temp_ref),
        c_eff_mAh=4410.0,
    )

    # Attach predictions
    df_out = df.copy()
    df_out["power_pred_mW"] = (X @ beta).astype(float)
    df_out["power_err_mW"] = df_out["power_pred_mW"] - df_out["power_total_mW"]

    return params, df_out


def simulate_soc(df_run: pd.DataFrame, params: ModelParams) -> pd.DataFrame:
    df = df_run.copy().reset_index(drop=True)

    # Fill essentials
    df["dt_s"] = _col_as_numeric_series(df, "dt_s", default=0.0).fillna(0.0)
    df["t_s"] = pd.to_numeric(df["t_s"], errors="coerce")
    if df["t_s"].isna().all():
        df["t_s"] = df["dt_s"].cumsum() - df["dt_s"]

    v = pd.to_numeric(df["voltage_mV"], errors="coerce") / 1000.0
    v = v.ffill().bfill()
    v = v.fillna(v.median() if v.notna().any() else 3.85)

    screen_p = _col_as_numeric_series(df, "power_screen_mW", default=0.0).fillna(0.0)
    cpu_p = _col_as_numeric_series(df, "power_cpu_mW", default=0.0).fillna(0.0)
    gps_on = _col_as_numeric_series(df, "is_gps_on", default=0.0).fillna(0.0)
    temp = _col_as_numeric_series(df, "temperature_cpu_C", default=np.nan).ffill().bfill()
    temp = temp.fillna(temp.median() if temp.notna().any() else params.temp_ref_C)

    # Power prediction
    temp_excess = (temp - params.temp_ref_C).clip(lower=0.0)
    p_pred = (
        params.p_base_mW
        + params.k_screen_mW_per_norm * screen_p
        + params.k_cpu * cpu_p
        + params.k_gps_mW * gps_on
        + params.leak_coeff_per_C * temp_excess
    )

    # SOC integration (charge-based SOC)
    soc0 = float(pd.to_numeric(df["soc_pct"], errors="coerce").dropna().iloc[0]) / 100.0
    soc = [soc0]

    denom = 3600.0 * float(params.c_eff_mAh)
    dt_arr = df["dt_s"].to_numpy(dtype=float)
    v_arr = v.to_numpy(dtype=float)
    p_arr = p_pred.to_numpy(dtype=float)

    for i in range(len(df) - 1):
        dt = float(dt_arr[i])
        if dt <= 0:
            soc.append(soc[-1])
            continue

        p_mw = float(p_arr[i])
        vi = float(v_arr[i])
        if not np.isfinite(vi) or vi <= 0:
            vi = 3.85

        dsoc = (p_mw / (vi * denom)) * dt
        soc_next = soc[-1] - dsoc
        soc_next = min(1.0, max(0.0, soc_next))
        soc.append(soc_next)

    df["power_pred_mW"] = p_pred.astype(float)
    df["soc_sim"] = np.array(soc, dtype=float)
    df["soc_sim_pct"] = df["soc_sim"] * 100.0

    return df


def validate(all_df: pd.DataFrame, params: ModelParams, out_dir: Path) -> pd.DataFrame:
    out_dir.mkdir(parents=True, exist_ok=True)

    rows = []
    for run_name, g in all_df.groupby("run_name"):
        g = g.sort_values("t_s")
        sim = simulate_soc(g, params)

        soc_meas = pd.to_numeric(sim["soc_pct"], errors="coerce")
        soc_sim = pd.to_numeric(sim["soc_sim_pct"], errors="coerce")

        # Compare on available SOC points
        m = soc_meas.notna() & soc_sim.notna()
        if m.sum() == 0:
            continue

        err = (soc_sim[m] - soc_meas[m]).to_numpy(dtype=float)
        rmse = float(np.sqrt(np.mean(err**2)))
        mape = float(np.mean(np.abs(err) / np.clip(np.abs(soc_meas[m].to_numpy(dtype=float)), 1e-6, None)) * 100.0)

        duration_s = float(pd.to_numeric(sim["dt_s"], errors="coerce").fillna(0).sum())

        rows.append(
            {
                "run_name": run_name,
                "n_samples": int(len(sim)),
                "duration_s": duration_s,
                "soc_initial": float(soc_meas.dropna().iloc[0]) if soc_meas.notna().any() else np.nan,
                "soc_final_meas": float(soc_meas.dropna().iloc[-1]) if soc_meas.notna().any() else np.nan,
                "soc_final_sim": float(soc_sim.dropna().iloc[-1]) if soc_sim.notna().any() else np.nan,
                "rmse_soc_pct": rmse,
                "mape_soc_pct": mape,
                "p_meas_mean_mW": float(pd.to_numeric(sim["power_total_mW"], errors="coerce").dropna().mean())
                if "power_total_mW" in sim.columns
                else np.nan,
                "p_pred_mean_mW": float(pd.to_numeric(sim["power_pred_mW"], errors="coerce").dropna().mean()),
            }
        )

    val = pd.DataFrame(rows).sort_values("run_name")
    val.to_csv(out_dir / "model_validation.csv", index=False, encoding="utf-8")

    # Plot summary: measured vs predicted mean power per run
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    if not val.empty:
        fig, ax = plt.subplots(figsize=(10, 6))
        x = val["p_meas_mean_mW"].to_numpy(dtype=float)
        y = val["p_pred_mean_mW"].to_numpy(dtype=float)
        ax.scatter(x, y)
        lo = float(np.nanmin([x.min(), y.min()]))
        hi = float(np.nanmax([x.max(), y.max()]))
        ax.plot([lo, hi], [lo, hi], linestyle="--", color="gray")
        ax.set_xlabel("Measured mean power (mW)")
        ax.set_ylabel("Predicted mean power (mW)")
        ax.set_title("Power model validation (per-run mean)")
        ax.grid(True, alpha=0.3)
        fig.tight_layout()
        fig.savefig(out_dir / "model_validation.png", dpi=150)

    return val


def main() -> int:
    ap = argparse.ArgumentParser(description="Fit interpretable power model + simulate continuous-time SOC ODE")
    ap.add_argument(
        "--input",
        type=Path,
        default=Path("artifacts/models/all_runs_model_input.csv"),
        help="Model input CSV generated by scripts/model_preprocess.py",
    )
    ap.add_argument(
        "--out-dir",
        type=Path,
        default=Path("artifacts/models"),
        help="Output dir for params + validation",
    )
    ap.add_argument(
        "--alpha",
        type=float,
        default=1000.0,
        help="Ridge regularization strength for power fit",
    )
    ap.add_argument(
        "--c-eff-mAh",
        type=float,
        default=4410.0,
        help="Effective capacity (mAh) used in SOC ODE",
    )

    args = ap.parse_args()

    all_df = pd.read_csv(args.input)

    params, df_fit = fit_power_model(all_df, alpha=args.alpha)
    params.c_eff_mAh = float(args.c_eff_mAh)

    # Save params
    params_path = args.out_dir / "model_params_v1.json"
    params_path.write_text(json.dumps(asdict(params), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # Reattach prediction back to full dataset for validation
    all_df2 = all_df.copy()

    # Build predictions using fitted coefficients (rebuild X on rows with power_total_mW)
    df_pred = all_df2.copy()
    df_pred["power_total_mW"] = pd.to_numeric(df_pred["power_total_mW"], errors="coerce")

    # Validate / plots
    validate(df_pred, params, args.out_dir)

    print(f"Wrote: {params_path}")
    print(f"Wrote: {args.out_dir / 'model_validation.csv'}")
    print(f"Wrote: {args.out_dir / 'model_validation.png'}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
