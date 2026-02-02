# I^2 R_int Loss Term Test (LOSO)

Source eval: `artifacts/models/eval_run_metrics_v2.csv`
QC keep only: True
R_int model: `R0_Rsoc_Rtpos` (Tref=40.0C)

## Error summary

```text
metric,n,mae_mW,rmse_mW,bias_mW
base,20,194.23139497036425,283.4179564880534,42.40348995426022
i2r_corrected,20,142.0420824166979,217.1390818560158,-82.35811355521213

```

## Residual correlations

```text
covariate,r_before,r_after
thermal_cpu0_C,0.6167047388740758,0.3430993975919517
battery_level0_pct,-0.2158084076540227,-0.0215912531044178
perfetto_voltage_mean_V,-0.2708188252242883,-0.0613146010478761
perfetto_current_mean_uA,0.8908956949945606,0.5762460070688055

```