# M5 客服处理与可复现评测

接口 POST /support/requests，body={message,order_id?}，Idempotency-Key必填；GET /support/tickets/{id}；POST /support/tickets/{id}/resolve，body={expected_revision}，需要Bearer SUPPORT_HUMAN_TOKEN（未配置时禁止人工完成）。响应/失败沿用trace_id与采样标记；support.route、support.order_lookup、support.human_resolve形成调用路径。

四类路由：订单状态、退换退款请求、转人工、澄清。物流+退款混合意图/否定/未知问题要求澄清；投诉/隐私/欺诈/明确人工优先转人。退款不自动执行资金操作；只读订单工具使用demo-100/demo-200固定夹具，缺订单号或未知订单要求补充。人工token仅是本地演示操作门，不是生产账户/RBAC体系。

工单状态needs_information/waiting_human/resolved；人工完成需当前revision且原状态waiting_human。同key同body返回原始响应，即使后来人工改变状态；GET查看最新状态。同key不同body返回409。锁内原子提交、容量256，满返回503，启动重置；不是持久工单服务或租户隔离系统。只存结构化路由/状态/事件序列和幂等摘要，不保存用户原话，不把原话写入trace。当前GET没有身份控制，只供本地开发，不开放生产流量。

评测：`uv run python -m agent_ops.evals.support_runner --output .local/support-results.json`。47个固定中英文标注，10人工/10退款/12订单/15澄清，判定需route和工单state同时正确，输出每条结果、路由混淆矩阵、数据SHA256，失败非零退出。包含同类改写、缺/未知订单、混合意图、否定、指令干扰与英文子串负例。该版47/47是固定夹具工程验收，未称 held-out 或真实客服准确率；规则路由不是LLM推理。真实固定DAG的30次provider延迟评测仍独立保留，不作为客服决策能力证据。

岗位08只覆盖后端会话输入→路由→工具/工单→人工接管路径和追踪/评测。没有前端、真实OMS、生产客服工作台、客服鉴权和持久审计，明确不覆盖全栈要求。

memory依赖已固定到M6提交1e2730c；SDK/源码迁移一致后实际集成测试运行，不将旧SDK与新迁移混测。
