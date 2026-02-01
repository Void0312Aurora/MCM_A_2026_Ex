# 扩充测试矩阵（基于亮度非线性 + 需要可识别参数）

## 背景：为什么 S2 的留出看起来“很容易”
你指出得很关键：`--set-brightness` 的数值（0–255）与手机亮度滑条/实际亮度（luminance）之间通常是 **非线性映射**（常见为 gamma/分段 LUT/OEM 定制）。

因此在当前数据中：
- `30` 已接近滑条中位；
- `90/150/210` 在滑条上更靠近高端，实际显示/背光条件更接近；

这会导致：在 `90/150/210` 之间做“留出某一档”更像是 **在一个很窄的工作区间内插值**，误差自然偏小。

## 扩充原则（让模型真正经受住验证）
1) **覆盖更广的亮度工作区间**：增加低端与高端点，避免所有样本挤在“高亮相近区”。
2) **同一条件重复测量**：在 540s 短窗下量化方差（尤其对低亮度更敏感）。
3) **负载做梯度而不是二元**：S3 CPU 负载用 threads 梯度（1/2/4/6/8）使 `P_cpu → T` 与 `T → leak` 关系可识别。
4) **A/B 用配对设计**：像 S4 vs S4-1 一样，用对照识别 offset（GPS / cellular 等），减少被其它项吸收。

## 建议新增矩阵（可直接执行）
见 [configs/test_plan_v2.csv](../configs/test_plan_v2.csv)。核心新增：
- S2：新增 `5/10/60/255`，并对 `30` 做重复。
- S3：新增 `threads=1/2/6/8` 梯度（`4` 可重复）。
- S4：GPS on/off 各做重复以估计短窗方差。

## 执行注意事项（降低混杂）
- 关闭自动亮度；固定同一静态页面；避免 UI 动画。
- 建议全程保持 **亮屏 + 已解锁**，并停留在同一静态页面（避免因“熄屏/唤醒回到锁屏界面”引入状态切换）。
- 若使用 `--set-brightness/--set-timeout-ms`（S2），建议配合 `--auto-reset-settings`，避免“上一亮度遗留”影响后续场景。
- 若要更干净地识别 GPS A/B（S4 vs S4-1），建议将 GPS 场景放在 CPU 满载梯度之前执行，减少温度/系统状态漂移。
- 每次 run 之间留出冷却/回温时间（或至少记录起始温度）。
- 随机化顺序：不要总是从低亮到高亮（避免温度漂移带来系统性偏差）。

## 一键生成命令
运行：
- `python scripts/generate_run_plan.py --plan configs/test_plan_v2.csv --python python`
或指定虚拟环境：
- `python scripts/generate_run_plan.py --python D:/workshop/MP_power/.venv/Scripts/python.exe`

也可以直接写入脚本文件（Windows PowerShell 友好编码）：
- `python scripts/generate_run_plan.py --plan configs/test_plan_v2.csv --out artifacts/run_plan_v2.ps1`

输出即为一组可复制执行的 `python scripts/pipeline_run.py ...` 命令。
