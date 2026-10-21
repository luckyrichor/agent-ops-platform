# P2 数据集核验与来源

由 Codex 生成 · 2026-10-21 · agent-ops-platform@711d89decac2。完整记录由 `scripts/report-p2.py` 自动生成，见 [原始统计](reports/p2-2026-10-21/dataset-verification.json)。所有原文与复用 ECD 原文的合成数据仅在 tx `.local/datasets/`，不进仓库、不发群、不再分发。

## 完整下载与校验

| 数据集 | ZIP 实际字节 | SHA256 | HTTP | 下载耗时 |
| --- | --- | --- | --- | --- |
| olist | 44,717,580 | `967e41e04fc306fe604e2a693f488995a8b41e5047418f8a5c8e4abd6deca784` | 200 | 6.867 秒 |
| ecd | 54,000,633 | `41f09f0312b7611863794fb46d9f7f4061d8171901232ac3c17929295e4878b2` | 200 | 7.590 秒 |

两份 ZIP 全部成员 CRC 通过，大小与方案一致。CSV 采用 CSV 解析计数并排除表头；ECD 按非空行计数，不拿物理换行数冒充带引号 CSV 的行数。

| 数据集 | 文件 | 实际记录数 | 成员 SHA256 |
| --- | --- | --- | --- |
| olist | olist_customers_dataset.csv | 99,441 | `983a422239e1712ded753b3bf9ecf47dc73f144d306029dcfa99e70a226883d2` |
| olist | olist_geolocation_dataset.csv | 1,000,163 | `b514f6fc991b9566aeba02aa5d67e2c3630f034b60a0e05aa0d082a3b66d88d6` |
| olist | olist_order_items_dataset.csv | 112,650 | `0bc4d068c4fe38cbb01bd90e8746e3c613fe7b4baef75fab7b0e329701c3e279` |
| olist | olist_order_payments_dataset.csv | 103,886 | `4f713964f2815dbbaa40b9488268c55aac3627bfce5aa96cf58d1f3616de3cc0` |
| olist | olist_order_reviews_dataset.csv | 99,224 | `012b61c7593e34f51fa614efdf802b9c7056ce6aae5307ddb93236e7cfc797d7` |
| olist | olist_orders_dataset.csv | 99,441 | `8df58ef3d2d7e9944010f7beecd9b75367f5588ec6e3c91cec19ae3345ef9ecf` |
| olist | olist_products_dataset.csv | 32,951 | `3e6569628a17fbc75fd206ee357b59e20364b9afa90f5b6cd5b4d624c58aa9cc` |
| olist | olist_sellers_dataset.csv | 3,095 | `1f643d2b950373b85735e7794b20986f528d7a000432e7c6f9bcbb44d0846a0e` |
| olist | product_category_name_translation.csv | 71 | `a81f0d1f27b27e7293f761bc79e3ce8f348ee39c4b3ed3e49bde38f478586278` |
| ecd | E-commerce dataset/dev.txt | 10,000 | `7ee4da6a7144040337b0f3dc999641255d5ef19a035d939ae4a6eafd3cadd7a3` |
| ecd | E-commerce dataset/test.txt | 10,000 | `7c5d5c60fee198e6855a714e1161ed165b564ccb96b4a9db36593e5f854319e6` |
| ecd | E-commerce dataset/train.txt | 1,000,000 | `7f7923594f2c4efec272850a21ec3c96b3259fae9e3fbb5d20bd4c1ca4199c3d` |

UserBehavior：**需负责人提供登录后下载的完整版文件**。预期 ZIP 大小 949,805,174 字节（方案元数据，尚非实测）；本轮实际获得数据字节 0，实际行数/SHA256/Zipf 指数均未验证。只读取公开页，未登录、未注册、未使用镜像替代，不要求在对话中发送密码或令牌。

## 下载地址与许可

- [Olist 官方页面](https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce)，按方案固定版本直链下载；CC BY-NC-SA 4.0，非商业、署名及相同方式共享约束按原许可处理。
- [ECD 作者仓库](https://github.com/cooelf/DeepUtteranceAggregation)，使用其原 Google Drive 文件。未发现明确数据许可；仅本地个人评测，公开材料只有方法、哈希和统计，不再分发文本。引用：Zhang 等，COLING 2018，Modeling Multi-turn Conversation with Deep Utterance Aggregation。
- [UserBehavior 天池页面](https://tianchi.aliyun.com/dataset/649)；许可 CC BY-NC-SA 4.0 目前仅按方案记录，原始文件仍未获得。

## 分布与局限

Olist 拟合联合经验分布并重采样：商品类目与有观测的价格/运费，订单状态/时间/原购物篮组合。无价格商品 0 个未补假价格。源月份实测从 2016-09 至 2018-10，方案的日期描述不是完整范围；源时区未提供，合成视为 UTC，不声称真实绝对时间。商品标题、库存、物流跟踪号为明确的合成字段。

ECD 去重正例上文 390,342 段，负响应候选不当作真实对话续句；空格按分词还原要求移除。实测片段数从 2 至 120，与 README 概括范围不同，未偷偷截断。首片段说话人快速线索检查 40 条：{'no-clear-cue': 35, 'service-cue': 5}，不是人工核验，无法据此确认身份。

**规则代理，非人工真实意图分布，首片段说话人未确认。** 经负责人批准，现有规则首片段命中权重只用于合成类别占比。全量工单行、事件载荷、manifest 与每文件 provenance 都保留此标记；不得用于路由准确率或质量指标。等待人工占比独立参数，当前 5%；未来 S1 做多档实验，本轮未做。P8 人工标注完成后替换并记 progress。

## 重建

```bash
uv run python scripts/download-datasets.py
uv run python scripts/fit-p2-data.py --verified .local/datasets/verified.json --output .local/fit-new
uv run python scripts/synthesize-p2.py --fit .local/fit-new --output .local/scale-new --seed 42 --owner-approved-ecd-proxy --waiting-human-ratio 0.05
```

UserBehavior 到位后显式传 `--userbehavior-file .local/datasets/raw/UserBehavior.csv.zip` 再核验，重新拟合并建新批次；当前未用猜测/均匀分布冒充其活跃度或商品热度。
