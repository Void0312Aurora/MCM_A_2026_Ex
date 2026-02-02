# Residual Correction (LOSO) Report

Source eval: `artifacts/models/thermal2_mix1_fix/eval_run_metrics_v2_2state.csv`
QC keep only: True
Covariates: ['battery_level0_pct', 'voltage_V', 'thermal_cpu0_C', 'thermal_batt0_C']

## Summary

```text
metric,n,mae_mW,rmse_mW,bias_mW
base,20,195.53962302629833,289.1759098901395,39.94670516240711
corrected,20,180.52192325117704,250.54366608254125,-5.131208507160923

```