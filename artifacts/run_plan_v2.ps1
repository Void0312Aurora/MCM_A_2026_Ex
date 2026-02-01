# GPS关重复测量（建议全程亮屏+解锁停留在同一页面；尽量先于CPU负载，降低温度混杂）
# plan_id=S4-REPEAT
# repeat 1/2
python scripts/pipeline_run.py --scenario S4 --duration 540 --interval 2 --thermal --display --qc

# GPS关重复测量（建议全程亮屏+解锁停留在同一页面；尽量先于CPU负载，降低温度混杂）
# plan_id=S4-REPEAT
# repeat 2/2
python scripts/pipeline_run.py --scenario S4 --duration 540 --interval 2 --thermal --display --qc

# GPS开重复测量（同上）
# plan_id=S4-REPEAT
# repeat 1/2
python scripts/pipeline_run.py --scenario S4-1 --duration 540 --interval 2 --thermal --display --qc

# GPS开重复测量（同上）
# plan_id=S4-REPEAT
# repeat 2/2
python scripts/pipeline_run.py --scenario S4-1 --duration 540 --interval 2 --thermal --display --qc

# CPU负载梯度：threads=1（建议全程亮屏+解锁保持同一页面）
# plan_id=S3-GRAD
python scripts/pipeline_run.py --scenario S3_load_t1 --duration 540 --interval 2 --thermal --display --qc --cpu-load-threads 1

# CPU负载梯度：threads=2（同上）
# plan_id=S3-GRAD
python scripts/pipeline_run.py --scenario S3_load_t2 --duration 540 --interval 2 --thermal --display --qc --cpu-load-threads 2

# CPU负载梯度：threads=4（已有，可重复；同上）
# plan_id=S3-GRAD
python scripts/pipeline_run.py --scenario S3_load_t4 --duration 540 --interval 2 --thermal --display --qc --cpu-load-threads 4

# CPU负载梯度：threads=6（同上）
# plan_id=S3-GRAD
python scripts/pipeline_run.py --scenario S3_load_t6 --duration 540 --interval 2 --thermal --display --qc --cpu-load-threads 6

# CPU负载梯度：threads=8（同上）
# plan_id=S3-GRAD
python scripts/pipeline_run.py --scenario S3_load_t8 --duration 540 --interval 2 --thermal --display --qc --cpu-load-threads 8

# S2新增低亮度点；建议手动关闭自动亮度；保持同一静态页面（结束后自动恢复设置避免影响后续）
# plan_id=S2-LOW
python scripts/pipeline_run.py --scenario S2_b005 --duration 540 --interval 2 --thermal --display --qc --enable-write-settings --set-brightness 5 --set-timeout-ms 2147483647 --auto-reset-settings

# S2新增低亮度点（结束后自动恢复设置避免影响后续）
# plan_id=S2-LOW
python scripts/pipeline_run.py --scenario S2_b010 --duration 540 --interval 2 --thermal --display --qc --enable-write-settings --set-brightness 10 --set-timeout-ms 2147483647 --auto-reset-settings

# S2新增中亮度点（尝试拉开与90/150/210差异；结束后自动恢复设置）
# plan_id=S2-MID
python scripts/pipeline_run.py --scenario S2_b060 --duration 540 --interval 2 --thermal --display --qc --enable-write-settings --set-brightness 60 --set-timeout-ms 2147483647 --auto-reset-settings

# S2新增高亮度端点（结束后自动恢复设置）
# plan_id=S2-HIGH
python scripts/pipeline_run.py --scenario S2_b255 --duration 540 --interval 2 --thermal --display --qc --enable-write-settings --set-brightness 255 --set-timeout-ms 2147483647 --auto-reset-settings

# 重复测量评估短窗方差（亮度映射非线性敏感；结束后自动恢复设置）
# plan_id=S2-REPEAT
# repeat 1/2
python scripts/pipeline_run.py --scenario S2_b30_1 --duration 540 --interval 2 --thermal --display --qc --enable-write-settings --set-brightness 30 --set-timeout-ms 2147483647 --auto-reset-settings

# 重复测量评估短窗方差（亮度映射非线性敏感；结束后自动恢复设置）
# plan_id=S2-REPEAT
# repeat 2/2
python scripts/pipeline_run.py --scenario S2_b30_1 --duration 540 --interval 2 --thermal --display --qc --enable-write-settings --set-brightness 30 --set-timeout-ms 2147483647 --auto-reset-settings
