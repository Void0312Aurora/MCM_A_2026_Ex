# Physical Terms Test: i2r + leakage + base (LOSO)

Source eval: `artifacts/models/eval_run_metrics_v2.csv`
QC keep only: True
temp_source: mean
terms: ['base', 'i2r', 'leak']
gamma grid: [0.0, 0.06] steps=31

## Error summary

```text
metric,n,mae_mW,rmse_mW,bias_mW
base,20,194.23139497036425,283.4179564880534,42.40348995426022
phys_corrected,20,287.7516783996475,359.4179840635064,-270.1873304343741

```

## Residual correlations

```text
covariate,r_before,r_after
thermal_cpu,0.6679091063470664,-0.3732716664975816
thermal_batt,0.2844568215857221,-0.2248236355877979
soc,-0.2158084076540227,0.0682506648036924
voltage,-0.2708188252242883,0.0707221624284307
current_uA,0.8908956949945606,-0.1690775030482759

```