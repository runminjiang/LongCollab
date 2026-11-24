# 验证清单

## P2P

从项目环境执行 `python -m pytest -q`。默认测试使用脚本端点。验证覆盖 MARBLE 固定名册、DRBench 动态名册和资源授权、动作校验、附件权限、会话恢复、重复交付、预算和评分错误。单活跃测试同时请求推进两个回合，断言实际执行成员的峰值为 1。交棒测试断言顺序为 A → B → A，最后一次恢复 A 原先的会话。[调度测试](../tests/test_team.py)

在 Docker 已准备好 `tme-codex:b1` 且已检出固定版本 MARBLE 时，可执行 `python -m tests.sdk_verification.verify_codex_team`。探针用脚本端点和真实 Codex 容器完成 Research 第 2 题的五人交棒，断言上传文件的内容、实际发送顺序以及逐人 token 用量。记录中的评分来自脚本评分器。[五回合记录](../tests/sdk_verification/results/codex-marble-team.json)

交付版按 Research 三题的 9、8、6 回合以及 Work 三题的 4、5、4 回合复核。使用历史已接受回复回放时，36 次唤醒输入和 6 份正式交付均逐字节匹配。Work 两组的真实 Docker 启动和 Research 七工具 Codex 的真实派工另有中途核查记录。[逐题记录](../tests/sdk_verification/results/handoff-check.json) · [复核命令](baseline-reproduction.md)

## 成员会话

`python -m tests.sdk_verification.verify_harnesses --openai-tools` 用脚本模型验证原生 Bash、上传、恢复会话与相邻成员的隔离。`--marble-tools` 额外检查 Research 工具清单和容器的网络接口。`python -m tests.sdk_verification.verify_member_effort` 核对首次和恢复请求中的推理档位。[请求记录](../tests/sdk_verification/results/member-effort.json)

## 独立复现

在准备好同一镜像与 MARBLE 检出后，运行 `python -m tests.sdk_verification.verify_codex_native`。本地脚本模型要求真实 CLI 派工，然后核对父子会话 ID、子会话中的答复和最终交付。单代理组使用同一模型，检查实际发给模型的工具清单。[两组探针结果](../tests/sdk_verification/results/codex-native.json)

Codex 对照用 `experiments/run_codex_research.py --mode native` 开启原生子代理。核对实际执行时，检查 `run.json` 里的 `native_team`、对应子会话的答复和父会话导出的答案。`--mode single` 使用同一题目和研究工具。

MARBLE 官方运行器生成逐题记录和汇总表。`--summary_only` 从已完成的记录重算汇总，新的模型推理和官方原题运行须另外配置模型。[两种复现的运行命令](../README.md)
