# Agent Team Harness 交接清单（初稿）

核对日期为 2026-11-24。交付渠道为 GitHub 的 `dev/deepseek` 分支，以交接时给出的完整 Git 提交号确定版本。在检出目录执行 `git rev-parse HEAD` 即可核对。顺序 P2P、七工具 Codex 和 DRBench Docker 的运行源码与已验收提交 `fd66f5693d0ac66e3216aae0b5ebd0144f711d7a` 一致。[获取代码](../README.md#从-github-接收) · [15 条逐题运行命令](historical-commands.md) · [逐题证据](../experiments/history)

## 一、功能实现清单

- [x] **团队初始化**　`run.py` 从实验 YAML 读取 benchmark 和题号，加载任务。MARBLE 使用上游原题角色，DRBench 由 Planner 为当前题生成至少两名职责互补的成员。Planner 填写逐人分工、选出首位成员，并写出 `plan.json`。[运行入口](../run.py) · [名册与授权校验](../runtime/planner.py) · [DRBench 任务清单](../tasks/drbench/research.yaml)
- [x] **文件权限**　每个成员只挂载自己的工作目录和会话目录。宿主按成员的 `resource_ids` 过滤任务材料。上传的附件最初仅上传者可读，随交棒发送后收件人才获得读取权。[容器挂载](../runtime/sandbox.py) · [材料过滤](../service/materials.py) · [附件授权](../service/archive.py) · [越权读取测试](../tests/test_isolation.py)
- [x] **工具权限**　可在成员名册中分配资源组。成员在回合内用 `python -m tools.client list` 查看可见的场景工具，调用时宿主再次校验资源组。当前 MARBLE 研究员共用 `research` 组，DRBench 生成的岗位共用 `drbench_app` 组。容器内的 Bash 等原生工具由执行器提供。[成员身份与请求校验](../service/api.py) · [工具授权](../service/materials.py) · [MARBLE 工具注册](../scenarios/marble.py)
- [ ] **通信权限**　已支持向一名队友发送点对点消息和附件，并校验收件人属于当前名册且不是本人。尚无配置项能按成员指定联系人白名单或选择通信渠道。[交棒校验](../runtime/envs/point_to_point.py) · [收发信](../service/messages.py)
- [ ] **成员记忆**　已有同题跨回合的私有状态。成员用 Bash 读写 `/work`，宿主保存在 `<运行目录>/members/<id>/work/`。原生会话使用该成员的 `home/`（容器内 `/state`）及会话 ID 恢复。其他成员通过消息和授权附件获取共享内容。尚无可配置的 memory 读写接口，也未提供跨题复用入口。[挂载与持久化](../runtime/sandbox.py) · [会话恢复测试](../tests/test_team.py) · [memory 实现状态](../memory/README.md)
- [x] **自主协作**　活跃成员自行决定何时结束本轮、是否共享、向哪位队友发送什么正文和附件，可选 `handoff` 或 `ready`。框架固定一次只运行一人。首位由 Planner 选择，之后优先唤醒交棒收件人，再看待收邮件，最后按种子选择尚未开工的成员。[交棒流程图](../README.md#p2p-如何交棒) · [调度与动作提交](../runtime/envs/point_to_point.py) · [A → B → A 测试](../tests/test_team.py)

交付工作区的离线测试共 59 项通过，有 1 条 Pydantic 警告。Codex 原生子代理和 MARBLE 官方运行器另有独立复现入口，本表的团队能力按顺序 P2P 入口核对。[SDK 验证清单](sdk-verification.md) · [独立复现说明](../README.md)

## 二、三场景兼容性

共用入口为 `python run.py --config <实验配置.yaml> --runner tme`。下表中的“已实跑”表示有可定位的真实模型运行记录。归档中的 `revision` 固定的是 benchmark 版本，历史运行未记录 harness 的 Git SHA。

| 场景 | 已接入 | 已实跑 | 场景配置、适配器或需替换的工具 | 实跑题号及结果路径 |
| --- | --- | --- | --- | --- |
| Research | [x] | [x] | MARBLE Research 支持 `research/N`，从原题加载角色和七项研究工具，交付文件为 `idea`。[任务配置](../tasks/marble/research.yaml) · [适配器](../scenarios/marble.py) | `research/1`，5 名成员、9 个回合，执行完成且已评分。运行 ID 为 `eee3da542c064654b77d1877dac327ca-research-1`。归档内文件见第三节。 |
| Work（DRBench） | [x] | [x] | 共用入口支持 DRBench 的企业应用工具和报告交付，P2P 按题生成名册后逐人交棒。[任务配置](../tasks/drbench/research.yaml) · [App 适配器](../scenarios/drbench.py) | P2P 和 Codex 原生子代理均完成并评分了 `DR0001`、`DR0002`、`DR0003`。逐题分数和归档位置见下表。 |
| AI for Science | [ ] | [ ] | 当前加载器只注册 MARBLE Research 和 DRBench。本次交付范围为 Research 和 Work。[场景注册入口](../scenarios/__init__.py) | 无可核实的本场景结果。 |

### Work 的历史对照结果

DRBench App 版本为 `0d699ecf6aa96b1de378595b432e9b16a82f0ed9`。两组均使用 `gpt-6-luna` 工作、`gpt-5.6-terra` 评分。下表为 `insights_recall`，以百分比显示，最后一列对三道题等权取平均。

| 历史运行方式 | DR0001 | DR0002 | DR0003 | 三题均值 |
| --- | ---: | ---: | ---: | ---: |
| 顺序 P2P | 0.00% | 14.29% | 50.00% | 21.43% |
| Codex 原生子代理 | 16.67% | 28.57% | 20.00% | 21.75% |

归档汇总为 `experiments/runs/drbench-app-three-task-comparison.json`。同目录下，P2P 的逐题目录为 `f3485343f9d9482db95d6946824efed8-<题号>/`，Codex 的为 `c02cccf986d445109e004df0ec79be5b-<题号>/`。六份 `submissions/report.json`、`result.json` 和 `scores.json` 均已核对。Codex 的 `DR0003` 首次评分出现 `RemoteProtocolError`，重试成功，过程保存在该题的 `scoring-attempts.json`。

三次历史 P2P 运行均由 Planner 生成三人名册。交付代码使用相同的建队提示和校验规则。这些分数来自历史运行。现有归档中未找到 MARBLE 官方运行器在这三道 DRBench 题上的结果。

### Research 的三组历史结果

MARBLE Research 第 1 至 3 题有完整的三组同题对照。下表取工作模型 `gpt-5.6-luna`、评委 `gpt-5.6-terra` 的批次。每题先对创新性、安全性、可行性取平均，再对三题等权取平均，满分为 5。

| 历史运行方式 | 完成并评分 | 三题均分 | 归档内证据 |
| --- | ---: | ---: | --- |
| 顺序 P2P | 3/3 | 4.00 | `experiments/runs/eee3da542c064654b77d1877dac327ca-research-<题号>/scores.json` |
| Codex 原生子代理 | 3/3 | 3.89 | `experiments/codex-eval/luna-native-7tools-36UNkoIE/scores-gpt-5.6-terra.json` |
| MARBLE 官方运行器 | 3/3 | 4.00 | MARBLE 检出目录下的 `results/luna-research/research/openai_gpt-5.6-luna/results.jsonl` 和同目录 `summary.json` |

### 本轮在线核查

当前交付版的验收记录见[工作区核对记录](../tests/sdk_verification/results/handoff-check.json)。从仓库根目录可直接运行两个场景的 P2P 及 Work Codex，无需另取原工作树或覆盖文件。命令见[交付入口](../README.md#运行-p2p)。本轮从同一份交付配置启动 Work 两组，逐题结果如下。

| 题号 | P2P 从 Planner 到第二次唤醒 | Codex 原生中途观测 |
| --- | --- | --- |
| DR0001 | 三人名册，REG001 交棒给 SYN001。首次 Planner 报 `Server disconnected without sending a response.`，同参单题重试通过 | 两个直属子代理，App 工具成功返回 |
| DR0002 | 三人名册，MKT01 交棒给 SYN03 | 两个直属子代理，App 工具成功返回 |
| DR0003 | 三人名册，LOYALTY_LEAD 就绪，宿主唤醒 CRM_ANALYST | 两个直属子代理，App 工具成功返回 |

两组的首轮输入分别与历史 Planner 和 Codex 提示摘要吻合。Research 的七工具 Codex 也在三道题上核实了相同首轮提示、工具清单和 5、5、3 个直属子代理。两类核查均在观察到指定中间行为后主动停止。[中途核查记录](../tests/sdk_verification/results/baseline-live.json) · [复核方法](baseline-reproduction.md)

## 三、交付清单

- [x] **代码仓库**　GitHub 交付分支包含 Work 动态名册的 P2P 入口、逐题命令和历史证据。用提交号核对接收版本。接收团队的远端读取权限待确认。[交付分支](https://github.com/Jerry-723/team-memory-eval/tree/dev/deepseek)
- [ ] **可复现环境**　宿主使用 Python 3.12，Codex CLI 使用 `0.166.0`。固定上游提交、镜像 ID、依赖版本和服务条件见复现手册。新机器从空环境安装尚待验证。[环境和镜像](baseline-reproduction.md#2-准备上游环境和历史配置)
- [x] **可运行的团队配置**　Research 采用原题名册和七项工具，Work 动态生成岗位并向成员分配 DRBench App 工具。[Research 配置](../experiments/history/configs/research-56.yaml) · [Work 配置](../experiments/history/configs/work.yaml)
- [x] **同题复现命令**　五组共 15 条逐题运行命令，附独立评分和历史补评命令。离线可复核 15 条评分和全部 6 份 TME 正式交付。[逐题命令](historical-commands.md)
- [ ] **一道题的真实结果与完整日志**　已交付下面这题的运行参数、名册、已接受动作轨迹、正式答案和评分记录。完整原始运行日志留存在内部归档。[本题交付文件](../experiments/history/cases/marble-p2p-gpt-5.6-luna/research-1)
- [x] **兼容性说明**　第二节记录 Research、Work 的实跑情况和场景接入范围。[场景加载器](../scenarios/__init__.py)

### 真实运行核对记录

以 TME 的 `research/1` 为例。运行 ID 为 `eee3da542c064654b77d1877dac327ca-research-1`，归档使用 MARBLE 提交 `eae9f15e5b9f1e808f8ff61800b129d5538c1b47`。Planner 和成员使用 `gpt-5.6-luna`，五人按顺序完成九个回合，正式交付研究方案 `idea`。评委 `gpt-5.6-terra` 给出的创新性、安全性、可行性分别为 4、4、3，平均 3.67/5。[原始交付](../experiments/history/cases/marble-p2p-gpt-5.6-luna/research-1/idea) · [交棒轨迹](../experiments/history/cases/marble-p2p-gpt-5.6-luna/research-1/trajectory.json) · [评分](../experiments/history/cases/marble-p2p-gpt-5.6-luna/research-1/score.json)
