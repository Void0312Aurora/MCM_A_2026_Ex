# CPU Gradient State-Normalization Report

Source: `artifacts/qc/qc_run_summary.csv`
Filter: scenario startswith `S3_load_t`; use_only_qc_keep=True

## Reference state

- (none; fit used only intercept+threads)

## Regression coefficients (Huber IRLS)

- intercept: 1691.5078
- threads: 225.9789

## Per-thread spread: raw vs normalized

(See CSV for full table)

## t4 runs (raw vs normalized)

```text
run_name,scenario,power_mW_obs,battery_level0_pct,battery_voltage0_mV,thermal_cpu0_C,thermal_batt0_C,power_mW_hat_at_ref_state,qc_keep,qc_reject_reasons
20260202_220816_S3_load_t4,S3_load_t4,2546.296188338732,74.0,4116.0,49.719,34.0,2595.423574680307,1,

```
