# 小跨 · 全链路 AI 运营官研究原型

[![Research prototype checks](https://github.com/LyzJames/xiaokua-ai-operations-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/LyzJames/xiaokua-ai-operations-agent/actions/workflows/ci.yml)

一个把运营目标变成可追踪任务、把经验变成可复用技能的 AI Agent 参考实现。工作台连接飞书与 Shopify，围绕**消息 → 持久任务 → 操作方案 → 独立审批 → 执行 → 回读 → 审计**构建闭环。

**用途：技术研究、教学、二次开发。** 完整真实复现需要你自己的飞书应用、Shopify 开发店铺与模型 API Key；也提供无需密钥的离线演示，用于先验证业务逻辑。公开版本包含真实平台适配代码，但不包含任何实际账户的凭据、订单、成员资料或平台截图。Amazon、Meta Ads、采购支付为后续扩展方向。

[分类测试报告与流程截图](docs/TEST_REPORT.md) · [真实平台接入](docs/INTEGRATIONS.md) · [架构与设计取舍](docs/RESEARCH.md) · [隐私发布说明](docs/PRIVACY.md) · [本轮技术调研与优化](docs/TECH_REVIEW.md) · [MIT 许可证](LICENSE)

![合成订单工作台](docs/images/04-synced-orders.jpg)

## 已实现的闭环

| 能力 | 当前实现 | 研究边界 |
| --- | --- | --- |
| 办公入口 | 飞书消息、现代卡片批准/拒绝、任务结果通知；官方 SDK 长连接或签名加密 HTTPS 回调 | 演示台替代真实飞书消息投递；真实接入需自己的自建应用 |
| 订单与商品 | Shopify 订单/规格同步、库存比较并设置、纯文本商品说明更新 | 默认只允许测试订单与带 `xiaokua-test` 标签的商品写入 |
| 订单处理 | 履约、物流信息更新、取消、原交易退款 | 取消不自动退款；未接入承运商轨迹；Checkout 网关未纳入本次验收 |
| 人工审批 | 操作前后差异、独立成员审批、24 小时有效期、执行前再校验 | 所有业务写入需审批，包括小额退款；没有自动消费额度或真实 AI 钱包 |
| 持久任务 | SQL 队列、租约、心跳、进程恢复、事件去重、只读任务重试 | 外部写入结果未知时转人工核实，禁止盲目重发 |
| 多角色分析 | LangGraph 原生并行节点、角色上下文隔离、SQL 步骤恢复、结构化输出与证据校验 | 没有生产记录或广告平台时，不推断制造根因或暂停真实广告 |
| 经验沉淀 | CSV/JSON 客服记录导入、按市场/主题生成带证据的技能草稿、人工启用、Markdown 导出 | 规则识别需至少 5 条同类记录；市场与主题匹配后按需加载，支持标准 Agent Skills 技能包导出 |
| 权限与审计 | 组织隔离、负责人/审批人/运营/只读角色、会话与 CSRF、凭据加密、追加式哈希审计链 | 数据库管理员仍有系统级控制；哈希链不是外部公证或合规认证 |
| 部署参考 | SQLite 本机模式、PostgreSQL、非 root Docker、HTTPS/备份示例 | 未进行公网、商店分发、容量或商业 SaaS 验收 |

## 快速开始：离线研究演示

需要 Python 3.11–3.13、[uv](https://docs.astral.sh/uv/getting-started/installation/) 与 Node.js 22。首次安装依赖需访问包源；启动后的演示不调用模型、Shopify 或飞书 API。

```bash
git clone https://github.com/LyzJames/xiaokua-ai-operations-agent.git
cd xiaokua-ai-operations-agent
uv sync --frozen
cd frontend
npm ci
npm run build
cd ..
uv run python -m backend.demo
```

Windows PowerShell、macOS 和 Linux 均使用上面的逐行命令。打开[本机演示工作台](http://127.0.0.1:8003)。

| 合成账号 | 角色 | 演示密码 |
| --- | --- | --- |
| `owner@example.invalid` | 负责人、提出业务方案 | `demo-only-local-123` |
| `approver@example.invalid` | 独立审批人 | `demo-only-local-123` |
| `operator@example.invalid` | 运营人员 | `demo-only-local-123` |

这些账号和密码是公开演示常量。演示入口只监听 `127.0.0.1`，不可作为公开服务器的登录方案。

演示数据库与合成店铺状态位于 `data/research_demo/`。重启保留任务、库存和审计。要重新开始，使用一个新目录，避免覆盖已有数据：

```bash
uv run python -m backend.demo --data-dir data/my-fresh-demo --port 8003
```

演示入口拒绝复用没有演示标记的已有数据库，并阻止真实平台授权、凭据导入及外部回调。前端页顶明确标注合成数据。登录工作台后打开[飞书事件模拟台](http://127.0.0.1:8003/demo/feishu)；两个页面共享当前成员会话。

## 按这个顺序体验完整流程

1. 用负责人登录，打开飞书事件模拟台，发送“小跨，同步昨晚的欧洲订单与库存”。点击“刷新消息”，看到任务接收和完成通知；工作台出现 2 笔合成订单、1 个商品规格。
2. 在“商品与库存”申请把库存从 10 改为 15。在“审批中心”审查差异；申请人不能批准自己的方案。
3. 退出负责人账号，用审批人登录，再刷新模拟台。点击库存卡片的“批准”，点击商品“同步”后工作台回读库存为 15。重复点击同一卡片不会再次执行。
4. 再用负责人提出商品说明更新，由审批人“拒绝”；商品内容保持原值。订单页面还可以体验 0.50 USD 合成退款、履约、物流更新与取消，分别审批。
5. 在“记忆与技能”导入 [合成客服 CSV](examples/support-synthetic.csv)，点击“学习并生成草稿”。德国能效和法国本地退货形成未启用草稿；核对证据后启用，可下载 Markdown 技能文件。
6. 发送“分析德国客户的能效咨询”。三个角色依据已同步数据与已启用知识生成报告；德国问题不会引用法国的退货草稿。
7. 在“审计日志”查看审批、执行、回读和技能启用记录，检查哈希链并导出 CSV。在“运行设置”查看队列和任务状态。

逐步截图、具体断言、失败路径与真实平台验收范围见 [测试报告](docs/TEST_REPORT.md)。

## 架构

```mermaid
flowchart LR
    UI[React 工作台] --> API[FastAPI / 会话 / RBAC / CSRF]
    FEI[飞书消息与卡片] --> EVENT[事件认证与去重]
    EVENT --> SQL[(SQL 持久事件 / 任务 / 作业)]
    API --> SQL
    SQL --> WORKER[Worker / 租约 / 恢复]
    WORKER --> READ[同步与三角色分析]
    READ --> GRAPH[LangGraph / 并行角色 / 汇总]
    WORKER --> PLAN[结构化操作方案]
    PLAN --> REVIEW[独立成员审批 / 24h]
    REVIEW --> RECHECK[权限与业务状态再校验]
    RECHECK --> WRITE[Shopify 写入与回读]
    WRITE --> AUDIT[追加式审计链]
    WRITE --> NOTICE[飞书结果通知]
    GRAPH --> MEMORY[带证据知识 / 技能 Markdown]
```

0.3 主入口由 SQL 持久化业务状态和任务队列。LangGraph 使用订单、库存、客服知识三个独立并行节点，汇合后生成报告。角色只接收本职责所需数据，引用必须属于其证据范围。`analysis_steps` 保存输入指纹和已完成输出，同一只读任务恢复时可以复用成功角色；数据或知识改变后重新分析受影响部分。它是应用层步骤日志，不是 LangGraph checkpointer。

默认模型格式为 JSON + 本地 Pydantic 严格校验，可用 `DEEPSEEK_ANALYSIS_FORMAT=strict_tools` 选择 DeepSeek Beta 的结构化 tool calls；这只是输出格式，不执行模型工具。每角色最多两次请求，只有短暂故障重试，失败原因和规则回退明确展示。模型只提供分析；业务写入始终经过独立审批。

知识先筛选元信息，再按市场与主题加载最多四条正文。导出的技能包包含合法目录名、SKILL.md 和引用证据。没有引入 CrewAI、ContextDB、MCP 或 A2A 服务；选择依据和后续研究方向见 [技术调研](docs/TECH_REVIEW.md)。历史 `backend/engine.py` 保留独立 0.1 checkpoint 实验，不是当前平台业务队列。

离线模式通过依赖注入把 Shopify 换成持久化 `DemoShop`，把飞书发送换成本地 outbox，复用正式审批和 worker。分析强制规则模式，即使本机已有密钥也不调用模型。真实模式使用你的 DeepSeek API Key；`DEEPSEEK_MODEL` 填写服务商实际开放的标识。

## 自己连接真实开发账号（可选）

先完成离线演示，再参考 [接入文档](docs/INTEGRATIONS.md)。真实入口与演示入口分开：

```bash
# 将 .env.example 复制为 .env，并填入你自己的配置；不要提交 .env。
uv run python -m uvicorn backend.service:app --host 127.0.0.1 --port 8000 --no-access-log
```

首次本机访问会显示初始设置流程。负责人创建组织，邀请另一成员承担独立审批；在“平台连接”完成 Shopify OAuth 与飞书连接。Webhook/OAuth 回调需满足平台的公网 HTTPS 配置要求，飞书消息也可使用官方 SDK 长连接。

```bash
uv run python -m backend.production.feishu_ws --connection-id YOUR_FEISHU_CONNECTION_ID
```

保持 `SHOPIFY_TEST_ONLY=true`。真实平台仅允许你自行授权的开发店铺和测试资源；**离线 demo 的成功不等于平台已授权或真实 API 已验收**。公开仓库不附带可用的 API key。研究者自行接入模型会发生服务商计费。

## 测试与复现

验收按能力归为五类；当前优化版本的分类回归全部通过。详细用例范围、界面图片和真实账号检查见 [测试报告](docs/TEST_REPORT.md)。

| 验收类别 | 验证重点 | 结果 |
| --- | --- | --- |
| 业务闭环 | 消息触发同步、库存、商品说明、订单履约/物流/取消/测试退款 | 通过 |
| 权限与安全 | 组织隔离、独立审批、CSRF、测试保护、去重、不可改审计 | 通过 |
| Agent 分析 | 原生角色并行、上下文隔离、Schema 和引用校验、有限重试与回退 | 通过 |
| 记忆与技能 | 带证据学习、草稿核对、地区/主题匹配、加载预算、标准技能包 | 通过 |
| 恢复与发布 | SQL 队列与角色恢复、PostgreSQL 并发、前端构建、公开文件隐私检查 | 通过 |

默认本机回归和完整合成业务验收：

```bash
uv run pytest -q --category-report data/test-categories.json
uv run python scripts/summarize_tests.py data/test-categories.json
uv run python scripts/demo_acceptance.py
cd frontend
npm run build
cd ..
```

默认回归使用 SQLite；未提供 PostgreSQL 时，分类报告将相应类别标为部分运行，不把未运行的数据库检查视为通过。要复现包含 PostgreSQL 的完整分类验收，需要本机 Docker：

```bash
uv run python scripts/start_test_postgres.py
uv run python scripts/run_postgres_tests.py --full --category-report data/test-categories.json
uv run python scripts/summarize_tests.py data/test-categories.json
docker stop xiaokua-postgres-test
```

你的平台账号与模型只读检查（不发消息、不写订单；模型请求使用合成内容，服务商会按自己的规则计费）：

```bash
uv run python scripts/verify_integrations.py --platforms-readonly --model
```

连接检查不是完整平台业务验收。真实消息、身份绑定、独立审批和 Shopify 测试资源的复现步骤见 [接入指南](docs/INTEGRATIONS.md)。公开仓库不包含真实平台截图或私有测试数据。

[GitHub Actions](https://github.com/LyzJames/xiaokua-ai-operations-agent/actions/workflows/ci.yml) 自动运行分类回归、一次性 PostgreSQL、合成闭环、前端构建和公开文件检查，使用合成数据，不需要真实平台 secret。仓库顶部徽章显示最新提交状态。

## 目录导航

```text
backend/demo.py              无凭据、只监听本机的演示入口
backend/demo_shop.py         持久化合成店铺适配器
backend/production/api.py    工作台、权限、连接、隐私请求接口
backend/production/workflows.py  提案、审批、执行与知识学习
backend/production/analysis.py   原生图角色分析与 SQL 步骤恢复
backend/model_runtime.py         有限模型请求、严格格式与引用校验
backend/production/skills.py     按需检索与 Agent Skills 导出
backend/production/worker.py SQL 队列、租约、事件及恢复
backend/production/shopify.py    GraphQL Admin API 适配器
backend/production/feishu*.py    消息、卡片、认证与官方 SDK 长连接
frontend/src/production.tsx React 0.3 工作台
frontend/demo/              明确标注合成数据的事件模拟台
examples/                   仅含合成的研究输入
scripts/                    演示验收、发布检查、备份与部署参考
tests/                      自动化测试
migrations/                 数据库迁移
docs/images/                基线与本轮优化的合成流程截图
deploy/                     自行研究部署时的通用模板
```

## 常见问题

- **页面提示“请先构建前端”？** 在 `frontend` 执行 `npm ci` 与 `npm run build`，再刷新页面。
- **消息发出后没有结果？** Worker 异步处理；稍后点击“刷新消息”和工作台“刷新数据”。查看“运行设置”中作业状态。
- **审批按钮不可用？** 确认当前账号是独立审批人，并且申请未过期。演示台与工作台共享同一个登录会话，切换成员后刷新模拟台。
- **技能草稿没有生成？** 每个市场/主题至少需要 5 条记录；同标题草稿重复学习不会重复创建。导入示例 CSV 可复现。
- **是否已经能作为商业 SaaS？** 这里交付研究原型和接入代码；平台商店分发、法律审查、生产容量、真实采购支付及公网试运营均不在交付范围。

## 隐私与贡献

请只用合成数据提交 Issue、截图与 PR。不要上传 `.env`、`Key.txt`、数据库、会话、备份或实际平台导出。发布扫描脚本是辅助检查，不能替代对新增图片和业务数据的人工审查。详见 [隐私说明](docs/PRIVACY.md)。

欢迎通过 GitHub Issue 和 PR 讨论研究改进。修改业务执行流程时，请保留独立审批、执行前校验和结果未知时停止自动重试的约束。

## 许可证

[MIT](LICENSE)。允许修改、再分发和商业使用，须保留版权与许可声明。第三方库、平台 API 和账号使用仍受各自条款约束。
