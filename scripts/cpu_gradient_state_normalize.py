from __future__ import annotations

import argparse
import re
from pathlib import Path

import numpy as np
import pandas as pd


def _try_emit_plot(out_dir: Path, out: pd.DataFrame) -> None:
    try:
        import matplotlib.pyplot as plt
    except Exception:
        return

    dfp = out.dropna(subset=["threads", "power_mW_obs", "power_mW_hat_at_ref_state"]).copy()
    if dfp.empty:
        return

    fig, ax = plt.subplots(figsize=(8, 4.5), dpi=160)
    # raw points
    ax.scatter(dfp["threads"], dfp["power_mW_obs"], s=28, alpha=0.85, label="raw (obs)")
    ax.scatter(
        dfp["threads"],
        dfp["power_mW_hat_at_ref_state"],
        s=28,
        alpha=0.85,
        label="state-normalized (at ref)",
    )

    # connect per-run
    for _, r in dfp.iterrows():
        ax.plot(
            [r["threads"], r["threads"]],
            [r["power_mW_obs"], r["power_mW_hat_at_ref_state"]],
            color="gray",
            alpha=0.25,
            linewidth=1.0,
        )

    ax.set_xlabel("threads")
    ax.set_ylabel("power (mW)")
    ax.set_title("CPU gradient: raw vs state-normalized")
    ax.grid(True, alpha=0.25)
    ax.legend(loc="best")
    fig.tight_layout()

    out_path = out_dir / "cpu_gradient_raw_vs_normalized.png"
    fig.savefig(out_path)
    plt.close(fig)


THREAD_RE = re.compile(r"(?:^|_)t(?P<t>\d+)(?:$|_)", re.IGNORECASE)


def _parse_threads(scenario: str) -> float:
    if not isinstance(scenario, str):
        return np.nan
    m = THREAD_RE.search(scenario)
    if not m:
        return np.nan
    try:
        return float(int(m.group("t")))
    except Exception:
        return np.nan


def _huber_weights(r: np.ndarray, c: float) -> np.ndarray:
    ar = np.abs(r)
    w = np.ones_like(ar)
    mask = ar > c
    w[mask] = c / ar[mask]
    return w


def fit_huber_irls(X: np.ndarray, y: np.ndarray, *, c: float = 1.5, iters: int = 25) -> np.ndarray:
    """Huber IRLS on standardized residual scale.

    X: (n, p) with intercept included
    y: (n,)
    """
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


def main() -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Normalize CPU gradient (S3_load_t*) perfetto power to a reference start-state "
            "using a lightweight regression. No re-tests required."
        )
    )
    ap.add_argument("--qc-run-summary", type=Path, default=Path("artifacts/qc/qc_run_summary.csv"))
    ap.add_argument("--out-dir", type=Path, default=Path("artifacts/qc"))
    ap.add_argument("--scenario-prefix", type=str, default="S3_load_t")

    ap.add_argument("--ref-soc", type=float, default=np.nan)
    ap.add_argument("--ref-voltage-mV", type=float, default=np.nan)
    ap.add_argument("--ref-thermal-cpu", type=float, default=np.nan)
    ap.add_argument("--ref-thermal-batt", type=float, default=np.nan)

    ap.add_argument("--use-only-qc-keep", action="store_true")
    ap.add_argument("--huber-c", type=float, default=1.5)
    ap.add_argument("--huber-iters", type=int, default=25)
    ap.add_argument("--emit-plot", action="store_true")

    args = ap.parse_args()

    df = pd.read_csv(args.qc_run_summary)
    if df.empty:
        print("Empty QC summary.")
        return 1

    df = df[df["scenario"].astype(str).str.startswith(args.scenario_prefix)].copy()
    if args.use_only_qc_keep and "qc_keep" in df.columns:
        df = df[pd.to_numeric(df["qc_keep"], errors="coerce").fillna(0).astype(int) == 1].copy()

    if df.empty:
        print(f"No runs matching prefix {args.scenario_prefix!r}.")
        return 1

    df["threads"] = df["scenario"].astype(str).map(_parse_threads)

    # Candidate features: intercept, threads, voltage_V, soc_pct, thermal_cpu_C, thermal_batt_C
    y = pd.to_numeric(df.get("perfetto_power_mean_mW"), errors="coerce")
    soc = pd.to_numeric(df.get("battery_level0_pct"), errors="coerce")
    v_mV = pd.to_numeric(df.get("battery_voltage0_mV"), errors="coerce")
    t_cpu = pd.to_numeric(df.get("thermal_cpu0_C"), errors="coerce")
    t_batt = pd.to_numeric(df.get("thermal_batt0_C"), errors="coerce")

    X_full = pd.DataFrame(
        {
            "intercept": 1.0,
            "threads": pd.to_numeric(df["threads"], errors="coerce"),
            "voltage_V": v_mV / 1000.0,
            "soc_pct": soc,
            "thermal_cpu_C": t_cpu,
            "thermal_batt_C": t_batt,
        }
    )

    # Feature selection: start with mandatory columns, then greedily add optional columns
    # only if we still have enough complete rows (n >= p+1).
    mandatory = ["intercept", "threads"]
    optional = ["voltage_V", "soc_pct", "thermal_cpu_C", "thermal_batt_C"]

    # Ensure y + mandatory exist
    m_base = np.isfinite(y.to_numpy(float))
    for c in mandatory:
        m_base &= np.isfinite(pd.to_numeric(X_full[c], errors="coerce").to_numpy(float))

    selected = list(mandatory)

    def mask_for(cols: list[str]) -> np.ndarray:
        m2 = m_base.copy()
        for cc in cols:
            m2 &= np.isfinite(pd.to_numeric(X_full[cc], errors="coerce").to_numpy(float))
        return m2

    # Greedy: add the optional feature that preserves the largest usable n.
    remaining = list(optional)
    while remaining:
        best = None
        best_n = -1
        best_cols = None
        for cand in remaining:
            cols2 = selected + [cand]
            m2 = mask_for(cols2)
            n2 = int(np.sum(m2))
            p2 = len(cols2)
            if n2 >= (p2 + 1) and n2 > best_n:
                best = cand
                best_n = n2
                best_cols = cols2

        if best is None:
            break
        selected = best_cols  # type: ignore[assignment]
        remaining.remove(best)

    X = X_full[selected].copy()

    # Fit mask requires all selected columns
    m = mask_for(selected)

    df_fit = df.loc[m].copy()
    X_fit = X.loc[m].to_numpy(float)
    y_fit = y.loc[m].to_numpy(float)

    # Need at least p+1 rows to fit stably
    p = int(X_fit.shape[1])
    if len(df_fit) < (p + 1):
        print(f"Not enough complete rows to fit (need >={p+1}, got {len(df_fit)}).")
        print(f"Selected features: {selected}")
        return 2

    beta = fit_huber_irls(X_fit, y_fit, c=float(args.huber_c), iters=int(args.huber_iters))

    # Reference state: user-provided or medians
    # Reference state for selected optional features (others ignored)
    ref: dict[str, float] = {}
    if "voltage_V" in selected:
        ref["voltage_V"] = float(args.ref_voltage_mV / 1000.0) if np.isfinite(args.ref_voltage_mV) else float(
            np.median(X_fit[:, selected.index("voltage_V")])
        )
    if "soc_pct" in selected:
        ref["soc_pct"] = float(args.ref_soc) if np.isfinite(args.ref_soc) else float(
            np.median(X_fit[:, selected.index("soc_pct")])
        )
    if "thermal_cpu_C" in selected:
        ref["thermal_cpu_C"] = float(args.ref_thermal_cpu) if np.isfinite(args.ref_thermal_cpu) else float(
            np.median(X_fit[:, selected.index("thermal_cpu_C")])
        )
    if "thermal_batt_C" in selected:
        ref["thermal_batt_C"] = float(args.ref_thermal_batt) if np.isfinite(args.ref_thermal_batt) else float(
            np.median(X_fit[:, selected.index("thermal_batt_C")])
        )

    # Predict each run's power at reference state (keeping its threads)
    X_all = X.to_numpy(float)
    y_hat = X_all @ beta

    X_ref = X_all.copy()
    if "voltage_V" in selected:
        X_ref[:, selected.index("voltage_V")] = ref["voltage_V"]
    if "soc_pct" in selected:
        X_ref[:, selected.index("soc_pct")] = ref["soc_pct"]
    if "thermal_cpu_C" in selected:
        X_ref[:, selected.index("thermal_cpu_C")] = ref["thermal_cpu_C"]
    if "thermal_batt_C" in selected:
        X_ref[:, selected.index("thermal_batt_C")] = ref["thermal_batt_C"]
    y_hat_ref = X_ref @ beta

    out = df.copy()
    out["power_mW_obs"] = y
    out["power_mW_hat"] = y_hat
    out["power_mW_hat_at_ref_state"] = y_hat_ref
    out["resid_mW"] = out["power_mW_obs"] - out["power_mW_hat"]

    # Per-thread summary: raw vs normalized spread
    g = out.dropna(subset=["threads", "power_mW_obs", "power_mW_hat_at_ref_state"]).groupby("threads")
    summary = g.agg(
        n=("power_mW_obs", "count"),
        raw_mean_mW=("power_mW_obs", "mean"),
        raw_std_mW=("power_mW_obs", "std"),
        raw_cv=("power_mW_obs", lambda s: float(s.std(ddof=1) / s.mean()) if s.mean() else np.nan),
        ref_mean_mW=("power_mW_hat_at_ref_state", "mean"),
        ref_std_mW=("power_mW_hat_at_ref_state", "std"),
        ref_cv=("power_mW_hat_at_ref_state", lambda s: float(s.std(ddof=1) / s.mean()) if s.mean() else np.nan),
        raw_ratio_max_min=("power_mW_obs", lambda s: float(s.max() / s.min()) if s.min() else np.nan),
        ref_ratio_max_min=("power_mW_hat_at_ref_state", lambda s: float(s.max() / s.min()) if s.min() else np.nan),
    ).reset_index()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    out_csv = args.out_dir / "cpu_gradient_state_normalized.csv"
    out.to_csv(out_csv, index=False, encoding="utf-8")

    sum_csv = args.out_dir / "cpu_gradient_state_normalized_summary.csv"
    summary.sort_values("threads").to_csv(sum_csv, index=False, encoding="utf-8")

    # Markdown report
    md = []
    md.append("# CPU Gradient State-Normalization Report")
    md.append("")
    md.append(f"Source: `{args.qc_run_summary.as_posix()}`")
    md.append(f"Filter: scenario startswith `{args.scenario_prefix}`; use_only_qc_keep={bool(args.use_only_qc_keep)}")
    md.append("")
    md.append("## Reference state")
    md.append("")
    if ref:
        for k in ["voltage_V", "soc_pct", "thermal_cpu_C", "thermal_batt_C"]:
            if k in ref:
                if k == "soc_pct":
                    md.append(f"- {k}: {ref[k]:.1f}")
                elif k == "voltage_V":
                    md.append(f"- {k}: {ref[k]:.3f}")
                else:
                    md.append(f"- {k}: {ref[k]:.2f}")
    else:
        md.append("- (none; fit used only intercept+threads)")
    md.append("")
    md.append("## Regression coefficients (Huber IRLS)")
    md.append("")
    cols = list(X.columns)
    for name, b in zip(cols, beta):
        md.append(f"- {name}: {b:.4f}")
    md.append("")
    md.append("## Per-thread spread: raw vs normalized")
    md.append("")
    md.append("(See CSV for full table)")
    md.append("")

    # Include a compact view for t4 if present
    t4 = out[out["threads"] == 4].copy()
    if len(t4) >= 1:
        md.append("## t4 runs (raw vs normalized)")
        md.append("")
        cols_keep = [
            "run_name",
            "scenario",
            "power_mW_obs",
            "battery_level0_pct",
            "battery_voltage0_mV",
            "thermal_cpu0_C",
            "thermal_batt0_C",
            "power_mW_hat_at_ref_state",
            "qc_keep",
            "qc_reject_reasons",
        ]
        cols_keep = [c for c in cols_keep if c in t4.columns]
        # Avoid optional dependency on `tabulate` by emitting a CSV-style block.
        md.append("```text")
        md.append(t4[cols_keep].to_csv(index=False))
        md.append("```")
        md.append("")

    out_md = args.out_dir / "cpu_gradient_state_normalized.md"
    out_md.write_text("\n".join(md), encoding="utf-8")

    if args.emit_plot:
        _try_emit_plot(args.out_dir, out)

    print(f"Wrote: {out_csv}")
    print(f"Wrote: {sum_csv}")
    print(f"Wrote: {out_md}")
    if args.emit_plot:
        print(f"Wrote: {args.out_dir / 'cpu_gradient_raw_vs_normalized.png'}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
