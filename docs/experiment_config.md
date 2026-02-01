# 实验组配置（来自 run.log）

本文把 [run.log](../run.log) 中的口头配置整理成“可用于建模/报告”的结构化定义，并对应到建模输入字段。

## 共性（所有组）
- WiFi：开
- 采样窗口：单次实验约 540s（受无 root 约束）
- 总功耗观测口径：Perfetto `android.power`（`power_mw_calc` 对齐到采样点）

## S1（网络基线/蜂窝开关）
- WiFi：开
- 蜂窝网络：开 / 关（组内对照）
- GPS：开
- 屏幕：关
- 主要目的：识别蜂窝网络带来的稳态功耗偏置（run-level offset）。

## S2（屏幕亮度梯度）
- WiFi：开
- 蜂窝网络：开
- GPS：开
- 屏幕：开
- 亮度：30 / 90 / 150 / 210（组内梯度）
- 主要目的：校准屏幕功耗代理项（`screen_power_mW_est`）与总功耗的一致性。

## S3（CPU 负载测试）
- WiFi：开
- 蜂窝网络：开
- GPS：开
- 屏幕：关
- 注：CPU 测试（空载 vs 负载）
- 主要目的：校准 CPU 功耗代理项（`power_cpu_mW`）与总功耗的一致性，并为热模型提供“发热输入”。

## S4（GPS 开关对照）
- WiFi：开
- 蜂窝网络：开
- GPS：开 / 关（组内对照）
- 屏幕：关
- 主要目的：识别 GPS 打开带来的稳态功耗增量（A/B 识别）。

## 建模落地方式
- 在 [configs/scenario_params.csv](../configs/scenario_params.csv) 中把上述配置落为场景级参数（wifi/cellular/gps/screen/亮度/CPU 测试标识）。
- 预处理脚本 [scripts/model_preprocess.py](../scripts/model_preprocess.py) 会把这些参数合并到每个样本行，生成 `wifi_on / cellular_on / is_gps_on / screen_on_cfg / brightness_target / cpu_test` 等字段。
- v2 模型 [scripts/model_battery_soc_v2_thermal1.py](../scripts/model_battery_soc_v2_thermal1.py) 用 S4 vs S4-1 的 A/B 残差来标定 GPS（作为“GPS OFF 偏置”，因此系数预期为负；其相反数即 GPS ON 增量）。
