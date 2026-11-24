# 给在本仓库工作的 agent

本项目实现单活跃成员的点对点协作基线。动手前先读 `docs/point-to-point.md`（设计）和 `docs/sdk-verification.md`（SDK 验证清单）。

目录结构和模块划分以 `docs/point-to-point.md` 第 1、4 节为准，本文件不重新定义。发现设计与本文件、或设计与实际可行做法冲突时，停下来在报告里指出，不要自行选择一边继续。环境、共用服务器和汇报方面的约束以本文件为准。

## 环境

- Python 环境：`$HOME/miniconda3/envs/team-memory-eval`（Python 3.12）。一律用 `$HOME/miniconda3/envs/team-memory-eval/bin/python` 和同目录的 `pip`、`pytest`，不要用系统 Python，不要改动其他 conda 环境。
- 项目环境与成员镜像的依赖已固定。不要升级，SDK 行为以已安装版本的源码为准。
- 本地模型端点（仅用于框架验证）：`http://127.0.0.1:8505/v1`，模型名 `qwen3.5-9b`，上下文 32768。它没有开启自动工具选择，带 `tool_choice: "auto"` 的请求会返回 400。不要重启或修改这个 vLLM 进程。需要工具调用时，用 SDK 的非原生工具调用方式，或者只用脚本化替身端点。

## 这台服务器是多人共用的

- 只能创建、停止、删除名字以 `tme-` 开头的容器、网络和卷。绝不碰其他容器、镜像、进程和 GPU。
- 只在本仓库 和 `/tmp/tme-*` 下写文件。
- 不要 `git init`、提交或推送。
- 长时间运行的进程在结束前清理掉。

## 代码约定

- 仓库根目录下的目录只有设计第 1 节列出的十一个：`docs/`、`runtime/`、`service/`、`tools/`、`memory/`、`scenarios/`、`tasks/`、`eval/`、`experiments/`、`tests/`、`images/`。不加外层包目录，不新增其他顶层目录。
- `runtime`、`service`、`tools`、`memory`、`scenarios`、`eval` 是顶层 Python 包，从仓库根目录导入（例如 `from service.messages import ...`）。
- `service/` 里按设计第 4 节的模块分文件：消息、材料、档案、预算账本、事件日志、模型网关各自一个模块，不合并成一个大文件。
- SDK 验证脚本及其结果放在 `tests/sdk_verification/`；验证清单和结论仍在 `docs/sdk-verification.md`。
- 还没有实现的目录只放一个 `README.md`，写明它在设计里的职责和"尚未实现"，不放占位代码。
- 团队状态全部在宿主侧的团队服务里。成员容器里的团队工具只是它的客户端。评分参考和真实模型密钥留在宿主。
- 测试默认不调用任何真实模型。需要模型的地方用脚本化替身端点（与 OpenAI 接口同协议，按脚本返回工具调用）。
- 注释和文档用中文，标识符用英文。

## 汇报要求

- 如实区分：已运行并通过、已运行但失败（附原始错误信息）、写了但没运行、没做。
- 不要为了让测试通过而改弱断言。验证项不成立就报告不成立。
- 最终回复不超过 60 行，列出改动的文件、每项的结果、遗留问题。
