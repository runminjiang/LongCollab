# 五组基线的环境、结果和核查方法

交付结果共 15 条。MARBLE Research 使用七项研究工具，三个框架各跑第 1 至 3 题。Work 使用 DRBench Docker App，Codex 原生和 TME 分别跑 DR0001 至 DR0003。逐题命令表列出原始批次入口、单题运行和独立评分步骤。[15 条逐题运行命令](historical-commands.md) · [逐题结果 CSV](../experiments/baseline-results.csv)

## 1. 核对历史分数

Research 对创新性、安全性、可行性三项取算术平均，满分 5。Work 使用官方 `insights_recall`。下表按题号排列，所有分数均来自历史评委 `gpt-5.6-terra`。

| 场景与基线 | 工作模型 | 第 1 题 | 第 2 题 | 第 3 题 |
| --- | --- | ---: | ---: | ---: |
| MARBLE · Codex 原生子代理 | gpt-5.6-luna | 3.67 | 4.00 | 4.00 |
| MARBLE · 官方框架 | gpt-5.6-luna | 3.67 | 4.67 | 3.67 |
| MARBLE · TME 顺序 P2P | gpt-5.6-luna | 3.67 | 4.33 | 4.00 |
| Work · Codex 原生子代理 | gpt-6-luna | 16.67% | 28.57% | 20.00% |
| Work · TME 顺序 P2P | gpt-6-luna | 0.00% | 14.29% | 50.00% |

从交付仓库根目录执行。以下命令读取归档的评分字段，验证已保存的送评文件摘要，并重建上表对应的逐题 CSV。

```bash
REPO="$PWD"
PY="$HOME/miniconda3/envs/team-memory-eval/bin/python"
RUNS=$(mktemp -d /tmp/tme-baselines-XXXXXXXX)
B="$REPO/experiments/history"
"$PY" experiments/replay_baseline_scores.py "$B" > "$RUNS/results.csv"
cmp "$RUNS/results.csv" experiments/baseline-results.csv
```

`index.json` 标明原始评分文件和 SHA-256。官方 JSONL 还保留原始行号和该行摘要。Work Codex 的 DR0003 首次评分遇到 `RemoteProtocolError`，同一份原报告重评得到 0.20。[来源索引](../experiments/history/index.json) · [该题原报告](../experiments/history/cases/drbench-codex-native-gpt-6-luna/DR0003/report.json)

## 2. 准备上游、环境和历史配置

历史命令下文以 `$PY` 和新输出路径代替原机器的绝对路径。MARBLE 官方命令与七工具 Codex 命令能在当时的 shell 历史中定位。Work 两条命令来自当时实际启动两组子进程的脚本。P2P 命令行只写配置名，该配置在不同实验间被修改过，历史目录的两份 YAML 按各批实际 `run.json` 分别还原，端点和密钥改从环境变量读取。

历史宿主使用项目的 Python 3.12 环境。核查环境为 3.12.16，额外的 benchmark 依赖装在独立目录。已有固定版本的上游检出时，直接设置 `TME_REFERENCE_REPOS` 为两者的父目录。需要重新准备时，在新目录执行以下命令。

```bash
export TME_REFERENCE_REPOS="$RUNS/upstream"
mkdir -p "$TME_REFERENCE_REPOS"
git clone https://github.com/ulab-uiuc/MARBLE.git "$TME_REFERENCE_REPOS/MARBLE"
git -C "$TME_REFERENCE_REPOS/MARBLE" checkout --detach eae9f15e5b9f1e808f8ff61800b129d5538c1b47
git clone https://github.com/ServiceNow/drbench.git "$TME_REFERENCE_REPOS/drbench"
git -C "$TME_REFERENCE_REPOS/drbench" checkout --detach 0d699ecf6aa96b1de378595b432e9b16a82f0ed9
```

`TME_BENCHMARK_DEPS` 可指向已准备的 benchmark 依赖目录，官方脚本启动前需将它加入 `PYTHONPATH`。历史目录的 `environment/python-distributions.txt` 记录本次核查可见的包版本（包括 CPU 版 torch）。这是核查环境快照；历史逐题清单没有保存 wheel 哈希或 conda 锁。同目录列出 Linux x86_64 的 conda 原始包 URL 与 MD5、宿主 pip 发行版、额外的 benchmark pip 发行版。迁移到新机器时可以安装到独立目录；每一层所用的私有镜像源、wheel 文件及模型缓存未归档，从空环境重新安装尚未验证。[项目依赖](../pyproject.toml) · [镜像构建](../README.md#环境和原题)

```bash
MARBLE="$TME_REFERENCE_REPOS/MARBLE"
if [[ -n ${TME_BENCHMARK_DEPS:-} ]]; then
  export PYTHONPATH="$TME_BENCHMARK_DEPS${PYTHONPATH:+:$PYTHONPATH}"
fi
export CUDA_VISIBLE_DEVICES="" LITELLM_LOCAL_MODEL_COST_MAP=True
```

如需在另一台机器重建环境，下面是按核查快照整理的安装命令。原机已配好的 conda 环境可跳过此步。CPU 版 torch 需要对应平台的 wheel。安装前应确认原软件源提供相同版本。

```bash
conda create -y --prefix "$RUNS/env" --file "$B/environment/linux-64-conda.txt"
PY="$RUNS/env/bin/python"
"$PY" -m pip install --no-deps -r "$B/environment/host-pip.txt"
export TME_BENCHMARK_DEPS="$RUNS/benchmark-deps"
"$PY" -m pip install --no-deps --target "$TME_BENCHMARK_DEPS" \
  --extra-index-url https://download.pytorch.org/whl/cpu -r "$B/environment/benchmark-pip.txt"
export PYTHONPATH="$TME_BENCHMARK_DEPS${PYTHONPATH:+:$PYTHONPATH}"
```

在线生成需要能访问相同模型 ID 的服务。在启动进程的 shell 中导出 `OPENAI_BASE_URL`（实际 API 前缀）、`OPENAI_API_KEY`（成员凭据）、`TME_JUDGE_BASE_URL` 和 `TME_JUDGE_API_KEY`（评委配置）。历史 TME 成员与评委使用同一地址，可以令 `export TME_JUDGE_BASE_URL="$OPENAI_BASE_URL"`。MARBLE 官方使用 LiteLLM 的 `OPENAI_API_BASE` 和 `OPENAI_API_KEY`，该凭据需能调用工作模型和评委。独立 Research Codex 用宿主文件版 ChatGPT 登录。Work 另需导出 `SERPER_API_KEY` 供公开搜索使用，模型服务需支持 Chat Completions、流式 Responses 以及 `text-embedding-ada-002`。

| 运行位置 | 历史镜像或程序 |
| --- | --- |
| 顺序 P2P，Codex 成员 | `tme-codex:b1`，镜像 ID `sha256:f3b79e6fc7f610d0e45b07b8178dde5d9c3a4adb9cee1a7d59507c78e8eaab39` |
| 两个场景的历史 Codex 原生组 | 宿主 Codex CLI `0.166.0`，把可执行文件所在目录挂入 `tme-claude-code:b1`，镜像 ID `sha256:b3e812610d0f787318d52f962e922bda3dc36f7b8f564c6144cc429037642198`。执行的程序为 Codex。 |
| Work 企业应用 | `ghcr.io/mmunozm/drbench-services@sha256:62355c66022a5208767f534152e27432722c9dd0a4154891355e6d2580b51bb5`，App 模式 |

两个本地成员镜像 ID 及宿主 CLI 版本已对照。新机器构建后用 `docker image inspect --format '{{.Id}}' <镜像标签>` 对照。镜像层、宿主登录和模型服务需在运行机器上另行准备。

## 3. MARBLE 官方框架

在固定的上游检出中运行 `scripts/run_benchmark.py`。三题共用 `graph` 模式和最多 5 轮的设置。已核对归档检出的 2,910 个文件，Git 根树为 `57e40328c22b0b3cde33afa2d0a7509e8e9c5f40`。该版本的 Research 环境注册七项工具，含 `fetch_webpage`。历史运行保留了启动参数和结果，未记录每个进程启动时的工作树指纹。[固定版本的官方 Research 环境](https://github.com/ulab-uiuc/MARBLE/blob/eae9f15e5b9f1e808f8ff61800b129d5538c1b47/marble/environments/research_env.py)

用官方程序从已评分记录重建汇总，可核验三题的指标和迭代次数。在线生成请使用逐题清单中的命令。

```bash
cp -R "$B/official/luna-research" "$RUNS/official-summary"
(cd "$MARBLE" && "$PY" scripts/run_benchmark.py --scenario research --model openai/gpt-5.6-luna \
  --output_dir "$RUNS/official-summary" --summary_only)
S="$RUNS/official-summary/research/openai_gpt-5.6-luna"
cmp "$S/summary.json" "$S/expected-summary.json"
```

## 4. TME 顺序 P2P

Research 从原题读取 5、5、3 人的固定名册。Work 由 Planner 按题生成名册，历史三题均生成三人。两组种子均为 7，最多 40 回合，每人上限 500,000 tokens。Research 成员采用原生默认推理档，Work 使用 `high`。六题均在正式交付后自动送评。[Research 配置](../experiments/history/configs/research-56.yaml) · [Work 配置](../experiments/history/configs/work.yaml)

准备好上游后，从仓库根目录执行下面的命令。它用归档的已接受回复检查 Planner 输入、成员顺序、每次正式唤醒输入以及最终交付的字节。Research 三题分别是 9、8、6 回合，Work 是 4、5、4 回合，合计 36 次唤醒和 6 份交付。被拒动作的纠正过程和实时检索不在此次回放范围内。

```bash
(cd "$REPO" && "$PY" -m experiments.history.replay_p2p_turns "$B")
```

## 5. 七工具 Codex 的中途核对

Research 历史批次以 `gpt-5.6-luna`、`high` 和宿主 Codex CLI `0.166.0` 运行。历史首轮提示、工具清单和官方角色均可逐题对照。下面从交付入口运行同题，使用内置同版 CLI 的镜像。核查时三题都有成功的真实工具调用，分别观察到 5、5、3 个直属子代理。达到核查点后主动停止，记录为 `KeyboardInterrupt`，本次核查没有最终评分。[中途核查节选](../tests/sdk_verification/results/baseline-live.json)

```bash
cd "$REPO"
OUT="$RUNS/delivery-seven"
mkdir -p "$OUT"
printf '%s\n' 1 2 3 | xargs -P 3 -I N "$PY" experiments/run_codex_research.py \
  --tasks N --mode native --model gpt-5.6-luna --effort high --auth chatgpt \
  --timeout 5400 --output "$OUT/task-N"
```

交付版在运行中保留原生会话，正常结束时也将父子关系写入 `run.json`。以下中途验收核对七个工具、成功的真实调用、模型和直属子代理数。即使随后主动停止，也能读取已保存的父子关系。完整交付可另外检查 `answer.jsonl` 和最终执行状态。

```bash
"$PY" - "$OUT" <<'PY'
import json
import sys
from pathlib import Path

from experiments.run_codex_research import native_team

root = Path(sys.argv[1])
expected_tools = [
    "get_related_papers", "get_recent_papers", "collect_publications_and_coauthors",
    "get_paper_by_keyword", "get_paper_by_arxiv_id", "get_paper_by_title", "fetch_webpage",
]
passed = True
for n, expected_children in [(1, 5), (2, 5), (3, 3)]:
    folder = root / f"task-{n}" / f"research-{n}"
    run = json.loads((folder / "run.json").read_text())
    with (folder / "events.jsonl").open() as events:
        parent = next(json.loads(line)["thread_id"] for line in events
                      if json.loads(line).get("type") == "thread.started")
    team = native_team(folder, parent)
    children = {a["thread_id"] for a in team
                if parent and a["parent_thread_id"] == parent}
    with (folder / "tools.jsonl").open() as calls:
        tool_returned = any(json.loads(line)["result"].get("success") is True for line in calls)
    ok = (tool_returned and run["mode"] == "native"
          and run["task_id"] == f"research/{n}" and run["model"] == "gpt-5.6-luna"
          and run["effort"] == "high" and run["tools"] == expected_tools
          and len(children) == expected_children)
    print(f"research/{n}  tools={len(run['tools'])}  children={len(children)}/{expected_children}  execution={run['status']}  checkpoint_pass={ok}")
    passed &= ok
raise SystemExit(0 if passed else 1)
PY
```

## 6. Work 的真实 Docker 启动

两组均由本项目入口为每道题单独启动 DRBench 上游的企业应用容器。宿主注册 App 工具并调用官方报告评分器。历史两组均完成三题，送评文件为 `submissions/report.json`。交付时另行核查了真实中间行为。TME 三题均生成三人名册，进入第二次成员唤醒。DR0001 首次分工遇到 `Server disconnected without sending a response.`，同参数重试通过。Codex 三题在核查点各有两个直属子代理，并已收到成功的 App 工具响应。[交付核查节选](../tests/sdk_verification/results/handoff-check.json)
