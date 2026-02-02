# CPU Gradient State-Normalization Report

Source: `artifacts/qc/qc_run_summary.csv`
Filter: scenario startswith `S3_load_t`; use_only_qc_keep=False

## Reference state

- voltage_V: 4.101
- soc_pct: 73.0
- thermal_cpu_C: 50.26

## Regression coefficients (Huber IRLS)

- intercept: 94677.3975
- threads: 61.1813
- voltage_V: -25786.2657
- soc_pct: 193.2368
- thermal_cpu_C: -10.9215

## Per-thread spread: raw vs normalized

(See CSV for full table)

## t4 runs (raw vs normalized)

```text
run_name,scenario,power_mW_obs,battery_level0_pct,battery_voltage0_mV,thermal_cpu0_C,thermal_batt0_C,power_mW_hat_at_ref_state,qc_keep,qc_reject_reasons
20260202_220816_S3_load_t4,S3_load_t4,2546.296188338732,74.0,4116.0,49.719,34.0,2717.1159513296852,1,
20260201_204549_S3_load_t4,S3_load_t4,4145.503761684406,33.0,3735.0,77.008,37.3,2717.1159513296852,0,soc<50.0;thermal_cpu0>60.0C

```
