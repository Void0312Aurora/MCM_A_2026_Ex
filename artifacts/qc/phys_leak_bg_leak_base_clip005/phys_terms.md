# Physical Terms Test: i2r + leakage + base (LOSO)

Source eval: `artifacts/models/eval_run_metrics_v2.csv`
QC keep only: True
temp_source: mean
terms: ['base', 'leak']
gamma grid: [0.0, 0.02] steps=21

## Error summary

```text
metric,n,mae_mW,rmse_mW,bias_mW
base,20,194.23139497036425,283.4179564880534,42.40348995426022
phys_corrected,20,184.05555827443575,272.2041358227928,-51.55153292352369

```

## Residual correlations

```text
covariate,r_before,r_after
thermal_cpu,0.6679091063470664,0.6554055738655721
thermal_batt,0.2844568215857221,0.2732207772639654
soc,-0.2158084076540227,-0.2264462981857772
voltage,-0.2708188252242883,-0.280084703826251
current_uA,0.8908956949945606,0.883263104800335

```