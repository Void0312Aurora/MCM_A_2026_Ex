# Physical Terms Test: i2r + leakage + base (LOSO)

Source eval: `artifacts/models/eval_run_metrics_v2.csv`
QC keep only: True
temp_source: mean
terms: ['i2r']
gamma grid: [0.0, 0.06] steps=31

## Error summary

```text
metric,n,mae_mW,rmse_mW,bias_mW
base,20,194.23139497036425,283.4179564880534,42.40348995426022
phys_corrected,20,139.9594576202784,214.8034909694425,-80.62318493674883

```

## Residual correlations

```text
covariate,r_before,r_after
thermal_cpu,0.6679091063470664,0.2852485548850195
thermal_batt,0.2844568215857221,0.1440567199545271
soc,-0.2158084076540227,-0.0330177070920216
voltage,-0.2708188252242883,-0.0698542732266085
current_uA,0.8908956949945606,0.5332864755451237

```