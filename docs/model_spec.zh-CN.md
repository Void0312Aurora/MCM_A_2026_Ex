# 连续时间电池 SOC 模型规格（v0）

更新时间：2026-01-30

目标：给出可复现、可解释的连续时间模型，用于预测智能手机电池的 SOC(t) 与空电时间（Time-to-Empty, TTE）。

## 1. 范围与建模原则

- 必须是显式连续时间描述（ODE/DAE/混合系统均可），满足题目“基于物理/机理原理”的要求。
- 数据用于参数估计与验证，不用离散曲线拟合替代连续模型。
- 优先从最小可用模型开始，逐步扩展屏幕/CPU/网络/GPS/后台与温度。

## 2. 状态、输入与输出

### 2.1 状态变量（states）

- SOC(t) ∈ [0, 1]：电池荷电状态。
- 可选扩展（用于温度影响与热降额）：T(t)（电池或机身/皮肤温度，单位 °C）。

### 2.2 外部输入/驱动（inputs）

- b(t)：屏幕亮度代理（0–255 或映射到 nit）。
- u_cpu(t)：CPU 负载代理（例如占用率/频点驻留统计/前台模式）。
- s_net(t)：网络模式/连接状态（Wi‑Fi/蜂窝/弱信号，或状态机模式）。
- s_gps(t)：GPS 是否启用/导航模式指示。
- n_bg(t)：后台活动强度代理（可用“基线+扰动”建模）。
- T_amb(t)：环境温度（如可获得）。

### 2.3 输出（outputs）

- TTE：从当前时刻 t0 开始，满足 SOC(t*) = SOC_min 的首次到达时间 t*（空电时间）。

## 3. 核心控制方程（v0 最小模型）

### 3.1 电荷守恒（SOC 动力学）

推荐用功率驱动 + 电压函数：

- SOC 方程：

  dSOC/dt = - P_total(t) / ( V_eff(SOC, T) * C_eff(T, age) )

其中：
- P_total(t) 为手机端从电池汲取的总功率（W）。
- V_eff(SOC, T) 为等效端电压（V），可用简化开路电压曲线或常值近似。
- C_eff(T, age) 为有效容量（Ah 或 Coulomb），包含温度与老化影响。

### 3.2 总功耗分解（解释性结构）

P_total(t) = P_base
           + P_screen(b(t))
           + P_cpu(u_cpu(t))
           + P_net(s_net(t))
           + P_gps(s_gps(t))
           + P_bg(n_bg(t))
           + eps(t)

备注：这不是黑箱回归，而是“机理可解释的加和结构”，各项可用分段线性/幂律/状态机实现。

### 3.3 关机阈值与可行域

- SOC_min：关机阈值（可取 0 或设备实测阈值）。
- 强制约束：SOC(t) 不小于 0；若数值积分出现越界，需裁剪并记录。

## 4. 温度与老化扩展（v1）

### 4.1 有效容量与内阻影响

- C_eff(T, age) = C0 * eta_T(T) * eta_age
- 可选：R(T, age) 上升导致 V_eff 下降，低 SOC 时更易触发低压关机

### 4.2 简化热模型（可选）

dT/dt = ( P_diss(t) - h*(T - T_amb) ) / C_th

并允许热约束影响输入：例如温度越高，系统限制亮度/频率上限（见采集到的 display thermal brightness 配置）。

## 5. 参数、单位与可估计性

建议明确每个参数的单位与估计来源：

- C0：标称容量（Ah），先验来自 power_profile 或规格（但需注明可能偏差）。
- P_base：待机基线功耗（W），可通过“屏幕关+固定网络状态”的实验估计。
- P_screen：随亮度变化的功耗曲线参数。
- P_cpu：CPU 子系统缩放参数（可结合 time_in_state + power_profile 形成先验）。
- 其他子系统：Wi‑Fi/蜂窝/GPS 可先用常值+状态机项。

## 6. 校准与验证接口（与数据协议对齐）

- 采样到的：level(%), voltage(mV), temperature(0.1°C), charge_counter(uAh)（如支持）
- 通过数值积分得到 SOC_hat(t)，与观测 SOC_obs(t) 对齐后估计参数。

## 7. 版本迭代建议

- v0：SOC + 分解功耗加和（最少参数，先跑通 TTE）
- v1：加入温度与亮度热降额约束（更贴近真实）
- v2：网络状态机（RRC 尾能耗）与后台事件模型（脉冲/泊松）

## 8. 代码实现映射（当前仓库）

本仓库的 v2 实现将“热效应”作为泄漏功耗（leakage）的结构化特征输入，而不是直接做复杂的功率-温度黑箱回归。

### 8.1 热模型：1-state vs 2-state

- 1-state：用一阶模型在每个 run 内拟合并模拟 `temp_cpu_hat_C`（仅用 CPU 温度观测）。
- 2-state：引入快/慢两个热状态（CPU 与电池/机身慢态），在每个 run 内同时利用 `temperature_cpu_C` 与 `temperature_C` 拟合，并输出：
  - `temp_cpu_hat_C`：快态（更贴近 die/CPU）
  - `temp_batt_hat_C`：慢态（更贴近 body/battery）
  - `temp_leak_hat_C`：用于 leakage 特征的温度（CPU/慢态的凸组合）

### 8.2 如何运行与对比

- 训练/拟合：
  - `python scripts/model_battery_soc_v2_thermal1.py --thermal-model 1state`
  - `python scripts/model_battery_soc_v2_thermal1.py --thermal-model 2state --leak-temp-mix-cpu 1.0`
- 评估（默认 S2 holdout；要看 S3 梯度建议用 `--eval loso`）：
  - `python scripts/model_eval_ood_v2.py --thermal-model 1state --eval loso`
  - `python scripts/model_eval_ood_v2.py --thermal-model 2state --leak-temp-mix-cpu 1.0 --eval loso`

输出文件会按 thermal model 自动加后缀，便于并排比较，例如：
- `artifacts/models/model_params_v2.json` vs `artifacts/models/model_params_v2_2state.json`
- `artifacts/models/model_samples_v2.csv` vs `artifacts/models/model_samples_v2_2state.csv`

### 8.3 协变量调整与残差矫正（用于“不可控起始状态”的鲁棒校准）

题目 A 的核心要求是“显式连续时间模型”。因此：

- **SOC 动力学（ODE）与功耗分解结构必须仍是主模型**。
- 所谓“协变量调整/残差矫正”只能作为**校准步骤**：用于吸收未建模或不可观测的隐藏状态（例如后台基线、传感器温度代理误差），而不是用离散回归替代 ODE。

我们在现有数据上观察到：同名场景的 `perfetto_power_mean_mW` 会因起始 SOC/电压/温度不同而出现显著差异。
为保证“公平比较”和“更稳定的参数估计”，采用两类操作：

1) **协变量调整（ANCOVA 视角）**：把“场景效应”和“起始状态效应”分离。
   - 形式上是：\(\bar P \approx \alpha_{scenario} + \beta^T z_0\)，其中 \(z_0\) 是起始协变量（SOC0、电压 V0、CPU/电池温度）。
   - 输出用于解释：为何 raw 均值不具可比性，以及哪些场景受起始状态影响最大。
   - 代码与产物：
     - `python scripts/scenario_covariate_adjustment.py`
     - `artifacts/qc/cov_adj/scenario_covariate_adjusted.*`

2) **残差矫正（hybrid 校准层）**：在不改变 SOC ODE 的前提下，对功耗分解中的“基线/泄漏项”做状态依赖校正。
   - 先有连续时间主模型：\(\dot{SOC} = - P_{model}(t) / (V_{eff} C_{eff})\)。
   - 再允许一个小的、可解释的校正项进入功耗分解：

     \[ P_{total}(t) = P_{model}(t) + \Delta P(z_0) \]

     其中 \(z_0\) 只包含 **t0 时刻可观测的起始状态**（例如 SOC0、V0、Tcpu0、Tbatt0）。
     这可以解释为“后台基线强度/有效内阻/隐藏热状态”的代理，属于参数校准而非黑箱预测。
   - 为避免数据泄漏，评估时用 LOSO（leave-one-scenario-out）在“其余场景”上拟合 \(\Delta P\)，再应用到被留出的场景。
   - 代码与产物：
     - `python scripts/residual_correction_loso.py`
     - `artifacts/qc/resid_correction/residual_correction.md`

注意：写作时应明确说明“hybrid 校准层”的输入不包含未来信息（不使用 run 中后段观测），并且主结论仍基于 ODE 模型给出 TTE（空电时间）预测。

### 8.4 内阻损耗项（I²R）：本次交付的优先物理修正

在现有数据中观察到：高负载场景下，残差与电流（以及温度）存在显著相关，说明仅靠子系统分解可能遗漏了随电流平方增长的损耗。

因此引入一个可解释的物理项：

\[ P_{loss}(t)=I(t)^2\,R_{int}(SOC,T) \]

并采用简单的参数化内阻：

\[ R_{int}(SOC,T)=R_0 + R_1(1-SOC) + R_2\max(0, T_{cpu}-T_{ref}) \]

其中所有系数约束为非负，且可选在每个 LOSO fold 上拟合非负缩放因子以避免双计数。

我们已用 LOSO（按场景留出）在现有数据上验证该项具备稳定收益，并将其作为本次交付的优先物理修正项。
产物与图表汇总见：
- `artifacts/reports/final_i2r/`
