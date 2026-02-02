# Model vs Start-State Covariates Analysis

## Error summary

```text
subset,n,mae_mW,rmse_mW,bias_mW
v2_1state:all,24,210.46707808251765,293.12489408759933,30.050101605422515
v2_1state:qc_keep,20,194.23139497036422,283.4179564880534,42.40348995426022
v2_2state:all,24,302.785534685793,487.3302001812305,7.28600563496272
v2_2state:qc_keep,20,225.3005894475271,328.82670620173224,-28.870146511435724

```

## Residual-covariate correlations (Pearson r)

Interpretation: if residual correlates with a covariate, model is not fully accounting for it (or proxy is noisy).

```text
covariate,n,pearson_r,subset
thermal_cpu0_C,24,0.5759984922250708,v2_1state:all
battery_voltage0_mV,24,-0.12033542662647323,v2_1state:all
battery_level0_pct,24,-0.09586687605624135,v2_1state:all
thermal_batt0_C,24,0.09088918152447444,v2_1state:all
thermal_cpu0_C,20,0.6167047388740758,v2_1state:qc_keep
battery_voltage0_mV,20,-0.2404282788505708,v2_1state:qc_keep
battery_level0_pct,20,-0.2158084076540227,v2_1state:qc_keep
thermal_batt0_C,20,0.12347202588251803,v2_1state:qc_keep
thermal_cpu0_C,24,0.8269136067732358,v2_2state:all
battery_voltage0_mV,24,-0.32024485791489216,v2_2state:all
battery_level0_pct,24,-0.3131681299330967,v2_2state:all
thermal_batt0_C,24,0.16183170873072564,v2_2state:all
thermal_cpu0_C,20,0.6161143210872879,v2_2state:qc_keep
battery_voltage0_mV,20,-0.33177081036839506,v2_2state:qc_keep
battery_level0_pct,20,-0.3078776206103007,v2_2state:qc_keep
thermal_batt0_C,20,0.019670141382534048,v2_2state:qc_keep

```