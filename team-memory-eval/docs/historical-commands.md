# 五组基线的 15 条逐题运行命令

每题的生成命令对应结果表的一行。MARBLE Research 的三组均使用七项研究工具和工作模型 `gpt-5.6-luna`，各跑第 1 至 3 题。Work 的两组均使用 `gpt-6-luna`，各跑 DR0001 至 DR0003。五组均用 `gpt-5.6-terra` 评分。生成命令会调用真实执行器。每条注释给出历史结果，便于和新运行的中间行为对照。Research 的分数满分 5，Work 的数值为 DRBench 的 `insights_recall`。[结果 CSV](../experiments/baseline-results.csv) · [原始评分文件位置及摘要](../experiments/history/index.json)

## 共用准备

在交付提交的仓库根目录打开 Bash。先准备选定实验需要的固定版本上游、Docker 镜像、依赖和登录。MARBLE 官方从上游根目录运行。Work 两组都由本项目启动上游 DRBench Docker App。历史原生 Codex 用宿主 CLI `0.166.0`，Research 原生还需文件版 ChatGPT 登录。成员服务用 `OPENAI_BASE_URL` 和 `OPENAI_API_KEY`，评委用 `TME_JUDGE_BASE_URL` 和 `TME_JUDGE_API_KEY`，Work 还需 `SERPER_API_KEY` 和 embedding 服务。[环境准备、镜像 ID 与模型条件](baseline-reproduction.md#2-准备上游环境和历史配置)

```bash
REPO=$PWD
PY="$HOME/miniconda3/envs/team-memory-eval/bin/python"
H="$REPO/experiments/history"
C="$H/cases"
O=$(mktemp -d /tmp/tme-history-XXXXXXXX)
export TME_REFERENCE_REPOS="${TME_REFERENCE_REPOS:-$REPO/experiments/upstream}"
export TME_BENCHMARK_DEPS="${TME_BENCHMARK_DEPS:-$REPO/experiments/benchmark-deps}"
export PYTHONPATH="$TME_BENCHMARK_DEPS${PYTHONPATH:+:$PYTHONPATH}"
export CUDA_VISIBLE_DEVICES="" LITELLM_LOCAL_MODEL_COST_MAP=True
export OPENAI_API_BASE="${OPENAI_BASE_URL:-}"
MARBLE="$TME_REFERENCE_REPOS/MARBLE"
WCFG="$C/drbench-p2p-gpt-6-luna"
P56="$C/marble-p2p-gpt-5.6-luna"
bash "$H/prepare-source.sh" work "$O/work"
bash "$H/prepare-source.sh" seven "$O/seven"
```

这两份临时源码只用当前 Git 检出的文件组装。运行输出也写入 `$O`，重跑时重新执行准备段即可。下文的 `settings.json` 是运行器可直接读取的单题配置。P2P 成员和评委从上述环境变量取凭据。Work 共用同题模型、预算和建队配置，命令显式选择 runner。原生组独立决定如何派工。

## 当时的批次入口

以下保留原始工作目录、模型和题号范围。为便于对照，Python 路径统一写为 `"$PY"`。历史的 `*.local.yaml` 随批次改写，逐题命令直接读取对应题目的配置。原始上游命令的 `--overwrite` 会覆盖同名输出。实际复跑请选下方写入新目录的单题命令。

```bash
# 交付项目根目录。P2P 的 shell 记录使用当时的本地配置。
"$PY" run.py --config experiments/marble-api.local.yaml

# 交付项目根目录。Work 启动脚本分别执行这两条子进程命令。
"$PY" -u run.py --config experiments/drbench-api.local.yaml --runner tme
"$PY" -u run.py --config experiments/drbench-api.local.yaml --runner codex-native

# 交付项目根目录。七工具批次使用新输出目录，每题一个独立进程。
# tme_out 代表该批次当时创建的输出目录。
printf '%s\n' 1 2 3 | xargs -P 3 -I TASK "$PY" experiments/run_codex_research.py --tasks TASK --mode native --model gpt-5.6-luna --effort high --auth chatgpt --timeout 5400 --output "$tme_out/task-TASK"

# MARBLE 上游仓库根目录。命令取自当时的 shell 历史。
cd "$MARBLE"
"$PY" scripts/run_benchmark.py --scenario research --model openai/gpt-5.6-luna --eval_model openai/gpt-5.6-terra --task_ids 1-3 --coordinate_mode graph --max_iterations 5 --output_dir results/luna-research --overwrite
```

下方从交付仓库根目录执行。历史批次的一条命令可生成多行结果，单题命令把原批次拆开，保留同题参数。P2P 各行根据原始运行清单绑定当时配置。历史未保存逐进程的 harness 提交号，早期动作被拒后的纠正过程也未完整复核。交接核查的观察结果和停止位置见复现手册。后续采样可能选出不同成员或得到不同评分。[批次命令与实跑核查](baseline-reproduction.md)

```bash
cd "$REPO"
```

## MARBLE Codex · gpt-5.6-luna · 七工具

每题第一条生成答案，第二条把新答案交给历史评委。交付验收依据此批，历史直属子代理数为 5、5、3。 [该批逐题证据](../experiments/history/cases/marble-native-gpt-5.6-luna-7tools)

```bash
# research/1  完成，3.67/5
"$PY" "$O/seven/experiments/run_codex_research.py" --tasks 1 --mode native --model gpt-5.6-luna --effort high --auth chatgpt --timeout 5400 --output "$O/native-seven-gpt-5.6-luna-1"
"$PY" -m eval.score --config "$H/configs/research-56.yaml" --judge-model gpt-5.6-terra --output "$O/native-seven-gpt-5.6-luna-1/scores.json" "$O/native-seven-gpt-5.6-luna-1/research-1/answer.jsonl"

# research/2  完成，4.00/5
"$PY" "$O/seven/experiments/run_codex_research.py" --tasks 2 --mode native --model gpt-5.6-luna --effort high --auth chatgpt --timeout 5400 --output "$O/native-seven-gpt-5.6-luna-2"
"$PY" -m eval.score --config "$H/configs/research-56.yaml" --judge-model gpt-5.6-terra --output "$O/native-seven-gpt-5.6-luna-2/scores.json" "$O/native-seven-gpt-5.6-luna-2/research-2/answer.jsonl"

# research/3  完成，4.00/5
"$PY" "$O/seven/experiments/run_codex_research.py" --tasks 3 --mode native --model gpt-5.6-luna --effort high --auth chatgpt --timeout 5400 --output "$O/native-seven-gpt-5.6-luna-3"
"$PY" -m eval.score --config "$H/configs/research-56.yaml" --judge-model gpt-5.6-terra --output "$O/native-seven-gpt-5.6-luna-3/scores.json" "$O/native-seven-gpt-5.6-luna-3/research-3/answer.jsonl"
```

## MARBLE 官方 · gpt-5.6-luna

从固定版本的上游仓库启动，官方引擎完成生成和评分。每条使用独立输出目录。 [该批逐题证据](../experiments/history/cases/marble-official-gpt-5.6-luna)

```bash
# research/1  完成，3.67/5
(cd "$MARBLE" && "$PY" scripts/run_benchmark.py --scenario research --task_ids 1 --model openai/gpt-5.6-luna --eval_model openai/gpt-5.6-terra --coordinate_mode graph --max_iterations 5 --output_dir "$O/official-gpt-5.6-luna-1" --overwrite)

# research/2  完成，4.67/5
(cd "$MARBLE" && "$PY" scripts/run_benchmark.py --scenario research --task_ids 2 --model openai/gpt-5.6-luna --eval_model openai/gpt-5.6-terra --coordinate_mode graph --max_iterations 5 --output_dir "$O/official-gpt-5.6-luna-2" --overwrite)

# research/3  完成，3.67/5
(cd "$MARBLE" && "$PY" scripts/run_benchmark.py --scenario research --task_ids 3 --model openai/gpt-5.6-luna --eval_model openai/gpt-5.6-terra --coordinate_mode graph --max_iterations 5 --output_dir "$O/official-gpt-5.6-luna-3" --overwrite)
```

## MARBLE P2P · gpt-5.6-luna

使用交付版顺序 P2P 入口和每题原配置。该批未显式设置成员推理档。 [该批逐题证据](../experiments/history/cases/marble-p2p-gpt-5.6-luna)

```bash
# research/1  完成，3.67/5
"$PY" "$REPO/run.py" --config "$P56/research-1/settings.json" --runner tme --output "$O/marble-p2p-gpt-5.6-luna-1"

# research/2  完成，4.33/5
"$PY" "$REPO/run.py" --config "$P56/research-2/settings.json" --runner tme --output "$O/marble-p2p-gpt-5.6-luna-2"

# research/3  完成，4.00/5
"$PY" "$REPO/run.py" --config "$P56/research-3/settings.json" --runner tme --output "$O/marble-p2p-gpt-5.6-luna-3"
```

## Work · Codex 原生子代理

每题启动独立的 DRBench Docker App，模型为 `gpt-6-luna`，推理档为 `high`。历史三个主会话各创建两个直属子代理。 [该批逐题证据](../experiments/history/cases/drbench-codex-native-gpt-6-luna)

```bash
# DR0001  完成，16.67%
"$PY" -u "$O/work/run.py" --config "$WCFG/DR0001/settings.json" --runner codex-native --output "$O/work-codex-native-DR0001"

# DR0002  完成，28.57%
"$PY" -u "$O/work/run.py" --config "$WCFG/DR0002/settings.json" --runner codex-native --output "$O/work-codex-native-DR0002"

# DR0003  报告完成，首次评分遇到 RemoteProtocolError，重评原报告得 20.00%
"$PY" -u "$O/work/run.py" --config "$WCFG/DR0003/settings.json" --runner codex-native --output "$O/work-codex-native-DR0003"
# 历史补评用同一份已交付报告。此行对归档原件重新调用官方评委。
"$PY" -m experiments.history.rescore_work "$H/configs/work.yaml" DR0003 "$C/drbench-codex-native-gpt-6-luna/DR0003/report.json" > "$O/work-native-DR0003-rescore.json"
```

## Work · 顺序 P2P

每题启动独立的 DRBench Docker App，模型为 `gpt-6-luna`，推理档为 `high`。历史三题各自生成三人名册，再逐人唤醒。 [该批逐题证据](../experiments/history/cases/drbench-p2p-gpt-6-luna)

```bash
# DR0001  完成，0.00%
"$PY" -u "$O/work/run.py" --config "$WCFG/DR0001/settings.json" --runner tme --output "$O/work-tme-DR0001"

# DR0002  完成，14.29%
"$PY" -u "$O/work/run.py" --config "$WCFG/DR0002/settings.json" --runner tme --output "$O/work-tme-DR0002"

# DR0003  完成，50.00%
"$PY" -u "$O/work/run.py" --config "$WCFG/DR0003/settings.json" --runner tme --output "$O/work-tme-DR0003"
```

要核对历史数字，可从 Git 检出的评分记录离线还原整张结果表。

```bash
"$PY" experiments/replay_baseline_scores.py "$H" > "$O/verified.csv"
cmp experiments/baseline-results.csv "$O/verified.csv"
```
