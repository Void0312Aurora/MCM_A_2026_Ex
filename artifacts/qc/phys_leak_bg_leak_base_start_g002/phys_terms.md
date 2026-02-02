# Physical Terms Test: i2r + leakage + base (LOSO)

Source eval: `artifacts/models/eval_run_metrics_v2.csv`
QC keep only: True
temp_source: start
terms: ['base', 'leak']
gamma grid: [0.0, 0.02] steps=21

## Error summary

```text
metric,n,mae_mW,rmse_mW,bias_mW
base,20,194.23139497036425,283.4179564880534,42.40348995426022
phys_corrected,20,218.16776860959104,302.82629494538264,-97.29197094316454

```

## Residual correlations

```text
covariate,r_before,r_after
thermal_cpu,0.6167047388740758,0.5944108009576585
thermal_batt,0.123472025882518,0.0839587144742508
soc,-0.2158084076540227,-0.2661344406954044
voltage,-0.2708188252242883,-0.3208529133147323
current_uA,0.8908956949945606,0.8965014124977249

```