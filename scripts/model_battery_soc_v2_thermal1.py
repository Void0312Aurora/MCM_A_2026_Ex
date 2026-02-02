from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import pandas as pd


@dataclass
class ThermalParams:
    # dT/dt = a*(T - T_amb) + b*P_heat_W
    # where a < 0, tau = -1/a
    a_per_s: float
    b_C_per_J: float
    t_amb_C: float


@dataclass
class ThermalParams2:
    # 2-state model using CPU and battery temperatures as observed signals.
    #
    # dT_cpu/dt  = a_cpu*(T_cpu - T_batt) + b_cpu*P_heat_W
    # dT_batt/dt = a_batt*(T_batt - T_amb) + b_couple*(T_cpu - T_batt)
    #
    # Constraints: a_cpu<=0, b_cpu>=0, a_batt<=0, b_couple>=0
    a_cpu_per_s: float
    b_cpu_C_per_J: float
    a_batt_per_s: float
    b_couple_per_s: float
    t_amb_C: float


@dataclass
class ModelParamsV2:
    # Electrical power model
    p_base_mW: float
    k_screen: float
    k_cpu: float
    k_leak_mW: float
    leak_gamma_per_C: float
    leak_tref_C: float

    # GPS offset applied when GPS is OFF (estimated from A/B residual; expected <= 0)
    k_gps_off_mW: float

    # Cellular offset (estimated from A/B residual)
    k_cellular_off_mW: float

    # Battery capacity
    c_eff_mAh: float


def _col_num(df: pd.DataFrame, col: str, default: float = 0.0) -> pd.Series:
    s = df[col] if col in df.columns else pd.Series(default, index=df.index)
    return pd.to_numeric(s, errors="coerce")


def _ridge_fit(X: np.ndarray, y: np.ndarray, alpha: float) -> np.ndarray:
    n = X.shape[1]
    A = X.T @ X + alpha * np.eye(n)
    b = X.T @ y
    return np.linalg.solve(A, b)


def fit_thermal_1state(df_run: pd.DataFrame) -> ThermalParams:
    """Fit a simple 1st-order thermal model using CPU temperature as observation.

    Model: dT/dt = a*(T - T_amb) + b*P_heat_W
    We set T_amb as the minimum observed T in-run (robust ambient proxy).
    """

    df = df_run.sort_values("t_s").copy().reset_index(drop=True)
    dt = _col_num(df, "dt_s", default=0.0).fillna(0.0).to_numpy(dtype=float)

    t_meas = _col_num(df, "temperature_cpu_C", default=np.nan).ffill().bfill()
    if t_meas.isna().all():
        # Fallback: a very weak cooling, no heating response
        return ThermalParams(a_per_s=-1.0 / 2000.0, b_C_per_J=0.0, t_amb_C=40.0)

    t = t_meas.fillna(float(t_meas.median())).to_numpy(dtype=float)

    # Heating proxy: CPU power estimate (mW) -> W
    p_cpu_mW = _col_num(df, "power_cpu_mW", default=0.0).fillna(0.0).to_numpy(dtype=float)
    p_heat_W = np.clip(p_cpu_mW, 0.0, None) / 1000.0

    t_amb = float(np.nanmin(t))
    if not np.isfinite(t_amb):
        t_amb = float(np.nanmedian(t)) if np.isfinite(np.nanmedian(t)) else 40.0

    # Build regression for dT/dt using finite differences
    # z_i = (T_{i+1}-T_i)/dt_i
    # x1_i = (T_i - T_amb)
    # x2_i = P_heat_W_i
    z = []
    x1 = []
    x2 = []

    for i in range(len(t) - 1):
        dti = float(dt[i])
        if not np.isfinite(dti) or dti <= 0:
            continue
        dT = float(t[i + 1] - t[i])
        z.append(dT / dti)
        x1.append(float(t[i] - t_amb))
        x2.append(float(p_heat_W[i]))

    if len(z) < 10:
        return ThermalParams(a_per_s=-1.0 / 2000.0, b_C_per_J=0.0, t_amb_C=t_amb)

    Z = np.asarray(z, dtype=float)
    X = np.column_stack([np.asarray(x1, dtype=float), np.asarray(x2, dtype=float)])

    # Ridge to stabilize (since x1 and x2 can be correlated)
    alpha = 1e-3
    beta = _ridge_fit(X, Z, alpha=alpha)

    a = float(beta[0])
    b = float(beta[1])

    # Enforce physically plausible signs: a <= 0, b >= 0
    if not np.isfinite(a) or a >= -1e-6:
        a = -1.0 / 2000.0
    if not np.isfinite(b) or b < 0:
        b = 0.0

    return ThermalParams(a_per_s=a, b_C_per_J=b, t_amb_C=t_amb)


def simulate_temperature_1state(df_run: pd.DataFrame, th: ThermalParams) -> pd.Series:
    df = df_run.sort_values("t_s").copy().reset_index(drop=True)
    dt = _col_num(df, "dt_s", default=0.0).fillna(0.0).to_numpy(dtype=float)

    t_meas = _col_num(df, "temperature_cpu_C", default=np.nan).ffill().bfill()
    t0 = float(t_meas.dropna().iloc[0]) if t_meas.notna().any() else th.t_amb_C

    p_cpu_mW = _col_num(df, "power_cpu_mW", default=0.0).fillna(0.0).to_numpy(dtype=float)
    p_heat_W = np.clip(p_cpu_mW, 0.0, None) / 1000.0

    t_hat = [t0]
    for i in range(len(df) - 1):
        dti = float(dt[i])
        if not np.isfinite(dti) or dti <= 0:
            t_hat.append(t_hat[-1])
            continue

        Ti = float(t_hat[-1])
        dTdt = th.a_per_s * (Ti - th.t_amb_C) + th.b_C_per_J * float(p_heat_W[i])
        t_next = Ti + dTdt * dti
        t_hat.append(float(t_next))

    return pd.Series(t_hat, index=df.index, dtype=float)


def fit_thermal_2state(df_run: pd.DataFrame) -> ThermalParams2:
    """Fit a simple 2-state thermal model using CPU + battery temperature observations.

    This targets a fast component (CPU) plus a slow component (battery/body), which
    is often needed to explain within-run drift as the device warms.
    """

    df = df_run.sort_values("t_s").copy().reset_index(drop=True)
    dt = _col_num(df, "dt_s", default=0.0).fillna(0.0).to_numpy(dtype=float)

    t_cpu_s = _col_num(df, "temperature_cpu_C", default=np.nan).ffill().bfill()
    t_batt_s = _col_num(df, "temperature_C", default=np.nan).ffill().bfill()

    if t_cpu_s.isna().all() or t_batt_s.isna().all():
        t_amb = float(np.nanmedian(t_cpu_s.to_numpy(dtype=float))) if t_cpu_s.notna().any() else 40.0
        return ThermalParams2(
            a_cpu_per_s=-1.0 / 200.0,
            b_cpu_C_per_J=0.0,
            a_batt_per_s=-1.0 / 2000.0,
            b_couple_per_s=0.0,
            t_amb_C=t_amb,
        )

    t_cpu = t_cpu_s.fillna(float(t_cpu_s.median())).to_numpy(dtype=float)
    t_batt = t_batt_s.fillna(float(t_batt_s.median())).to_numpy(dtype=float)

    p_cpu_mW = _col_num(df, "power_cpu_mW", default=0.0).fillna(0.0).to_numpy(dtype=float)
    p_heat_W = np.clip(p_cpu_mW, 0.0, None) / 1000.0

    t_amb = float(np.nanmin(t_batt))
    if not np.isfinite(t_amb):
        t_amb = float(np.nanmedian(t_batt)) if np.isfinite(np.nanmedian(t_batt)) else 40.0

    # Regression 1: dT_cpu/dt = a_cpu*(T_cpu - T_batt) + b_cpu*P
    z1: list[float] = []
    x1: list[float] = []
    x2: list[float] = []

    # Regression 2: dT_batt/dt = a_batt*(T_batt - T_amb) + b_couple*(T_cpu - T_batt)
    z2: list[float] = []
    x3: list[float] = []
    x4: list[float] = []

    for i in range(len(t_cpu) - 1):
        dti = float(dt[i])
        if not np.isfinite(dti) or dti <= 0:
            continue

        z1.append(float((t_cpu[i + 1] - t_cpu[i]) / dti))
        x1.append(float(t_cpu[i] - t_batt[i]))
        x2.append(float(p_heat_W[i]))

        z2.append(float((t_batt[i + 1] - t_batt[i]) / dti))
        x3.append(float(t_batt[i] - t_amb))
        x4.append(float(t_cpu[i] - t_batt[i]))

    if len(z1) < 10 or len(z2) < 10:
        return ThermalParams2(
            a_cpu_per_s=-1.0 / 200.0,
            b_cpu_C_per_J=0.0,
            a_batt_per_s=-1.0 / 2000.0,
            b_couple_per_s=0.0,
            t_amb_C=t_amb,
        )

    alpha = 1e-3
    beta1 = _ridge_fit(
        np.column_stack([np.asarray(x1, dtype=float), np.asarray(x2, dtype=float)]),
        np.asarray(z1, dtype=float),
        alpha=alpha,
    )
    beta2 = _ridge_fit(
        np.column_stack([np.asarray(x3, dtype=float), np.asarray(x4, dtype=float)]),
        np.asarray(z2, dtype=float),
        alpha=alpha,
    )

    a_cpu = float(beta1[0])
    b_cpu = float(beta1[1])
    a_batt = float(beta2[0])
    b_couple = float(beta2[1])

    # Physically we only need a_cpu <= 0; values close to 0 are plausible (very weak coupling).
    # Do NOT snap small negative values to a strong default, or hot-start runs will unrealistically
    # cool to the battery temperature.
    if not np.isfinite(a_cpu) or a_cpu > 0:
        a_cpu = -1.0 / 2000.0
    if not np.isfinite(b_cpu) or b_cpu < 0:
        b_cpu = 0.0
    if not np.isfinite(a_batt) or a_batt > 0:
        a_batt = -1.0 / 5000.0
    if not np.isfinite(b_couple) or b_couple < 0:
        b_couple = 0.0

    return ThermalParams2(
        a_cpu_per_s=a_cpu,
        b_cpu_C_per_J=b_cpu,
        a_batt_per_s=a_batt,
        b_couple_per_s=b_couple,
        t_amb_C=t_amb,
    )


def simulate_temperature_2state(
    df_run: pd.DataFrame,
    th: ThermalParams2,
    *,
    leak_temp_mix_cpu: float = 0.7,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Simulate 2-state temperatures.

    Returns (T_cpu_hat, T_batt_hat, T_leak_hat) where T_leak_hat is a convex mix
    used by the leakage feature.
    """

    df = df_run.sort_values("t_s").copy().reset_index(drop=True)
    dt = _col_num(df, "dt_s", default=0.0).fillna(0.0).to_numpy(dtype=float)

    t_cpu_meas = _col_num(df, "temperature_cpu_C", default=np.nan).ffill().bfill()
    t_batt_meas = _col_num(df, "temperature_C", default=np.nan).ffill().bfill()

    t_cpu0 = float(t_cpu_meas.dropna().iloc[0]) if t_cpu_meas.notna().any() else th.t_amb_C
    t_batt0 = float(t_batt_meas.dropna().iloc[0]) if t_batt_meas.notna().any() else th.t_amb_C

    p_cpu_mW = _col_num(df, "power_cpu_mW", default=0.0).fillna(0.0).to_numpy(dtype=float)
    p_heat_W = np.clip(p_cpu_mW, 0.0, None) / 1000.0

    w = float(leak_temp_mix_cpu)
    if not np.isfinite(w):
        w = 0.7
    w = float(np.clip(w, 0.0, 1.0))

    t_cpu_hat = [t_cpu0]
    t_batt_hat = [t_batt0]
    for i in range(len(df) - 1):
        dti = float(dt[i])
        if not np.isfinite(dti) or dti <= 0:
            t_cpu_hat.append(t_cpu_hat[-1])
            t_batt_hat.append(t_batt_hat[-1])
            continue

        Tcpu = float(t_cpu_hat[-1])
        Tb = float(t_batt_hat[-1])
        dTcpu_dt = th.a_cpu_per_s * (Tcpu - Tb) + th.b_cpu_C_per_J * float(p_heat_W[i])
        dTb_dt = th.a_batt_per_s * (Tb - th.t_amb_C) + th.b_couple_per_s * (Tcpu - Tb)

        t_cpu_hat.append(float(Tcpu + dTcpu_dt * dti))
        t_batt_hat.append(float(Tb + dTb_dt * dti))

    s_cpu = pd.Series(t_cpu_hat, index=df.index, dtype=float)
    s_batt = pd.Series(t_batt_hat, index=df.index, dtype=float)
    s_leak = (w * s_cpu + (1.0 - w) * s_batt).astype(float)
    return s_cpu, s_batt, s_leak


def fit_power_model_v2(
    all_df: pd.DataFrame,
    alpha: float,
    leak_gamma_per_C: float,
    *,
    thermal_model: str = "1state",
    leak_temp_mix_cpu: float = 0.7,
) -> tuple[ModelParamsV2, pd.DataFrame, pd.DataFrame]:
    """Two-stage fit:
    1) Fit thermal per-run, simulate T_hat
    2) Fit electrical power model on GPS-OFF rows (intercept + screen + cpu + leak(T_hat))
    3) Estimate k_gps from S4 vs S4-1 residual means (nonnegative)
    """

    df = all_df.copy()
    df["dt_s"] = _col_num(df, "dt_s", default=0.0).fillna(0.0)
    df = df[df["dt_s"] > 0]

    df["power_total_mW"] = _col_num(df, "power_total_mW", default=np.nan)
    df = df[df["power_total_mW"].notna()]

    df["power_screen_mW"] = _col_num(df, "power_screen_mW", default=0.0).fillna(0.0)
    df["power_cpu_mW"] = _col_num(df, "power_cpu_mW", default=0.0).fillna(0.0)
    df["is_gps_on"] = _col_num(df, "is_gps_on", default=0.0).fillna(0.0)
    df["cellular_on"] = _col_num(df, "cellular_on", default=1.0).fillna(1.0)

    thermal_model = str(thermal_model or "1state").strip().lower()
    if thermal_model not in {"1state", "2state"}:
        thermal_model = "1state"

    # Per-run thermal fit + simulation
    thermal_rows = []
    t_hat_all = []
    for run_name, g in df.groupby("run_name"):
        tmp = g.sort_values("t_s").copy().reset_index(drop=True)

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

            tau_cpu = float(-1.0 / th2.a_cpu_per_s) if th2.a_cpu_per_s < 0 else float("inf")
            tau_batt = float(-1.0 / th2.a_batt_per_s) if th2.a_batt_per_s < 0 else float("inf")
            thermal_rows.append(
                {
                    "run_name": run_name,
                    "thermal_model": "2state",
                    "t_amb_C": th2.t_amb_C,
                    "a_cpu_per_s": th2.a_cpu_per_s,
                    "b_cpu_C_per_J": th2.b_cpu_C_per_J,
                    "a_batt_per_s": th2.a_batt_per_s,
                    "b_couple_per_s": th2.b_couple_per_s,
                    "tau_cpu_s": tau_cpu,
                    "tau_batt_s": tau_batt,
                    "leak_temp_mix_cpu": float(leak_temp_mix_cpu),
                }
            )
        else:
            th = fit_thermal_1state(tmp)
            t_hat = simulate_temperature_1state(tmp, th)
            tmp["temp_cpu_hat_C"] = t_hat.to_numpy(dtype=float)
            tmp["temp_batt_hat_C"] = np.nan
            tmp["temp_leak_hat_C"] = tmp["temp_cpu_hat_C"].to_numpy(dtype=float)

            tau = float(-1.0 / th.a_per_s) if th.a_per_s < 0 else float("inf")
            thermal_rows.append(
                {
                    "run_name": run_name,
                    "thermal_model": "1state",
                    "t_amb_C": th.t_amb_C,
                    "a_cpu_per_s": th.a_per_s,
                    "b_cpu_C_per_J": th.b_C_per_J,
                    "a_batt_per_s": np.nan,
                    "b_couple_per_s": np.nan,
                    "tau_cpu_s": tau,
                    "tau_batt_s": np.nan,
                    "leak_temp_mix_cpu": np.nan,
                }
            )

        t_hat_all.append(tmp)

    df2 = pd.concat(t_hat_all, ignore_index=True)
    thermal_df = pd.DataFrame(thermal_rows).sort_values("run_name")

    # Leak feature from simulated temp (structured: leak doubles ~ every 10C by default)
    t_ref = float(np.nanmedian(df2["temp_leak_hat_C"].to_numpy(dtype=float)))
    leak_feat = np.exp(leak_gamma_per_C * (df2["temp_leak_hat_C"].to_numpy(dtype=float) - t_ref))

    # Fit base on the dominant operating point (GPS ON + cellular ON).
    # Then apply calibrated offsets only when a subsystem is turned OFF.
    m_gps_on = df2["is_gps_on"].to_numpy(dtype=float) >= 0.5
    m_cell_on = df2["cellular_on"].to_numpy(dtype=float) >= 0.5
    m_base = m_gps_on & m_cell_on

    y = df2.loc[m_base, "power_total_mW"].to_numpy(dtype=float)
    X = np.column_stack(
        [
            np.ones_like(y),
            df2.loc[m_base, "power_screen_mW"].to_numpy(dtype=float),
            df2.loc[m_base, "power_cpu_mW"].to_numpy(dtype=float),
            leak_feat[m_base],
        ]
    ).astype(float)

    beta = _ridge_fit(X, y, alpha=alpha)

    # Base prediction (without GPS)
    p_base = float(beta[0])
    k_screen = float(beta[1])
    k_cpu = float(beta[2])
    k_leak = float(beta[3])

    p0 = (
        p_base
        + k_screen * df2["power_screen_mW"].to_numpy(dtype=float)
        + k_cpu * df2["power_cpu_mW"].to_numpy(dtype=float)
        + k_leak * leak_feat
    )

    # GPS offset from A/B residual mean: prefer S4 (GPS OFF) and S4-1 (GPS ON) if present.
    df2["p0_pred_mW"] = p0
    df2["resid0_mW"] = df2["power_total_mW"] - df2["p0_pred_mW"]

    def _run_mean_resid(run: str) -> float | None:
        m = df2["run_name"].astype(str).eq(run)
        if m.sum() == 0:
            return None
        return float(df2.loc[m, "resid0_mW"].mean())

    r_s4 = _run_mean_resid("20260201_213514_S4")
    r_s41 = _run_mean_resid("20260201_215338_S4-1")

    if r_s4 is not None and r_s41 is not None:
        # Offset applied only when GPS is OFF.
        k_gps_off = float(r_s4 - r_s41)
        k_gps_off = min(0.0, k_gps_off)
        gps_source = "S4_minus_S4-1"
    else:
        k_gps_off = 0.0
        gps_source = "none"

    # Cellular offset from S1 A/B residual means (if present).
    # We assume S1-HS-1 is cellular ON and S1-HS-2 is cellular OFF per configs/scenario_params.csv.
    def _run_mean_resid_cell(run: str) -> float | None:
        m = df2["run_name"].astype(str).eq(run)
        if m.sum() == 0:
            return None
        return float(df2.loc[m, "resid0_mW"].mean())

    r_s1_on = _run_mean_resid_cell("20260131_230812_S1-HS-1")
    r_s1_off = _run_mean_resid_cell("20260201_174510_S1-HS-2")
    if r_s1_on is not None and r_s1_off is not None:
        # Offset applied only when cellular is OFF.
        # If cellular OFF reduces power, residual on the OFF run is negative.
        k_cell_off = float(r_s1_off - r_s1_on)
        k_cell_off = min(0.0, k_cell_off)  # enforce: cellular-off should not increase power
        cell_source = "S1-HS-2_minus_S1-HS-1"
    else:
        k_cell_off = 0.0
        cell_source = "none"

    df2["power_pred_mW"] = (
        df2["p0_pred_mW"]
        + k_gps_off * (1.0 - df2["is_gps_on"].to_numpy(dtype=float))
        + k_cell_off * (1.0 - df2["cellular_on"].to_numpy(dtype=float))
    )
    df2["gps_fit_source"] = gps_source
    df2["cell_fit_source"] = cell_source

    params = ModelParamsV2(
        p_base_mW=p_base,
        k_screen=k_screen,
        k_cpu=k_cpu,
        k_leak_mW=k_leak,
        leak_gamma_per_C=float(leak_gamma_per_C),
        leak_tref_C=float(t_ref),
        k_gps_off_mW=float(k_gps_off),
        k_cellular_off_mW=float(k_cell_off),
        c_eff_mAh=4410.0,
    )

    return params, df2, thermal_df


def simulate_soc(df_run: pd.DataFrame, c_eff_mAh: float) -> pd.DataFrame:
    df = df_run.sort_values("t_s").copy().reset_index(drop=True)
    dt = _col_num(df, "dt_s", default=0.0).fillna(0.0).to_numpy(dtype=float)

    v = _col_num(df, "voltage_mV", default=np.nan) / 1000.0
    v = v.ffill().bfill().fillna(float(v.median()) if v.notna().any() else 3.85).to_numpy(dtype=float)

    p = _col_num(df, "power_pred_mW", default=np.nan).ffill().bfill().to_numpy(dtype=float)

    soc_meas = _col_num(df, "soc_pct", default=np.nan)
    soc0 = float(soc_meas.dropna().iloc[0]) / 100.0 if soc_meas.notna().any() else 0.5

    denom = 3600.0 * float(c_eff_mAh)
    soc = [soc0]
    for i in range(len(df) - 1):
        dti = float(dt[i])
        if not np.isfinite(dti) or dti <= 0:
            soc.append(soc[-1])
            continue

        vi = float(v[i])
        if not np.isfinite(vi) or vi <= 0:
            vi = 3.85

        pi = float(p[i])
        dsoc = (pi / (vi * denom)) * dti
        soc_next = soc[-1] - dsoc
        soc_next = min(1.0, max(0.0, soc_next))
        soc.append(soc_next)

    df["soc_sim"] = np.asarray(soc, dtype=float)
    df["soc_sim_pct"] = df["soc_sim"] * 100.0
    return df


def validate(df_all: pd.DataFrame, out_dir: Path, c_eff_mAh: float, *, suffix: str = "") -> pd.DataFrame:
    rows = []
    for run_name, g in df_all.groupby("run_name"):
        sim = simulate_soc(g, c_eff_mAh=c_eff_mAh)

        soc_meas = _col_num(sim, "soc_pct", default=np.nan)
        soc_sim = _col_num(sim, "soc_sim_pct", default=np.nan)
        m = soc_meas.notna() & soc_sim.notna()
        if m.sum() == 0:
            continue

        err = (soc_sim[m] - soc_meas[m]).to_numpy(dtype=float)
        rmse = float(np.sqrt(np.mean(err**2)))
        mape = float(np.mean(np.abs(err) / np.clip(np.abs(soc_meas[m].to_numpy(dtype=float)), 1e-6, None)) * 100.0)

        duration_s = float(_col_num(sim, "dt_s", default=0.0).fillna(0.0).sum())

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
                "p_meas_mean_mW": float(_col_num(sim, "power_total_mW", default=np.nan).dropna().mean()),
                "p_pred_mean_mW": float(_col_num(sim, "power_pred_mW", default=np.nan).dropna().mean()),
            }
        )

    val = pd.DataFrame(rows).sort_values("run_name")
    val.to_csv(out_dir / f"model_validation_v2{suffix}.csv", index=False, encoding="utf-8")

    # Plot measured vs predicted mean power per run
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
        ax.set_title("Power model validation v2 (per-run mean)")
        ax.grid(True, alpha=0.3)
        fig.tight_layout()
        fig.savefig(out_dir / f"model_validation_v2{suffix}.png", dpi=150)

    return val


def main() -> int:
    ap = argparse.ArgumentParser(description="v2: thermal model (1/2-state) + structured leak + GPS A/B calibration")
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
        help="Output dir",
    )
    ap.add_argument(
        "--alpha",
        type=float,
        default=2000.0,
        help="Ridge strength for base power fit (GPS-OFF only)",
    )
    ap.add_argument(
        "--c-eff-mAh",
        type=float,
        default=4410.0,
        help="Effective capacity (mAh) used in SOC ODE",
    )
    ap.add_argument(
        "--leak-doubling-C",
        type=float,
        default=10.0,
        help="Leakage doubles every N degrees C (Arrhenius-inspired prior)",
    )
    ap.add_argument(
        "--thermal-model",
        choices=["1state", "2state"],
        default="1state",
        help="Thermal model used to generate temperature for leakage feature.",
    )
    ap.add_argument(
        "--leak-temp-mix-cpu",
        type=float,
        default=0.7,
        help="For 2state: leak_temp = mix*cpu_hat + (1-mix)*batt_hat (0..1).",
    )

    args = ap.parse_args()

    all_df = pd.read_csv(args.input)

    leak_gamma = math.log(2.0) / float(args.leak_doubling_C)

    params, df_pred, thermal_df = fit_power_model_v2(
        all_df,
        alpha=float(args.alpha),
        leak_gamma_per_C=leak_gamma,
        thermal_model=str(args.thermal_model),
        leak_temp_mix_cpu=float(args.leak_temp_mix_cpu),
    )
    params.c_eff_mAh = float(args.c_eff_mAh)

    args.out_dir.mkdir(parents=True, exist_ok=True)

    suffix = "" if str(args.thermal_model) == "1state" else f"_{str(args.thermal_model)}"

    (args.out_dir / f"model_params_v2{suffix}.json").write_text(
        json.dumps(asdict(params), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    thermal_df.to_csv(args.out_dir / f"thermal_fit_v2{suffix}.csv", index=False, encoding="utf-8")

    validate(df_pred, out_dir=args.out_dir, c_eff_mAh=params.c_eff_mAh, suffix=suffix)

    # Save a thin per-sample dump for debugging A/B and thermal
    keep_cols = [
        "run_name",
        "t_s",
        "dt_s",
        "soc_pct",
        "voltage_mV",
        "temperature_C",
        "temperature_cpu_C",
        "temp_cpu_hat_C",
        "temp_batt_hat_C",
        "temp_leak_hat_C",
        "power_total_mW",
        "power_cpu_mW",
        "power_screen_mW",
        "is_gps_on",
        "cellular_on",
        "p0_pred_mW",
        "power_pred_mW",
        "resid0_mW",
        "gps_fit_source",
        "cell_fit_source",
    ]
    cols = [c for c in keep_cols if c in df_pred.columns]
    df_pred[cols].to_csv(args.out_dir / f"model_samples_v2{suffix}.csv", index=False, encoding="utf-8")

    print(f"Thermal model: {args.thermal_model}")
    print(f"Wrote: {args.out_dir / f'model_params_v2{suffix}.json'}")
    print(f"Wrote: {args.out_dir / f'thermal_fit_v2{suffix}.csv'}")
    print(f"Wrote: {args.out_dir / f'model_samples_v2{suffix}.csv'}")
    print(f"Wrote: {args.out_dir / f'model_validation_v2{suffix}.csv'}")
    print(f"Wrote: {args.out_dir / f'model_validation_v2{suffix}.png'}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
