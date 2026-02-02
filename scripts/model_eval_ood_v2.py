from __future__ import annotations

import argparse
import sys
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pandas as pd

# Allow importing sibling script modules when executed as a script.
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from model_battery_soc_v2_thermal1 import (  # noqa: E402
    ModelParamsV2,
    fit_power_model_v2,
    fit_thermal_1state,
    fit_thermal_2state,
    simulate_soc,
    simulate_temperature_1state,
    simulate_temperature_2state,
)


def _col_num(df: pd.DataFrame, col: str, default: float = 0.0) -> pd.Series:
    s = df[col] if col in df.columns else pd.Series(default, index=df.index)
    return pd.to_numeric(s, errors="coerce")


def predict_power_v2(
    df_all: pd.DataFrame,
    params: ModelParamsV2,
    *,
    thermal_model: str = "1state",
    leak_temp_mix_cpu: float = 0.7,
) -> pd.DataFrame:
    df = df_all.copy()
    df["power_screen_mW"] = _col_num(df, "power_screen_mW", default=0.0).fillna(0.0)
    df["power_cpu_mW"] = _col_num(df, "power_cpu_mW", default=0.0).fillna(0.0)
    df["is_gps_on"] = _col_num(df, "is_gps_on", default=0.0).fillna(0.0)
    df["cellular_on"] = _col_num(df, "cellular_on", default=1.0).fillna(1.0)

    thermal_model = str(thermal_model or "1state").strip().lower()
    if thermal_model not in {"1state", "2state"}:
        thermal_model = "1state"

    # Per-run thermal fit + simulate temperature (uses observed temps/cpu power; allowed at test time)
    out_rows: list[pd.DataFrame] = []
    for run_name, g in df.groupby("run_name"):
        g = g.sort_values("t_s")

        tmp = g.copy().reset_index(drop=True)
        if thermal_model == "2state":
            th2 = fit_thermal_2state(tmp)
            t_cpu_hat, t_batt_hat, t_leak_hat = simulate_temperature_2state(
                tmp,
                th2,
                leak_temp_mix_cpu=float(leak_temp_mix_cpu),
            )
            tmp["temp_cpu_hat_C"] = t_cpu_hat.to_numpy(dtype=float)
            tmp["temp_batt_hat_C"] = t_batt_hat.to_numpy(dtype=float)
            tmp["temp_leak_hat_C"] = t_leak_hat.to_numpy(dtype=float)
        else:
            th = fit_thermal_1state(tmp)
            t_hat = simulate_temperature_1state(tmp, th)
            tmp["temp_cpu_hat_C"] = t_hat.to_numpy(dtype=float)
            tmp["temp_batt_hat_C"] = np.nan
            tmp["temp_leak_hat_C"] = tmp["temp_cpu_hat_C"].to_numpy(dtype=float)

        leak_feat = np.exp(params.leak_gamma_per_C * (tmp["temp_leak_hat_C"].to_numpy(dtype=float) - params.leak_tref_C))
        p0 = (
            params.p_base_mW
            + params.k_screen * tmp["power_screen_mW"].to_numpy(dtype=float)
            + params.k_cpu * tmp["power_cpu_mW"].to_numpy(dtype=float)
            + params.k_leak_mW * leak_feat
        )

        p = (
            p0
            + params.k_gps_off_mW * (1.0 - tmp["is_gps_on"].to_numpy(dtype=float))
            + params.k_cellular_off_mW * (1.0 - tmp["cellular_on"].to_numpy(dtype=float))
        )

        tmp["power_pred_mW"] = p.astype(float)
        out_rows.append(tmp)

    return pd.concat(out_rows, ignore_index=True)


def run_level_metrics(df_pred: pd.DataFrame, c_eff_mAh: float) -> pd.DataFrame:
    rows = []
    for run_name, g in df_pred.groupby("run_name"):
        g = g.sort_values("t_s")

        p_meas = _col_num(g, "power_total_mW", default=np.nan)
        p_pred = _col_num(g, "power_pred_mW", default=np.nan)
        if p_meas.notna().any() and p_pred.notna().any():
            p_meas_mean = float(p_meas.dropna().mean())
            p_pred_mean = float(p_pred.dropna().mean())
        else:
            p_meas_mean = np.nan
            p_pred_mean = np.nan

        # SOC error
        sim = simulate_soc(g, c_eff_mAh=float(c_eff_mAh))
        soc_meas = _col_num(sim, "soc_pct", default=np.nan)
        soc_sim = _col_num(sim, "soc_sim_pct", default=np.nan)
        m = soc_meas.notna() & soc_sim.notna()
        rmse_soc = float(np.sqrt(np.mean((soc_sim[m] - soc_meas[m]).to_numpy(dtype=float) ** 2))) if m.sum() > 0 else np.nan

        rows.append(
            {
                "run_name": run_name,
                "scenario": str(g["scenario"].dropna().iloc[0]) if "scenario" in g.columns and g["scenario"].notna().any() else "",
                "n_samples": int(len(g)),
                "p_meas_mean_mW": p_meas_mean,
                "p_pred_mean_mW": p_pred_mean,
                "p_rel_err_pct": float((p_pred_mean - p_meas_mean) / p_meas_mean * 100.0)
                if np.isfinite(p_meas_mean) and p_meas_mean != 0
                else np.nan,
                "rmse_soc_pct": rmse_soc,
            }
        )

    return pd.DataFrame(rows)


def eval_split(
    all_df: pd.DataFrame,
    train_mask: np.ndarray,
    split_name: str,
    alpha: float,
    leak_doubling_C: float,
    c_eff_mAh: float,
    *,
    thermal_model: str,
    leak_temp_mix_cpu: float,
) -> tuple[dict, pd.DataFrame]:
    train_df = all_df.loc[train_mask].copy()
    test_df = all_df.loc[~train_mask].copy()

    # Fit params on train
    leak_gamma = float(np.log(2.0) / leak_doubling_C)
    params, _, _ = fit_power_model_v2(
        train_df,
        alpha=alpha,
        leak_gamma_per_C=leak_gamma,
        thermal_model=str(thermal_model),
        leak_temp_mix_cpu=float(leak_temp_mix_cpu),
    )

    # Predict on test
    pred = predict_power_v2(
        test_df,
        params,
        thermal_model=str(thermal_model),
        leak_temp_mix_cpu=float(leak_temp_mix_cpu),
    )

    # Per-sample MAE
    p_meas = _col_num(pred, "power_total_mW", default=np.nan)
    p_pred = _col_num(pred, "power_pred_mW", default=np.nan)
    m = p_meas.notna() & p_pred.notna()
    mae = float((p_pred[m] - p_meas[m]).abs().mean()) if m.any() else np.nan

    # Run-level metrics on heldout
    run_metrics = run_level_metrics(pred, c_eff_mAh=float(c_eff_mAh))

    summary = {
        "split": split_name,
        "n_train_samples": int(train_df.shape[0]),
        "n_test_samples": int(test_df.shape[0]),
        "n_test_runs": int(run_metrics["run_name"].nunique()),
        "power_sample_mae_mW": mae,
        **{f"param_{k}": v for k, v in asdict(params).items()},
    }
    run_metrics.insert(0, "split", split_name)
    return summary, run_metrics


def main() -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Generalization evaluation for v2 thermal(1-state) model. "
            "Default focuses on S2 brightness holdout (within required experiments), "
            "while broader leave-one-run/scenario splits are opt-in."
        )
    )
    ap.add_argument(
        "--input",
        type=Path,
        default=Path("artifacts/models/all_runs_model_input.csv"),
        help="Model input CSV",
    )
    ap.add_argument(
        "--out-dir",
        type=Path,
        default=Path("artifacts/models"),
        help="Output directory",
    )
    ap.add_argument("--alpha", type=float, default=2000.0)
    ap.add_argument("--leak-doubling-C", type=float, default=10.0)
    ap.add_argument("--c-eff-mAh", type=float, default=4410.0)
    ap.add_argument(
        "--thermal-model",
        choices=["1state", "2state"],
        default="1state",
        help="Thermal model used for both fitting and evaluation.",
    )
    ap.add_argument(
        "--leak-temp-mix-cpu",
        type=float,
        default=0.7,
        help="For 2state: leak_temp = mix*cpu_hat + (1-mix)*batt_hat (0..1).",
    )
    ap.add_argument(
        "--eval",
        choices=["s2-holdout", "looro", "loso", "all"],
        default="s2-holdout",
        help=(
            "Evaluation mode. 's2-holdout' leaves out each S2 brightness level in turn (recommended). "
            "'looro' leaves out one run at a time. 'loso' leaves out one scenario at a time. 'all' runs all splits."
        ),
    )

    args = ap.parse_args()

    df = pd.read_csv(args.input)

    # Only rows with measured power
    df["power_total_mW"] = _col_num(df, "power_total_mW", default=np.nan)
    df = df[df["power_total_mW"].notna()].copy()

    # Drop tiny runs
    run_counts = df.groupby("run_name").size()
    keep_runs = set(run_counts[run_counts >= 30].index.astype(str))
    df = df[df["run_name"].astype(str).isin(keep_runs)].copy()

    df["scenario"] = df["scenario"].astype(str)

    summaries = []
    all_run_metrics = []

    eval_mode = str(args.eval)

    # A) S2 brightness holdout (within required experiments): leave out each S2 brightness level.
    if eval_mode in {"s2-holdout", "all"}:
        s2_levels = sorted([s for s in df["scenario"].astype(str).unique() if s.startswith("S2")])
        for s2 in s2_levels:
            train_mask = df["scenario"].astype(str).ne(s2).to_numpy()
            summary, run_metrics = eval_split(
                df,
                train_mask=train_mask,
                split_name=f"S2_HOLDOUT:{s2}",
                alpha=float(args.alpha),
                leak_doubling_C=float(args.leak_doubling_C),
                c_eff_mAh=float(args.c_eff_mAh),
                thermal_model=str(args.thermal_model),
                leak_temp_mix_cpu=float(args.leak_temp_mix_cpu),
            )
            summaries.append(summary)
            all_run_metrics.append(run_metrics)

    # B) Leave-one-run-out (opt-in).
    if eval_mode in {"looro", "all"}:
        for run_name in sorted(df["run_name"].astype(str).unique()):
            train_mask = df["run_name"].astype(str).ne(run_name).to_numpy()
            summary, run_metrics = eval_split(
                df,
                train_mask=train_mask,
                split_name=f"LOORO:{run_name}",
                alpha=float(args.alpha),
                leak_doubling_C=float(args.leak_doubling_C),
                c_eff_mAh=float(args.c_eff_mAh),
                thermal_model=str(args.thermal_model),
                leak_temp_mix_cpu=float(args.leak_temp_mix_cpu),
            )
            summaries.append(summary)
            all_run_metrics.append(run_metrics)

    # C) Leave-one-scenario-out (opt-in).
    if eval_mode in {"loso", "all"}:
        for scenario in sorted(df["scenario"].astype(str).unique()):
            train_mask = df["scenario"].astype(str).ne(scenario).to_numpy()
            summary, run_metrics = eval_split(
                df,
                train_mask=train_mask,
                split_name=f"LOSO:{scenario}",
                alpha=float(args.alpha),
                leak_doubling_C=float(args.leak_doubling_C),
                c_eff_mAh=float(args.c_eff_mAh),
                thermal_model=str(args.thermal_model),
                leak_temp_mix_cpu=float(args.leak_temp_mix_cpu),
            )
            summaries.append(summary)
            all_run_metrics.append(run_metrics)

    out_dir = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    suffix = "" if str(args.thermal_model) == "1state" else f"_{str(args.thermal_model)}"
    out_summary = out_dir / f"eval_summary_v2{suffix}.csv"
    out_runs = out_dir / f"eval_run_metrics_v2{suffix}.csv"

    pd.DataFrame(summaries).to_csv(out_summary, index=False, encoding="utf-8")
    pd.concat(all_run_metrics, ignore_index=True).to_csv(out_runs, index=False, encoding="utf-8")

    print(f"Thermal model: {args.thermal_model}")
    print(f"Wrote: {out_summary}")
    print(f"Wrote: {out_runs}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
