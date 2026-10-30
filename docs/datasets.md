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

## 2026-10-23 延时任务补跑核查（Codex，北京时间）

本次分析与四门复验的代码版本为 `3ccfe84f58e85395ae75ff909b0921045e4f87a2`，开始时工作区干净、pull 后与远端一致；本节和本次报告是新增文档，以上 2026-10-21 的来源与原始数据不改写。

负责人消息称 UserBehavior ZIP 已上传，但本次在实际 tx 主机仍未找到。项目 `.local/datasets/raw/` 只有 `olist.zip`（44,717,580 字节）和 `ecd.zip`（54,000,633 字节）；`/home/ubuntu/files` 不存在。已递归检查 `/home/ubuntu`、`/tmp`、`/mnt`、`/data`、`/srv`、`/var/tmp` 的 UserBehavior 名称变体，并检查大体积 ZIP/GZIP 和 800–1100 MiB 的其他命名文件，未发现候选。该结论仅限本次可读路径，不声称文件未上传到任何位置。

附录 C 的 **949,805,174 字节仍是历史页面元数据，非本次文件实测值**。实际 ZIP 大小、行数、CRC、SHA256、活跃度、热度、浏览/加购/购买比例均为未验证，差值无法计算；不得把“未找到”记作实际 0 字节文件。没有登录、注册、改用镜像或替代数据源。

没有重建或导入批次，没有修改已验收商品、库存、订单、用户或访问事件表；本轮导入耗时不适用。既有 `.local/datasets/` 表观占用复查为 6,835,117,461 字节（不含 Docker 数据卷），两份 raw ZIP 大小与旧记录相同，但本轮没有重跑其全量 CRC/行数核验。原有数据库行数、导入耗时和校验和仍属于 2026-10-21 的证据。

本轮完整日志、源码摘要、文件搜索记录、B1 旧证据核对及待补文件说明见 [补跑报告](reports/p2-followup-2026-10-23/delivery.md)。需要负责人提供服务器上可读的 ZIP 实际绝对路径；不需要密码或令牌。P2 仍未全部验收，P3 及以后未执行。


## 2026-10-23 UserBehavior 到位后的完整核验与重建

已从负责人提供的飞书附件取得完整 ZIP。实际 949,805,174 字节，与方案附录 C 的 949,805,174 字节相差 0；SHA256 `ce56e7bccfe48f9c58134b691fb49430a3262fbd808a6b49f07a8c66aacc73d5`。解压成员 UserBehavior.csv，3,672,347,465 字节，成员 SHA256 `46fdd7d389c1ddc7922eb7d9014af5573a4a3045da28c6c46197636873d8f1a9`。完整读取到 EOF，CRC 全通过，headerless CSV 五列校验通过，实际 100,150,807 行，未抽样、未去重、未截断、未静默清洗。源原文仅 tx 私有 `.local/datasets/raw/`，GitHub/飞书只放方法、哈希和聚合统计。

真实用户 ID 去重 987,994，商品 4,162,024，类目 9,439。源时间戳范围 1902-05-07T22:32:46+00:00 至 2037-04-09T05:22:35+00:00（UTC 展示）。异常统计 `{"negative_timestamp": 318, "outside_reference_2017_11_25_to_12_03_asia_shanghai": 55576}`；源时间戳异常原样保留，本轮没有按日期筛除。

页面本轮只返回标题，不能重新读取文件元数据；大小对照采用方案保留的历史页面元数据，不能写成“本轮重新查到页面大小”。初次完整核验与 OLS 诊断 606.281 秒，重新全量统计 pv 并校准概率模型 188.695 秒；附录 C 原文保留，未把“约一亿条（待核实）”改成未测数字。

## 活跃度、热度与行为比例

用户活跃度采用全量每用户事件数的经验直方图，按固定种子独立重采样到合成用户；分位数 `{'p50': 75, 'p95': 279, 'p99': 409}`。没有复制真实用户身份，也没有把订单原来的结构性用户关联伪称活跃度拟合。

| 行为 | 原始事件数 | 全量比例 |
| --- | --- | --- |
| buy | 2,015,839 | 2.012804% |
| cart | 5,530,446 | 5.522118% |
| fav | 2,888,258 | 2.883909% |
| pv | 89,716,264 | 89.581169% |

访问热度优先以 pv 计数，覆盖 4,068,790 个商品、89,716,264 次浏览。原始 OLS log-rank 指数 1.44149359、R² 0.95007559，但概率 CDF 最大差 0.91608961，因此没有将其用于正式事件。最终采用有限支持 Zipf 的最大似然均值方程，指数 0.78318312，原始与拟合排名 CDF 最大差 0.17562205，全支持均值方程残差 0.0000000016；前 1% 商品占浏览 36.101030%，前 10% 占 76.149562%。所有行为合计的 OLS 诊断指数另报 1.46407309，不与 pv 指数混用。此拟合是工作负载近似，不宣称通过幂律假设检验；长尾与头部残差如实保留。

## 重建范围、确定性与导入

重建前已在本群说明范围。只重建 1,000,000 行 users.csv，并在独立 P2 验收库仅更新 commerce.users.activity_events；用户 ID 集与外键不变。新批次引用既有冻结商品、类目、库存、订单、明细、物流、工单、会话、消息 CSV，不复制改写，也不切换主应用。

独立重跑两份完整用户文件，seed=42，SHA256 `d04147a8064b0f8aa3ad0125b4abe71e971848f94b53e870af3339e3fc724943` 相同；包含浏览/加购/购买/收藏的访问事件每份共 1,000,000 条的两份私有离线文件，SHA256 `fe22b8fbf580ef3f77daca1762e5116c966d458c6bd0cd1acc1ecccc750006c7` 相同。旧 OLS 诊断产生的两份尝试文件保留为历史，不导入、不参与最终校验。全部正式冻结引用文件的内容哈希、用户文件及 provenance 校验一致。计时/目录路径不参与数据校验和；不是声称运行时间逐字相同。

事件按拟合的浏览热度在 200,000 个合成 SKU 排名上截断抽样，用户按活跃度加权，行为独立按全量比例抽样。合成 SKU 排名不具有淘宝商品语义，没有学习行为转移/购物会话，不把购买比例当因果转化率；此轮只生成离线数据，不做 S3 缓存压测。

| 实体 | 目标行数 | 本轮数据库实查 |
| --- | --- | --- |
| commerce.products | 200,000 | 200,000 |
| commerce.users | 1,000,000 | 1,000,000 |
| commerce.orders | 2,000,000 | 2,000,000 |
| support.tickets | 1,000,000 | 1,000,000 |
| commerce.messages | 10,000,000 | 10,000,000 |

仅本轮用户活动权重 COPY、更新和精确比对耗时 9.804 秒；含其他全表前后内容哈希及 ANALYZE 的总耗时 165.069 秒，不能与旧全量导入混称一次导入。旧表已有导入记录继续见历史报告，本轮不重报为新导入。全量前后哈希比对 13 张未改动表均一致，用户 ID 全集比对一致，活动权重不匹配 0 行。无已验收商品/订单覆盖、无新增永久表、无数据库切换。

本轮每份新增用户 CSV 10,250,620 字节、每份访问事件 73,126,392 字节；包含只引用而未重新复制的所有 CSV 的逻辑体积 1,944,323,214 字节。实查数据库（含索引/空闲空间）4,814,879,767 字节，数据工作目录 8,113,170,221 字节；工作目录不含 Docker 卷，数据库体积与 CSV 不重复算。


完整交接、版本差异与未完成项见 [本轮报告](reports/p2-userbehavior-2026-10-23/delivery.md)。

## 2026-10-30 ECD 脱敏声明与第三方处理范围补充

本轮重读[作者README](https://github.com/cooelf/DeepUtteranceAggregation/blob/master/README.md)与[论文§4.1，第3745页脚注6](https://aclanthology.org/C18-1317.pdf#page=6)：README未见明确脱敏条款，**论文明确声明数据已脱敏、匿名化并获合作方同意**。此前本项目只记录了未独立完成全面PII验证，现补全上游声明，不能把“未独立验证”解释为“作者没有声明”。

本地完整语料仍未完成全面PII检测/人工复核；未找到明确涵盖独立ZIP的许可证或第三方模型上传授权，文章CC BY 4.0不能单独证明独立数据包的许可。当前第三方处理为`pending_owner_decision`，负责人尚未允许外发；公开再分发未获批准。ECD原文及复用文本不外发、不入仓库；ECD开发银标先限本地规则代理或暂缓，不能用规则代理作最终真值。作者声明、本项目核验和使用授权分开记录。此次50条DeepSeek小样只用P2结构事实加新合成提问，没有读取/上传ECD文本。
