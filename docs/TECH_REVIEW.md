# 0.3 技术调研与优化决策

调研日期：2026-10-12。目标是可审查、可复现的飞书 + Shopify 运营 Agent 研究原型；完整平台复现使用研究者自己的账号和模型 API Key。没有引入付费云端编排、向量数据库或真实采购支付。

## 选择依据

| 技术方向 | 官方现状与用途 | 本项目决定 |
| --- | --- | --- |
| LangGraph 原生图编排 | 同一 super-step 可并行执行节点，通过 reducer 合并状态；支持显式工作流 | 采用三个独立角色节点及汇合边；锁定的版本为 1.2.14，减少重复封装线程池 |
| 持久执行与人工审批 | 图状态持久化与外部副作用需要分别设计恢复和幂等 | 业务审批继续由 SQL 事务管理；新增角色步骤日志，保留成功结果以支持只读任务恢复 |
| 结构化模型输出 | JSON 格式不等于符合业务 Schema；DeepSeek 提供 Beta strict tool calls | 默认稳定 JSON + 本地严格校验；可选 strict 模式，仍做本地校验，不执行模型返回的工具 |
| Agent Skills | 标准技能目录包含带 name、description 的 SKILL.md，正文和引用资料按需加载 | 采用标准 Markdown 与目录 ZIP 导出；先筛选知识元信息，再加载匹配正文 |
| MCP | 2026-07-28 规范提供工具/资源互操作，长任务转为独立扩展 | 本轮暂不接入：没有外部 MCP 服务需求；未来适配仍需沿用组织身份、审批与审计 |
| A2A | 面向独立 Agent 服务之间的发现与任务交互 | 三个角色在同一进程，暂不引入跨服务协议和额外认证层 |
| Agent 可观测性 | 耗时、调用量和 token 有助于定位失败；提示与回答可能包含私密资料 | 本地记录模式、安全错误码、耗时、服务商提供的 token 用量与结果复用；不增加云端日志服务 |

依据：[LangGraph 图与 reducer](https://docs.langchain.com/oss/python/langgraph/graph-api)、[工作流模式](https://docs.langchain.com/oss/python/langgraph/workflows-agents)、[版本资料](https://pypi.org/project/langgraph/1.2.14/)、[持久化](https://docs.langchain.com/oss/python/langgraph/persistence)、[结构化输出](https://docs.langchain.com/oss/python/langchain/structured-output)、[DeepSeek strict 模式](https://api-docs.deepseek.com/guides/tool_calls/)、[Agent Skills 规范](https://agentskills.io/specification)、[MCP 正式发布](https://blog.modelcontextprotocol.io/posts/2026-07-28/)、[A2A 规范](https://a2a-protocol.org/latest/specification/)、[OpenTelemetry GenAI 资料](https://opentelemetry.io/docs/specs/semconv/registry/attributes/gen-ai/)。

## 落地改进及可验证效果

**角色上下文隔离。** 订单角色只接收订单统计和允许引用的快照标识；库存角色接收库存统计；客服角色只拿匹配的已启用知识。角色输出按固定顺序汇总，工作台展示进度、证据缺口和执行记录。并行测试使用三个角色的同步屏障，验证原生图实际并发执行。

**恢复有依据。** `analysis_steps` 与业务数据库共用 SQLite/PostgreSQL。每个角色保存输入指纹与已完成输出，同一任务恢复时复用相同输入的成功结果；统计、知识版本、模型或响应模式改变时，受影响角色重新计算。模型回退结果会重新尝试。它是应用层步骤日志，**不是 LangGraph checkpointer，也不保存整个图状态**。若模型已响应但进程尚未保存结果，恢复仍可能再次调用模型；不宣称外部调用恰好一次。

**结构化结果经过两层检查。** 模型只能返回 `finding`、`evidence`、`limitations`。本地 Pydantic 严格校验类型、长度和多余字段；引用只能来自本角色的证据清单。有证据却不引用、返回未知引用、输出截断或试图添加退款字段，都会采用明确标注原因的规则回退。引用检查证明来源在允许范围，不能证明自然语言结论必然正确。

**限制请求规模。** 每个角色最多两次模型请求，只有短暂网络错误或可短等待的限流允许重试；每次最多 1400 输出 tokens，整个分析图每轮最多六次请求。鉴权、请求格式和输出校验失败不自动修复重试。超时控制作用于 HTTP 阶段，不能当作整张图的绝对总时限。用量仅记录服务商报告的值，不是账单或费用上限。

**知识按需加载。** 检索先读标题、市场和短摘要，市场与主题必须同时匹配，最多加载四条、每条最多 1600 字符。正文内容和证据版本参与结果指纹。禁用知识后不会继续复用此前客服角色结果。当前只支持已定义的市场别名和能效/退货主题，不宣称通用语义检索。

**技能可以跨工具审阅。** 下载的 SKILL.md 使用合法 ASCII 名称、用途说明和字符串元信息；技能包提供同名目录以及 `references/evidence.md`。没有脚本执行能力或预授工具权限。草稿状态保留，导出不表示自动启用。示例见 [标准技能目录](../examples/skills/de-energy-response/SKILL.md)。

**测试结果服务于验收。** 自动化报告按业务闭环、权限与安全、Agent 分析、记忆与技能、恢复与发布分类；缺少 PostgreSQL 时明确标为部分运行。CI 也生成同样的分类摘要。公开结果不包含测试参数、异常文本、凭据、机器目录或真实平台标识。

## 真实模式与研究边界

本轮使用授权账号验证了 Shopify 只读访问、飞书应用认证，并分别对 DeepSeek JSON 与 strict 模式发送合成内容，验证响应格式与引用检查。机器可读摘要见 [连接检查](live-readonly-check.json) 与 [strict 检查](live-strict-model-check.json)。没有重新发送平台消息或执行业务写入。

这些检查不替代飞书消息/卡片投递、Shopify 测试订单闭环或开放问题的模型质量评估。完整真实复现流程见 [接入指南](INTEGRATIONS.md)；基线与本轮证据范围见 [分类测试报告](TEST_REPORT.md)。

后续优先方向是经过同意的合成质量评测集、更多市场/主题、平台 API 版本迁移和容量研究。接入 MCP/A2A、语义记忆或其他模型时，应先提出明确的复现需求，再为新的接口增加身份、数据边界和失败行为测试。
