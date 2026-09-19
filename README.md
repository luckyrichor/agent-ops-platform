# agent-ops-platform

Agent 应用与可观测评测平台。电商场景的多 Agent 任务拆解、工具调用、高并发流式处理，配套追踪与评测体系。

**状态：未开工**（2026-09-19）

## 对应岗位

05 字节 Agent 应用开发、07 字节 LLM Agent（TRAE）、10 米哈游 Agent（数据方向）、11 米哈游 后端（AI 游戏方向）、14 拼多多 电商 Agent 后端。岗位原文见 [workplan-docs](https://github.com/luckyrichor/workplan-docs)。

这是**目标关联**，不代表单个电商演示自动覆盖上述岗位的全部要求。要对应 10 的数据 Agent 能力，仍需数据资产或语义层实践；要对应 07 的研发工具要求，仍需开发者产品方向的证据。

## 重点

不做 Agent 入门演示，直接压 JD 真正区分人的地方：

- **可观测**：多 Agent 任务拆解后一条请求散成调用树，trace 如何串联、出错时如何定位到具体某一跳的工具调用
- **高并发流式**：SSE/WebSocket 的背压、慢消费者隔离、取消传播
- **评测**：可复现的评测集与判定口径，而不是看几条输出下结论
- **集成 `agent-memory`**：作为记忆服务的第一个真实调用方，打通接口时序、失败降级与幂等

## 依赖

通过 HTTP API 调用 [`agent-memory`](https://github.com/luckyrichor/agent-memory)。两者保持独立边界：`agent-memory` 提供记忆能力，本项目负责业务任务与平台能力。

## 技术栈

Python（暂定，与 `agent-memory` 一致以便共用 SDK 和部署方式）。具体选型待技术方案确定后写入本节。
