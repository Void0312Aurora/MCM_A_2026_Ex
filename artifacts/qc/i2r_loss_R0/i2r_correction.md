# I^2 R_int Loss Term Test (LOSO)

Source eval: `artifacts/models/eval_run_metrics_v2.csv`
QC keep only: True
R_int model: `R0` (Tref=40.0C)

## Error summary

```text
metric,n,mae_mW,rmse_mW,bias_mW
base,20,194.23139497036425,283.4179564880534,42.40348995426022
i2r_corrected,20,170.22375709561567,241.5278891567485,-111.03814895230155

```

## Residual correlations

```text
covariate,r_before,r_after
thermal_cpu0_C,0.6167047388740758,0.5037153570610579
battery_level0_pct,-0.2158084076540227,-0.1753635647187769
perfetto_voltage_mean_V,-0.2708188252242883,-0.2234742303525974
perfetto_current_mean_uA,0.8908956949945606,0.7761123031072645

```