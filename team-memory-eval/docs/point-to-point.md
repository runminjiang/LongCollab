# 点对点交付

## 1. 目录

| 位置 | 职责 |
| --- | --- |
| `runtime/` | 根据任务名册分工，在成员的私有容器和原生会话中执行回合。 |
| `service/` | 宿主保存信箱、材料权限、附件、正式交付、用量和事件，转发模型请求。 |
| `tools/` | 成员工具客户端和宿主轨迹查看器。 |
| `scenarios/`、`tasks/`、`eval/` | 加载固定版本的原题，定义可见材料和交付项，调用官方评分器。 |
| `experiments/`、`images/` | 实验配置、独立 Codex 基线和成员镜像。 |
| `tests/`、`docs/`、`memory/` | 离线测试、原生会话验证、使用文档和预留目录。 |

## 2. 谁接下一棒

MARBLE 使用原题的固定名册。DRBench 由 Planner 根据题目生成名册，至少两种不同岗位，每人获授权使用 `drbench_app`。Planner 一次完成分工并指定首位成员，名册在本题后续回合保持固定。每次只运行一名成员，宿主等当前回合退出后再处理其动作。成员可以连续查材料、用工具、写文件，回合结束时提交一个动作。

- `handoff` 指定收件人，发送正文以及可选的附件 ID。收件人下一轮接棒，已经就绪的成员也可以被来信唤醒。
- `ready` 结束自己的工作。成员也可以在交棒的同时设置 `ready: true`。任何正式交付都要先上传文件，再把文件 ID 填入动作的 `submissions`。

没有指定收件人时，宿主先检查待收邮件，再按种子选择一位尚未开工的成员。全员就绪且交付齐全时进入评分。无人可唤醒但仍有缺件时，结果记为 `stalled`。

## 3. 读一条轨迹

`plan.json` 记录分工。`events.jsonl` 中的 `activated` 给出接棒原因，`committed` 给出本轮动作和下一位成员。同一 `turn_id` 只提交一次。例如，A 发给 B，B 回信后就绪，A 恢复自己的会话、上传文件，最后用 `ready` 提交。

## 4. 模块入口

运行入口在 `run.py`。单题调度和交棒在 `runtime/envs/point_to_point.py`，动作格式在 `runtime/envs/team.py`。成员容器在 `runtime/sandbox.py` 中启动，会话适配器在 `runtime/harnesses/`。宿主分别通过 `service/messages.py` 投递消息、`service/archive.py` 管理文件版本、`service/materials.py` 控制材料与工具权限、`service/budget.py` 记账、`service/events.py` 记录事件、`service/gateway.py` 转发模型请求。
