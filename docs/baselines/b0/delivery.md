# agent-ops-platform 首次交付：修复与 B0

日期：2026-10-18（北京时间）；执行：Codex。代码基线：`baseline-b0` / `d37c7d34e80f88ca345b074821531ba8e0ab303b`。

## 范围先说明

本次按用户明确描述“修已知错误并建立B0，首次交付后再决定后续”执行。原附件P0/P1实际是环境栈／PostgreSQL数据层，且没有B0阶段；已发起范围澄清。本报告不能当作附件P0/P1全部完成。后续环境栈、数据库持久化、前端、商品、支付、模型网关等等待审阅后的指示，不提前推进。

附件原件在 `docs/agent-ops-platform面试级完善方案.md`，44746字节，保留原作者`@sh7`与原署名日期；来源及SHA256见source-document.json。计划原件未修改，解释另写在这里。

## 修复与证据

1. 英文人工关键词原按子串匹配，humanitarian、inhuman、humanoid误转人工。增加单词边界，明确human/fraud/privacy仍转人工，中文优先级及原47条夹具保留。没有用后续S5最终评测语料调规则。
2. 内部memory HTTP客户端继承终端代理，坏代理会在构造阶段导致500，未进入记忆降级。改为trust_env=False直连内部服务，身份、2秒超时和409传播语义保持。
3. 旧measure-streams.py每个流创建Dispatcher且未显式关闭，复跑覆盖固定历史文件。改为共享Dispatcher+finally关闭，默认.local并拒绝覆盖已有输出；旧2026-10-02原始数据不修改。
4. 方案S4的“无并发上限”与代码不符：已有Semaphore默认8槽，可配置1–64，总预算含等槽与重试。B0保留既有保护，不故意劣化。缺少的入口排队上限、熔断等留后续阶段。

原74测试全过；新增7回归在原始1c5d1ef独立worktree上4失败/3通过，修复后7/7。扩展后pytest81通过、0跳过（含真实PostgreSQL memory联调）、ruff通过、strict mypy14文件通过；固定客服回归47/47，只作回归不作真实准确率。旧流脚本32健康流完成，慢流被取消。详细见regression-proof.json、gates.json和support-regression.json。

## B0怎样测

```bash
bash scripts/bootstrap.sh
uv run python scripts/benchmark-b0.py --output .local/b0-my-run
uv run pytest -q
uv run ruff check src tests scripts/benchmark-b0.py scripts/measure-streams.py
uv run mypy --strict src
uv run python -m agent_ops.evals.support_runner --output .local/my-support-regression.json
```

原始版本冻结`baseline-v0=1c5d1ef`；修复后`baseline-b0=d37c7d3`，标签不随结果文档提交移动。采样时工作树干净，所有源码、脚本和锁文件SHA256已回查一致。环境、依赖版本及配置见results/environment.json。

每轮新起一个127.0.0.1单worker Uvicorn，客户端CPU0、API允许CPU1/2/3；同机共享内核/内存带宽，不称完全隔离。确定性planner、本地demo商品、memory关闭、内存工单、trace ratio1/exporter none，无付费模型/模型桩。预热每轮commerce/SSE/support各10次，support预热重放同key。

六场景每轮各128请求、并发8、五轮，共3840次。create检查状态与revision；replay/get检查原ticket_id；conflict同key不同body必须409；导购检查实际商品、outcome和trace关联。SSE经真实TCP逐行读取，不用ASGI缓冲结果冒充首帧；首事件started不是模型首token。

延迟从客户端Semaphore放行开始，含连接池等待和响应解析，不含发压待发积压。闭环负载、固定场景顺序、未随机；每场景每轮仅0.206–0.244秒。内存工单上限256保持，不能扩大新建数量后悄悄改实现。近尾噪声明显，此B0是首次工程复现点，不是长时间稳态容量或可靠p99承诺；更长窗口与模型桩属于后续工作。

## 五轮结果

每格是五轮相应指标的中位数；括号是五轮最小–最大，不是置信区间。分位数用最近秩。

| 场景 | p50 ms | p95 ms | p99 ms（范围） | req/s（范围） |
|---|---:|---:|---:|---:|
| commerce | 10.86 | 21.06 | 28.93（25.85–45.06） | 608.7（573.4–614.8） |
| sse | 12.12 | 23.70 | 29.99（26.14–39.32） | 589.1（525.2–619.9） |
| ticket_create | 11.24 | 28.42 | 100.20（36.09–137.50） | 556.8（542.7–598.0） |
| ticket_replay | 11.13 | 29.33 | 33.78（23.09–37.03） | 592.8（557.4–616.4） |
| ticket_conflict | 11.01 | 29.81 | 35.78（33.15–37.75） | 574.1（562.5–580.4） |
| ticket_get | 10.21 | 28.02 | 31.99（27.71–127.61） | 593.2（571.2–609.5） |

3840/3840契约符合：640次预期409、3200次预期200，异常结果0，不把409当故障。所有原始请求观察在results/raw.json，逐轮在summary.json，跨轮在aggregate.json，固定输入在inputs.json。没有性能提升结论，不选最好一轮。

五轮采样阶段服务CPU总计约3.45秒；单进程观测峰值RSS最大51952KiB（约50.7MiB）。VmHWM是整个进程累计峰值，不是场景新增量，也不含发压程序与其他服务；CPU tick分辨率也限制短场景的解读。

## 仍然保留的限制与下一步审阅

内存工单容量256、重启丢失、幂等仅单进程；无客户身份隔离；固定DAG不等同自主规划；没有百万工单、万连接、独立路由质量、真实支付或新环境栈证据。本次不提前完成后续阶段，等用户审阅决定。完整B0复现口径见同目录README。
