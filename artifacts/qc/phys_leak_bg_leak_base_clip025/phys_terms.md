# Physical Terms Test: i2r + leakage + base (LOSO)

Source eval: `artifacts/models/eval_run_metrics_v2.csv`
QC keep only: True
temp_source: mean
terms: ['base', 'leak']
gamma grid: [0.0, 0.04] steps=21

## Error summary

```text
metric,n,mae_mW,rmse_mW,bias_mW
base,20,194.23139497036425,283.4179564880534,42.40348995426022
phys_corrected,20,338.7028310631401,398.698322851391,-262.50675639023893

```

## Residual correlations

```text
covariate,r_before,r_after
thermal_cpu,0.6679091063470664,0.6429654088367431
thermal_batt,0.2844568215857221,0.149967630694459
soc,-0.2158084076540227,-0.4169011260696999
voltage,-0.2708188252242883,-0.4674564381121201
current_uA,0.8908956949945606,0.8970604604261802

```