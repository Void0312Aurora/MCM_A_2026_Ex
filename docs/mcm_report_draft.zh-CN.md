# MCM Problem A 报告草稿（工作版）

更新时间：2026-02-03

> 目标：在“后续不再补测”的约束下，给出可复现、可解释的连续时间模型，并用现有数据完成必要的校准与证据链。

---

## 1. 执行摘要（可直接放进最终稿）

我们构建了一个**显式连续时间**的电池 SOC 模型：

\[
\frac{dSOC}{dt} = -\frac{P_{total}(t)}{V_{eff}(SOC,T)\,C_{eff}}
\]

其中总功耗采用可解释的分解结构，并针对“同名场景功耗差异巨大”的现象进行了数据质量控制（QC）与协变量分析。证据表明：差异主要来自**起始 SOC/电压/温度**等初始状态差异，而非实验不可复现。

在不增加新测试的前提下，我们引入一个更物理、更可解释的修正项：**电池内阻损耗（Joule heating）**

\[
P_{loss}(t)=I(t)^2\,R_{int}(SOC,T)
\]

并使用 **LOSO（leave-one-scenario-out）** 在现有数据上验证其泛化效果：在 QC 通过的 20 条 run 上，run 级平均功耗误差（MAE）从 194 mW 降到 135 mW，同时“残差-CPU 起始温度”的相关性显著降低（0.62 → 0.15）。这说明内阻项能稳定吸收一部分此前被当作“热/隐藏状态”的系统性误差，且满足题目对连续时间物理建模的要求。

---

## 2. 数据与口径（简述）

- 主功耗观测：Perfetto `android.power` 汇总到 run 级均值功耗（mW）。
- 协变量：run 起始 SOC（%）、run 起始 CPU 温度（°C）、run 均值电流（µA）、均值电压（V）等。
- QC：剔除 perfetto 缺失、设备状态异常等 run，保留 `qc_keep=1` 的样本用于评估。

相关产物：
- QC 总表：[artifacts/qc/qc_run_summary.csv](../artifacts/qc/qc_run_summary.csv)

---

## 3. 为什么需要内阻项（现象到机理）

在 CPU 梯度等高负载场景中，平均电流显著上升。
若只用“子系统功耗分解”的理想化结构，往往会低估**随电流平方增长**的损耗（例如电池内阻、连接器/导线等效电阻造成的 I²R 热耗散）。
这会导致：

- 高负载 run 的功耗系统性低估（正残差）。
- 残差与电流、温度、SOC/电压等协变量出现相关（因为内阻随温度与 SOC/电压状态变化）。

因此将 I²R 作为一个**只增加功耗**的可解释项加入，是“补齐物理缺失项”的自然选择。

---

## 4. 内阻项形式与约束（可直接放进方法部分）

我们采用参数化内阻模型（非负约束），并将其作为功耗分解中的附加项：

\[
R_{int}(SOC,T)=R_0 + R_1(1-SOC) + R_2\max(0, T_{cpu}-T_{ref})
\]

其中 $R_0,R_1,R_2\ge 0$，$T_{ref}=40\,^{\circ}C$。

同时，为避免主模型已部分吸收 I²R 而造成双计数，我们在每个 LOSO fold 上额外拟合一个非负缩放 $s\ge 0$：

\[
P_{loss}(t)\leftarrow s\cdot I(t)^2R_{int}(SOC,T)
\]

这仍属于物理结构内的“校准”，而不是用回归替代 ODE。

---

## 5. 验证设计（LOSO，防止泄漏）

- Split：按场景留出（leave-one-scenario-out）。
- 训练：用其余场景拟合 $R_{int}$（以及每 fold 的 $s$）。
- 测试：把拟合得到的 I²R 损耗项加回到被留出场景的功耗预测中，并比较误差。

最终冻结的结果与数据包：
- 指标与相关性摘要：[artifacts/reports/final_i2r/i2r_correction.md](../artifacts/reports/final_i2r/i2r_correction.md)
- run 级明细：[artifacts/reports/final_i2r/run_level_i2r_correction.csv](../artifacts/reports/final_i2r/run_level_i2r_correction.csv)

---

## 6. 结果（可直接粘贴到结果部分）

在 `qc_keep=1` 的 20 条 run 上：

- MAE：194.23 → 134.68 mW
- RMSE：283.42 → 211.03 mW
- bias：+42.40 → -53.69 mW

残差相关（越接近 0 越好）：

- 残差 vs `thermal_cpu0_C`：0.62 → 0.15
- 残差 vs `perfetto_current_mean_uA`：0.89 → 0.49

关键图（报告可用）：
- [artifacts/reports/final_i2r/figures/residual_vs_tcpu0.png](../artifacts/reports/final_i2r/figures/residual_vs_tcpu0.png)
- [artifacts/reports/final_i2r/figures/residual_vs_current.png](../artifacts/reports/final_i2r/figures/residual_vs_current.png)
- [artifacts/reports/final_i2r/figures/per_scenario_mae.png](../artifacts/reports/final_i2r/figures/per_scenario_mae.png)

---

## 7. 讨论与局限

- 内阻项显著改善高负载场景误差，且降低了残差对起始温度/电流的相关性，支持其“物理缺失项”的解释。
- 仍存在一定负偏差（bias < 0），提示主模型其它项（例如 CPU 子系统、背景基线等）仍可能存在轻微过估或 I²R 双计数未完全消除。
- 对“泄漏/后台基线”类项：仅用 run 级均值协变量做 LOSO 并不稳定，容易在 holdout 场景过矫正；因此本次交付优先保留更可辨识、更物理的 I²R 项。

---

## 8. 下一步（如需完善最终交付）

- 将 I²R 项正式并入 v2 的连续时间功耗分解与 SOC ODE 训练流程，重新输出一套统一的参数与评估结果。
- 把 QC 门禁、协变量分析与 I²R 的证据链整合为最终 MCM 报告的“方法-验证-讨论”三段，形成可复现的交付包。
