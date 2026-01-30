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

### 2.4 CPU 频率与驻留（强推荐，利于解释功耗项）

- 实时频率：`/sys/devices/system/cpu/cpu*/cpufreq/scaling_cur_freq`
- 驻留统计：`/sys/devices/system/cpu/cpufreq/policy*/stats/time_in_state`

用途：与 power_profile（overlay）中的 cpu.core_power.clusterX 结合，得到 CPU 子系统能耗的解释性估计（用于分解 P(t)、给参数先验）。

## 3. 实验场景设计（用于估计不同功耗项）

每组建议 20–40 分钟，记录开始/结束时刻与中途任何切换。

- S1 待机基线：屏幕关，分别在（飞行模式 / Wi‑Fi / 蜂窝）条件下测 P_base 与网络尾耗。
- S2 屏幕功耗：固定其他条件，亮度设为 5 个档位（如 20/60/120/180/240），估计 P_screen(b)。
- S3 CPU 负载：运行固定负载（例如本地 benchmark/视频解码），估计 P_cpu(u)。
- S4 导航/GPS：固定亮度与网络，启用导航，估计 P_gps + 网络项。

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

- 自动推断 cpufreq policy → power_profile cluster 映射（用于后续批量算能耗列）：
	- `D:/workshop/MP_power/.venv/Scripts/python.exe scripts/map_policy_to_cluster.py --serial <serial>`
	- 产物输出到：`artifacts/android/power_profile/policy_cluster_map.json`

- 用 time_in_state 增量估算某个 policy 对应 cluster 的 CPU 能耗（mJ）：
	- `D:/workshop/MP_power/.venv/Scripts/python.exe scripts/parse_time_in_state.py --cluster-csv artifacts/android/power_profile/cluster0_freq_power.csv --deltas-csv artifacts/runs/<run.csv> --out artifacts/runs/<run_with_energy.csv> --policy 0`
	- 说明：power_profile 提供的是 `power_ma`，脚本会用采样 CSV 的 `battery_voltage_mv` 折算 $P_{mW}=I_{mA}V_{mV}/1000$，并按 $E_{mJ}=P_{mW}\Delta t_{ms}/1000$ 得到能量。

## 5. 质量控制与注意事项

- 采样时避免系统自动更新/备份等强扰动（尽量在相同网络环境、相似后台状态下重复实验）。
- 屏幕自动亮度/高刷/省电模式应在元数据中注明（最好固定）。
- 电量百分比有量化误差与迟滞，若 charge counter 可用，优先用于短时间窗口的能量/电荷变化估计。

## 6. 数据合规与引用

- 若使用外部公开数据/规格，需要给出来源与许可说明。
- 本项目自采数据建议附在最终提交的可复现材料中（或提供生成脚本与采样说明）。
