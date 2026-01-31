# 已审阅的可用功耗/电池数据（ADB）

更新时间：2026-01-31

本文记录在本工作区/本机/已连接手机上，通过 ADB 已验证可访问、可提取、可用于建模/验证的功耗相关数据，以及已发现但可能受限或需要进一步解析的配置文件。

相关标准化文档：

- 连续时间模型规格：`docs/model_spec.zh-CN.md`
- 数据采集与实验协议：`docs/data_protocol.zh-CN.md`

## 1. 连接与权限结论（已验证）

- 设备连接状态：`adb devices -l` 显示为 `device`（非 `unauthorized`）。
- Shell 身份：`adb shell id` 为 `uid=2000(shell)`（标准 ADB shell，非 root）。
- 结论：多数 `dumpsys` 与部分 `/sys/devices/system/cpu/*` 可读；部分电池 sysfs 与少量 vendor 路径对 `adb pull` 有限制。

## 2. 可直接采集的“连续时间模型”观测量（推荐）

### 2.1 电池状态（观测 SOC/电压/温度等）

- 命令：`adb shell dumpsys battery`
- 已观测字段（示例）：
  - `level`/`scale`（电量百分比）
  - `voltage`（mV）
  - `temperature`（0.1°C）
  - `Charge counter`（uAh，设备支持时）

备注：曾出现 `UPDATES STOPPED`，已验证可通过 `adb shell dumpsys battery reset` 或 `adb shell cmd battery reset` 恢复。

### 2.1.1 Perfetto `android.power` 电池计数器（高频，强烈推荐优先验证）

- 结论（已在本机型验证）：无 root 条件下，Perfetto 的 `android.power` data source **可以**采到电池高频计数器轨道：
  - `batt.current_ua`（瞬时电流）
  - `batt.voltage_uv`（本机实际数值表现更像 mV；解析脚本会自动做单位推断）
  - `batt.charge_uah`（电量计数器）
  - `batt.capacity_pct`
- 优势：采样间隔可到 250ms 量级，显著缓解 `dumpsys battery` 的 `Charge counter` 低频/阶梯化问题，适合 9 分钟硬约束下做趋势对比。

仓库支持：
- 采集：`scripts/pipeline_run.py` 增加 `--perfetto-android-power`（与采样并行录制，结束后自动拉取并解析）。
- 解析：`python -m mp_power.pipeline_ops parse-perfetto-android-power --trace <trace.pftrace> --out-dir <out_dir>`（输出 timeseries CSV + summary CSV/JSON）。

### 2.2 热数据（可用于热模型/温度影响）

- 命令：`adb shell dumpsys thermalservice`
- 可读内容：CPU/GPU/SOC/BATTERY 等温度传感器值与热状态。

### 2.3 CPU 负载代理（可用于解释功耗项）

- 命令：`adb shell dumpsys cpuinfo` 或 `adb shell top -b -n 1`

### 2.4 屏幕亮度代理

- 命令：`adb shell settings get system screen_brightness`

## 3. 与 CPU 频点功耗相关的“配置表”（可读、可提取）

### 3.1 power_profile.xml（framework 资源 + overlay）

- 发现：`/system/framework/framework-res.apk` 可通过 `adb pull` 拉取并解析，但其中 `res/xml/power_profile.xml` 多为占位值（例如 0.1、capacity=1000 等），实际生效值由 overlay 覆盖。

- 已提取并解析的“生效 overlay”版本：
  - 文件：`artifacts/android/overlays/FrameworkResOverlay_power_profile_xmltree.txt`
  - 内容包含：
    - `cpu.core_speeds.cluster0/1/2`（频点列表，kHz）
    - `cpu.core_power.cluster0/1/2`（与频点一一对应的功耗/电流估计项）
    - `cpu.clusters.cores`（各簇核心数）
    - 以及 screen/wifi/bluetooth/gps/radio 等条目

重要说明：power_profile 不是“硬件实测功耗”，通常是系统用于 Batterystats/估算的参数表；更适合作为建模先验/初值与相对比较基准。

## 4. CPU 频点/驻留时间（可读，可用于把配置表变成时间序列能耗估计）

已验证以下 sysfs 可读（无需 root）：

- 实时频率：`/sys/devices/system/cpu/cpu*/cpufreq/scaling_cur_freq`
- 频点驻留统计：`/sys/devices/system/cpu/cpufreq/policy*/stats/time_in_state`

用途建议：
- 用 `time_in_state` 的增量计算每个采样窗口各频点驻留时间 $\Delta t_i$
- 结合 `cpu.core_power.clusterX[i]` 得到窗口内 CPU 估计能耗（用于解释/分解 $P(t)$，并可作为参数初值）

## 5. 已确认受限的路径（需要替代方案）

- 电池 sysfs：`/sys/class/power_supply/battery/voltage_now`、`/sys/class/power_supply/battery/temp` 读取被拒（Permission denied）。
  - 替代：用 `dumpsys battery`/`dumpsys thermalservice`。

- 部分 vendor 路径对 `adb pull` 可能被拒（failed to stat / Permission denied）。
  - 替代：可用 `adb exec-out cat <path>` 流式拉取（本仓库已有辅助脚本 `scripts/adb_exec_out_pull.py`）。

## 6. 待继续审阅的数据源（已发现）

已在设备上发现大量与 power/thermal/perf 相关的配置文件（路径来自 `find /vendor/etc /product/etc /odm/etc ...`）：

- power 相关：
  - `/vendor/etc/power_app_cfg.xml`
  - `/vendor/etc/powercontable.xml`
  - `/vendor/etc/powerscntbl.xml`
  - `/vendor/etc/nnapi_powerhal.json`
  - `/vendor/etc/init/mtkpower_applist-mtk-default.rc` 等

- perf 相关：
  - `/vendor/etc/miperf_app_cfg.xml`
  - `/vendor/etc/miperf_contable.xml`
  - `/vendor/etc/miperf_actvity_cfg.xml`
  - `/vendor/etc/miperflock_activity_cfg.xml`

- thermal 相关：
  - `/vendor/etc/thermal/thermal.conf` 及多份 `thermal_policy_*.conf`
  - `/vendor/etc/thermal-map.conf`
  - `/product/etc/displayconfig/thermal_brightness_control.xml`
  - `/odm/etc/thermal-*.conf`（按场景：navigation/video/game 等）

下一步计划：把以上配置文件批量拉取到 `artifacts/android/configs/`，做结构化审阅（字段/表格含义、是否可映射为模型状态机/参数），并把结论追加到本文。

## 7. 已拉取的配置文件（结构化审阅与可用性结论）

本节针对已保存到 `artifacts/android/configs/` 的文件做“能否定量、能否观测、如何用于连续时间模型”的审阅。

### 7.1 perf/power HAL 资源映射与场景表（可用于构建“状态机/控制输入”，但不直接给出功耗）

#### 7.1.1 `vendor__etc__powercontable.xml`（Mediatek power hint 资源字典）

- 位置：`artifacts/android/configs/vendor__etc__powercontable.xml`
- 结构：`<CMD name=... id=...>`，每条 CMD 通常包含：
  - `<Entry path=...>`：对应 sysfs 或 proc 节点（例如 `/sys/module/mtk_fpsgo/parameters/*`、`/dev/cpuctl/*`、`/sys/module/mtk_core_ctl/*`）
  - `<MinValue>/<MaxValue>/<DefaultValue>/<InitWrite>`：允许范围与初始化写入
  - `<Compare>`：约束关系（more/less）
- 可用性结论：
  - 适合用于“解释系统为何在某些场景更耗电”：它明确列出 OS/厂商能调哪些旋钮（uclamp、cpuset、fpsgo、dram opp、core_ctl 等）。
  - 不直接给出“旋钮→瓦特”的标定，因此更适合做**控制输入/约束项**，而非直接的功耗参数。

#### 7.1.2 `vendor__etc__powerscntbl.xml`（场景 powerhint → 资源写入序列）

- 位置：`artifacts/android/configs/vendor__etc__powerscntbl.xml`
- 结构：`<scenario powerhint="...">` 下有多条 `<data cmd="..." param1="...">`
- 典型场景（片段中已出现）：`LAUNCH`、`MTKPOWER_HINT_APP_TOUCH`、`MTKPOWER_HINT_*` 等
- 可用性结论：
  - 非常适合把手机功耗模型做成**混合系统/分段连续模型**：
    - 事件（启动/触控/切换 Activity）触发 powerhint
    - powerhint 将若干 sysfs 参数（最小频率、uclamp、dram opp、gpu freq min 等）在一段 hold time 内推到更激进的性能区
    - 结果是 $P_{cpu}(t), P_{gpu}(t)$ 的参数在一段时间内发生跃迁
  - 仍然不提供“每个设置对应多少 mW”的标定，建议与 `time_in_state`/`power_profile` 组合使用：
    - powerhint 解释“为什么频率驻留会变化”
    - time_in_state + power_profile 提供“频率驻留→估计能耗”

#### 7.1.3 `vendor__etc__miperf_contable.xml`（另一套 perf 资源 → 节点映射，偏示例/跨平台痕迹）

- 位置：`artifacts/android/configs/vendor__etc__miperf_contable.xml`
- 结构：`<CMD name=...><Entry path=...>`，多为 cpuset、sched、GPU devfreq 等路径
- 观察：文件里出现 `msm_performance` 与 `kgsl` 路径（更像 Qualcomm 体系），与本机型的 Mediatek 生态不完全一致。
- 可用性结论：
  - 可作为“厂商 perf 服务可能在改哪些内核节点”的参考。
  - 不建议把其路径/参数当作本机可用的定量依据；若用于报告，更适合作为**机制示例**而非“本机实测”。

#### 7.1.4 `vendor__etc__miperf_*_cfg.xml`、`vendor__etc__miperflock_activity_cfg.xml`（应用/Activity → boost 列表）

- 位置：
  - `artifacts/android/configs/vendor__etc__miperf_app_cfg.xml`
  - `artifacts/android/configs/vendor__etc__miperf_actvity_cfg.xml`
  - `artifacts/android/configs/vendor__etc__miperflock_activity_cfg.xml`
- 结构：以包名/Activity 为键，给出一组 PERF_RES 指令（如 `PERF_RES_MIN_FREQ_CPU`、`PERF_RES_SCHED_BOOST`、`PERF_RES_BOOSTLIST`、`duration`）
- 可用性结论：
  - 可用来构建“后台/前台/特定应用触发的性能模式”这一控制输入 $u(t)$。
  - 不提供功耗标定；建议把它当作“模式切换的解释依据”。

#### 7.1.5 `vendor__etc__nnapi_powerhal.json`（AI/NN 推理场景的 power hint 配置）

- 位置：`artifacts/android/configs/vendor__etc__nnapi_powerhal.json`
- 结构：按 workload 分类（如 `fast_single_answer`、`sustained_speed`、`compilation`），每类给出一组 `{name,id,value}` 的 perf 资源设置
- 可用性结论：
  - 可用于定义“AI 推理/编译”时的功耗模式（更高的 uclamp、dram opp、perf mode 等）。
  - 同样不提供直接功耗；用于“解释为什么此类负载更耗电/更热”很有价值。

### 7.2 可直接用于模型耦合/约束的热-亮度配置（可定量、强可解释）

#### 7.2.1 `product__etc__displayconfig__thermal_brightness_control.xml` 与 `common_multi_factor_thermal_brightness_control.xml`

- 位置：
  - `artifacts/android/configs/product__etc__displayconfig__thermal_brightness_control.xml`
  - `artifacts/android/configs/product__etc__displayconfig__common_multi_factor_thermal_brightness_control.xml`
- 结构：按 `thermal-condition-item`（如 Default、TGAME、NAVIGATION 等）给出温度区间与 `nit` 上限
- 可用性结论：
  - 这是“温度影响续航”的一个直接链路：温度上升 → 系统降低允许亮度上限 → 屏幕功耗降低（用户观感改变）。
  - 在建模时可把它写成约束：$\text{nit}(t) \le f_{mode}(T(t))$，从而让 $P_{screen}(t)$ 自动随温度受限。

### 7.3 应用白名单 power 配置（解释/归因很强，但主要是策略而非功耗标定）

#### 7.3.1 `vendor__etc__power_app_cfg.xml`

- 位置：`artifacts/android/configs/vendor__etc__power_app_cfg.xml`
- 结构：`<WHITELIST>` 下按 `Package/Activity/FPS/WINDOW` 分层，内部是大量 `<data cmd=... param1=...>`
- 可用性结论：
  - 体现“为何同一部手机在不同应用/帧率目标下耗电差异巨大”：某些应用被强制启用 fpsgo/boost/uclamp/DRAM/GPU 限制或提升。
  - 适合用于报告中的“驱动因素识别”：例如游戏/高帧率场景被配置了更高的 freq 上限、较激进的调度/热感知阈值。
  - 不建议把其中 param1 直接解释为功耗（它们多为阈值、频率上限、开关量），应当与实际采样（频率驻留、温度、电池电流/电量变化）闭环验证。

### 7.4 目前不可直接解析的 thermal 配置（格式不明/疑似二进制或加扰）

以下文件已成功拉取，但经嗅探不属于 ZIP/GZIP，且内容非明文配置，短期内难以直接转化为“参数表”：

- `artifacts/android/configs/vendor__etc__thermal-map.conf`（二进制头，非文本）
- `artifacts/android/configs/vendor__etc__thermal__thermal.conf`（文本但明显加扰/不可读）
- `artifacts/android/configs/vendor__etc__thermal__thermal_policy_00.conf`（文本但明显加扰/不可读）
- `artifacts/android/configs/odm__etc__thermal-navigation.conf`（二进制/不可读）
- `artifacts/android/configs/odm__etc__thermal-video.conf`（二进制/不可读）

可用性结论：
- 这些文件更像厂商 thermal daemon 的私有输入格式；在不引入厂商工具链/源码的情况下，不建议强行解码。
- 建模与验证上，可用 `dumpsys thermalservice` 的实时温度/热状态替代，且与题目要求（连续时间机理模型）更贴近。

## 8. “可以定量用”的数据 vs “只能解释用”的数据（汇总）

### 8.1 可定量（能进方程/能算数）

- `dumpsys battery`：电量百分比、电压、温度、charge counter（若支持）
- `dumpsys thermalservice`：多路温度传感器
- `/sys/devices/system/cpu/cpufreq/*/stats/time_in_state`：频点驻留时间
- `FrameworkResOverlay_power_profile_xmltree.txt`：频点列表 + 对应功耗/电流估计（先验/初值）
- displayconfig 的 thermal-brightness：温度→亮度上限（可作为约束函数）

### 8.2 解释/状态机（能解释差异与驱动因素，但不直接给瓦特）

- `powerscntbl.xml`：事件/场景触发的性能策略（对 $P(t)$ 形成可解释的跃迁）
- `powercontable.xml`：系统有哪些“旋钮”（uclamp/cpuset/fpsgo/dram opp/core_ctl 等）
- `power_app_cfg.xml`：应用白名单策略导致的功耗差异
- `nnapi_powerhal.json`、`miperf_*`：特定负载/Activity 的 boost 机制

## 9. 对后续建模的直接建议（把配置文件用在“连续时间机理模型”里）

- 把 `power_profile` + `time_in_state` 用作 CPU 子系统的“可解释功耗估计器”：
  - 频点驻留提供 $\Delta t_i$，配置表提供每频点的相对功耗权重，得到 $E_{cpu}$ 的估计（用于分解总功耗、做参数初值与敏感性）
- 把 `powerscntbl.xml`、`power_app_cfg.xml` 用作“模式切换解释”：
  - 定义离散模式 $m(t)$（例如 LAUNCH/TOUCH/游戏/导航/视频），模式改变导致 $P_{cpu}$、$P_{gpu}$、$P_{radio}$ 的参数切换
- 把 displayconfig 的 thermal brightness 用作“热→亮度→功耗”的耦合约束，使模型能解释过热后续航与亮度变化
- 对不可读 thermal conf：不作为数据源；改用 `dumpsys thermalservice` 的实时观测做验证与不确定性分析

## 10. 非 root “策略/调度事件”如何做到不揣测（新增）

本节目标：尽量找到“系统直接暴露的策略/模式/事件”，而不是只靠推断。

### 10.1 先探测：是否存在可 `dumpsys` 的厂商/策略服务

仓库提供了一键探测脚本：

- 脚本：`policy/probe_policy_interfaces.py`
- 输出：`artifacts/android/policy_probe/<timestamp>/`（保存 `dumpsys -l`、`service list`、`cmd -l`、候选服务的 `dumpsys <name>` 等）

用法：

- `python policy/probe_policy_interfaces.py`
- 若有多设备/无线调试：`python policy/probe_policy_interfaces.py --serial <serial>`

说明：

- 若 ROM 暴露了 `dumpsys <vendor_service>`（例如 power/perf/mtk/mi 相关服务），该输出通常能给出比“旋钮推断”更直接的模式/策略信息。
- 若服务不允许访问，脚本会记录失败原因，但不终止整体探测。

### 10.2 用 Perfetto 抓“策略事件”（可选，非 root）

若你希望看到更接近“策略切换时刻”的证据链，可以让 pipeline 录制包含 `linux.ftrace` + atrace 类别的 Perfetto trace，然后从 trace 的 slices 中提取可能的策略/PowerHAL markers：

- 采集开关：`scripts/pipeline_run.py --perfetto-policy-trace`
- 解析输出（在 report 目录）：
  - `perfetto_policy_markers.csv`
  - `perfetto_policy_markers_summary.json`

示例：

- `python scripts/pipeline_run.py --scenario S2_b90 --duration 540 --interval 2 --perfetto-android-power --perfetto-policy-trace`

备注：

- 是否能抓到“准确策略名”取决于 ROM 是否在 atrace/ftrace 中埋点（有则直接看到；无则只能看到频率/idle/sched 的效果链）。
