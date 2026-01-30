# 数据采集与实验协议（ADB，v0）

更新时间：2026-01-30

目标：用可复现的方式采集“足以做参数估计与验证”的数据，支持连续时间 SOC 模型；强调数据支持而非替代模型。

## 1. 设备与前置条件

- 设备：Android 手机（已验证 ADB 可用，shell uid=2000）。
- 连接：USB 调试开启，`adb devices -l` 显示 `device`。
- 权限边界：部分电池 sysfs（/sys/class/power_supply/battery/*）可能拒绝读取；优先使用 `dumpsys battery` 与 `dumpsys thermalservice`。

## 1.1 无线调试（推荐，避免 USB 供电干扰）

当你希望“电脑不通过 USB 给手机供电”时，推荐使用 Android 11+ 的 **无线调试**。

- 手机端：开发者选项 → 无线调试 → 打开 →「使用配对码配对设备」
- 电脑端（PowerShell）：
	- `echo <配对码> | adb pair <手机显示的IP:配对端口>`
	- 配对成功后通常会自动出现一个设备 serial，形如 `..._adb-tls-connect._tcp`
	- 用 `adb devices -l` 查看当前无线设备是否为 `device`

脚本 `scripts/adb_sample_power.py` 在未指定 `--serial` 时会自动优先选择 `_adb-tls-connect._tcp` 的无线设备。

## 2. 采样字段（最小集）

建议 1–5 秒采样一次，记录时间戳（PC 端）与场景标签。

### 2.1 电池/电量（必需）

来自 `adb shell dumpsys battery`：
- level, scale（SOC%）
- voltage（mV）
- temperature（0.1°C）
- charge counter（uAh，若存在）

备注：如出现 `UPDATES STOPPED`，执行 `adb shell dumpsys battery reset` 或 `adb shell cmd battery reset`。

### 2.2 热（推荐）

来自 `adb shell dumpsys thermalservice`：
- BATTERY / SKIN / SOC / CPU 等温度传感器值
- Thermal Status

### 2.3 屏幕亮度代理（推荐）

- `adb shell settings get system screen_brightness`

备注：部分设备上 `adb shell settings put ...` 会因缺少 WRITE_SETTINGS 而失败。
此时仍可通过“手动调节亮度 + ADB 读取数值(0–255)”完成严谨的 S2 亮度实验。
可用脚本按当前亮度自动命名并开跑：
- `D:/workshop/MP_power/.venv/Scripts/python.exe scripts/s2_run_from_current_brightness.py --duration 540 --thermal --auto-reset-battery`

在一些机型上，也可以通过 AppOps 允许 shell 写设置（不保证所有系统都支持）：
- `adb shell appops set com.android.shell WRITE_SETTINGS allow`
随后即可尝试 `settings put system screen_brightness ...`。

### 2.4 CPU 频率与驻留（强推荐，利于解释功耗项）

- 实时频率：`/sys/devices/system/cpu/cpu*/cpufreq/scaling_cur_freq`
- 驻留统计：`/sys/devices/system/cpu/cpufreq/policy*/stats/time_in_state`

用途：与 power_profile（overlay）中的 cpu.core_power.clusterX 结合，得到 CPU 子系统能耗的解释性估计（用于分解 P(t)、给参数先验）。

## 3. 实验场景设计（用于估计不同功耗项）

本项目以“**可比性优先**”为目标：先把最小集合（S0/S1/S2）标准化，后续扩展再加场景。

### 3.1 标准实验矩阵（必须遵守）

请以 [docs/experiment_matrix.zh-CN.md](docs/experiment_matrix.zh-CN.md) 为准，它定义了：
- 必须测什么（v0 最小集合）
- 每条 run 的固定项/记录项
- 统一的验收标准与剔除规则

### 3.2 场景定义（v0 最小集合）

> 重点不是“做很多对照”，而是“固定条件 + 可验收 + 可解释”。

- S0 仪器自检（5–8 分钟）：验证无线 ADB 稳定、charge_counter 分辨率、thermal 列可用。
- S1 基线（屏幕关）：在**固定网络条件**下估计 `P_base` 的均值与波动。
- S2 亮度阶梯（屏幕开）：在**固定网络条件**下估计 `P_screen(brightness)`。

### 3.3 可选扩展（按需要添加）

- S3 CPU 负载：用于校准/解释 CPU 项（time_in_state + power_profile 缩放）。
- S4 网络模式：用于区分 Wi‑Fi vs 蜂窝等状态常值项（不追求穷举对照）。

## 4. 数据格式（CSV 规范建议）

建议列：
- ts_pc（ISO 时间）
- scenario（S1/S2/...）
- note（场景标签/事件）
- battery_level, battery_voltage_mv, battery_temp_deciC, charge_counter_uAh
- brightness
- cpu_policy0_time_in_state_delta_ms（可展开为多列）
- thermal_battery_C, thermal_skin_C, thermal_soc_C（可选）

原则：字段缺失允许为空，但必须在元数据里说明缺失原因。

## 4.1 脚本化采样与解析（可复现）

本仓库已提供最小闭环脚本（Windows/PowerShell 下直接运行）。

- 解析 overlay 的 power_profile（生成 cluster 频点-电流表）：
	- `D:/workshop/MP_power/.venv/Scripts/python.exe scripts/parse_power_profile_overlay.py artifacts/android/overlays/FrameworkResOverlay_power_profile_xmltree.txt`
	- 产物输出到：`artifacts/android/power_profile/`（含 `clusterX_freq_power.csv` 与 `power_profile.json`）

- ADB 连续采样写入 CSV（带“假断电/断连”容错：写 `adb_error` 列不中断进程）：
	- `D:/workshop/MP_power/.venv/Scripts/python.exe scripts/adb_sample_power.py --scenario S1 --duration 600 --interval 2 --auto-reset-battery`
	- 默认输出到：`artifacts/runs/<run_id>_<scenario>.csv`
	- 若同时连了多个设备，建议显式指定：`--serial <adb devices -l 显示的serial>`
	- 可选采集热传感器：追加 `--thermal`（会增加 `thermal_status` 与 `thermal_<name>_C` 列）

- 自动推断 cpufreq policy → power_profile cluster 映射（用于后续批量算能耗列）：
	- `D:/workshop/MP_power/.venv/Scripts/python.exe scripts/map_policy_to_cluster.py --serial <serial>`
	- 产物输出到：`artifacts/android/power_profile/policy_cluster_map.json`

- 基于 mapping + power_profile 批量给采样 CSV 增加 CPU 能耗列（每个 policy + 总和）：
	- `D:/workshop/MP_power/.venv/Scripts/python.exe scripts/enrich_run_with_cpu_energy.py --run-csv artifacts/runs/<run.csv> --out artifacts/runs/<run_enriched.csv>`

- 固定流水线（一条命令串起：采样→能耗列→报告图表）：
	- `D:/workshop/MP_power/.venv/Scripts/python.exe scripts/pipeline_run.py --scenario S1 --duration 1200 --interval 2 --thermal --auto-reset-battery`
	- 输出：`artifacts/runs/<run>_enriched.csv` + `artifacts/reports/<run>_enriched/summary.md` + `timeseries.png`

- 用 time_in_state 增量估算某个 policy 对应 cluster 的 CPU 能耗（mJ）：
	- `D:/workshop/MP_power/.venv/Scripts/python.exe scripts/parse_time_in_state.py --cluster-csv artifacts/android/power_profile/cluster0_freq_power.csv --deltas-csv artifacts/runs/<run.csv> --out artifacts/runs/<run_with_energy.csv> --policy 0`
	- 说明：power_profile 提供的是 `power_ma`，脚本会用采样 CSV 的 `battery_voltage_mv` 折算 $P_{mW}=I_{mA}V_{mV}/1000$，并按 $E_{mJ}=P_{mW}\Delta t_{ms}/1000$ 得到能量。

## 5. 质量控制与注意事项

- 采样时避免系统自动更新/备份等强扰动（尽量在相同网络环境、相似后台状态下重复实验）。
- 屏幕自动亮度/高刷/省电模式应在元数据中注明（最好固定）。
- 电量百分比有量化误差与迟滞，若 charge counter 可用，优先用于短时间窗口的能量/电荷变化估计。

### 5.1 统一验收门槛（强制）

- 该 run 必须满足：`adb_error` 全为空（或剔除后剩余有效时长 ≥ 目标时长的 95%）。
- 若出现 `UPDATES STOPPED`，必须 reset 并恢复到 `battery_updates_stopped=0`，否则该段作废。
- S2 亮度阶梯：每个档位内 `brightness` 应为常数（阶跃变化只发生在切档时刻）。

## 6. 数据合规与引用

- 若使用外部公开数据/规格，需要给出来源与许可说明。
- 本项目自采数据建议附在最终提交的可复现材料中（或提供生成脚本与采样说明）。
