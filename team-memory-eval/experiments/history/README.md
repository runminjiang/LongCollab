# 历史实验记录

按结果表的批次和题号打开 `cases/<批次>/<题号>/`，Research 题号在路径中写成 `research-1` 等。每题的 `score.json` 保存归档的评分字段。原始评分文件位置、摘要和送评文件对应关系在索引中。[15 条运行命令](../../docs/historical-commands.md) · [逐题分数](../baseline-results.csv) · [来源索引](index.json)

| 目录 | 内容 |
| --- | --- |
| [cases](cases) | 各组的送评答案或报告、评分记录和适用的运行参数。六条 P2P 运行另有名册、交棒轨迹和上传文件。 |
| [configs](configs) | 从历史运行清单还原的Research 和 Work 两份批量配置，端点与凭据由运行环境提供。 |
| [official](official) | MARBLE 官方七工具批次的逐题评分字段、迭代次数和参考汇总。 |
| [environment](environment) | 核查机器的 conda、pip 和 benchmark 依赖快照。 |

`prepare-source.sh` 在新目录装入旧启动器和适配器，供逐条命令使用。`work-overlay` 恢复 Work 动态名册与宿主 Codex CLI 启动方式，`research-native` 保留 Research 七工具的宿主 CLI 启动方式。日常交付入口在仓库根目录。[环境与批次复核](../../docs/baseline-reproduction.md)
