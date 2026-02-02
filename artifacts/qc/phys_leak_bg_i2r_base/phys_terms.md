# Physical Terms Test: i2r + leakage + base (LOSO)

Source eval: `artifacts/models/eval_run_metrics_v2.csv`
QC keep only: True
temp_source: mean
terms: ['base', 'i2r']
gamma grid: [0.0, 0.06] steps=31

## Error summary

```text
metric,n,mae_mW,rmse_mW,bias_mW
base,20,194.23139497036425,283.4179564880534,42.40348995426022
phys_corrected,20,173.9099840918616,246.2759478492202,-156.10633355620993

```

## Residual correlations

```text
covariate,r_before,r_after
thermal_cpu,0.6679091063470664,0.2625178781072389
thermal_batt,0.2844568215857221,0.126195549772757
soc,-0.2158084076540227,-0.0459178578780236
voltage,-0.2708188252242883,-0.0799125293022788
current_uA,0.8908956949945606,0.5081354811075299

```