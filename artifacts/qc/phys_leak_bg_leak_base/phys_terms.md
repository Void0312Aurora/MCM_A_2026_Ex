# Physical Terms Test: i2r + leakage + base (LOSO)

Source eval: `artifacts/models/eval_run_metrics_v2.csv`
QC keep only: True
temp_source: mean
terms: ['base', 'leak']
gamma grid: [0.0, 0.06] steps=31

## Error summary

```text
metric,n,mae_mW,rmse_mW,bias_mW
base,20,194.23139497036425,283.4179564880534,42.40348995426022
phys_corrected,20,307.23839218023244,376.01211236114847,-232.38754333903293

```

## Residual correlations

```text
covariate,r_before,r_after
thermal_cpu,0.6679091063470664,0.5804797019350924
thermal_batt,0.2844568215857221,0.0906719180053883
soc,-0.2158084076540227,-0.4488222555072387
voltage,-0.2708188252242883,-0.4981479018689876
current_uA,0.8908956949945606,0.8819575666188024

```