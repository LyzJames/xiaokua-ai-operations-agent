# 真实平台接入参考

此文档面向使用自己开发账号的研究者。离线演示无需配置这些内容；不要把演示账号用于公网服务。真实入口是 `backend.service:app`，离线入口是 `backend.demo`。

## 完整真实复现：先准备自己的研究环境

完整功能需要飞书自建应用、已安装 Shopify 应用的开发店铺，以及可用 DeepSeek API Key。所有凭据只放私有 `.env` 或密钥文件；原型不会替你提供平台账号。

1. 按 README 安装后端依赖并构建前端，将 `.env.example` 复制为私有 `.env`。
2. 保持 `APP_ENV=development`、`PUBLIC_URL=http://127.0.0.1:8000`、`SHOPIFY_TEST_ONLY=true`，填写你自己的平台凭据和 `DEEPSEEK_MODEL`。本机研究优先使用飞书长连接。
3. 启动 `uv run python -m uvicorn backend.service:app --host 127.0.0.1 --port 8000 --no-access-log`，在首次设置页面读取本机 `data/setup.token` 完成组织初始化。该 token 不要公开。
4. 邀请另一个成员作为独立审批人；同一个申请人无法批准自己的业务写入。
5. 自己拥有并已安装应用的开发店铺，可在“平台连接”导入本机 Client Credentials 配置。面向其他商户的安装使用后文 OAuth，需要平台认可的 HTTPS 回调。
6. 导入飞书连接或填写组织机器人配置，选择长连接。把核实过的 Open ID 绑定到组织成员，再启动 SDK 长连接进程；平台发布配置见后文。
7. 先运行只读连接检查，再在飞书发“同步订单与库存”，在工作台查看任务与同步结果。
8. 使用明确的测试订单和带测试标签的商品，依次提出库存、说明、履约、物流、取消或测试退款方案，由另一成员审批并回读。只读探针不会代替这些业务操作。
9. 导入合成客服样例、生成并核对草稿、启用知识；发送“分析德国客户的能效咨询”。真实工作台使用你的模型密钥，报告区分模型分析和规则回退，并展示证据、缺口和执行记录。

没有要求租用服务器。本机 Client Credentials 研究入口仅适用于平台允许的自有店铺应用；如果平台授权方式要求 HTTPS，请按其规则配置自己的回调。飞书长连接能在本机接收消息与卡片。

```bash
uv run python scripts/verify_integrations.py --platforms-readonly --model
```

探针只读平台身份/订单访问，用合成资料测试模型，不发送消息或业务写入。输出只含状态和安全错误码；不会输出店铺、App ID、成员身份或 token。

## Shopify

1. 创建你自己的 Shopify 应用并安装到开发店铺。根据应用用途选择分发方式；公开分发、受保护客户数据许可及商店审核不是代码可以自动替代的步骤。
2. 在后端私有 `.env` 配置 Client ID、Client Secret 与固定 `PUBLIC_URL`。工作台采用独立应用的授权码 OAuth，不是嵌入式 Admin 应用。
3. 在平台配置 App URL 与 `/api/shopify/callback` 的 HTTPS 回调，使用 [通用 TOML 模板](../deploy/shopify.app.toml.example) 检查访问范围与事件订阅。模板中的 `ops.example.com`、`YOUR_SHOPIFY_CLIENT_ID` 都是占位符。
4. 组织负责人在“平台连接”输入自己的 `your-store.myshopify.com` 并授权。平台连接及同步结果归属当前组织；OAuth 状态、HMAC、域名和租户关系都由后端校验。
5. 订单访问可能需要在应用平台的 Protected customer data access 中说明用途并保存；公开应用还需要相应审查。本项目不查询客户姓名、邮箱、电话或地址。

权限包括订单、产品、库存、位置及商户管理的履约订单。默认查询近 30 天订单，不需要为了这个范围申请 `read_all_orders`。真实 API 使用 GraphQL Admin API `2026-10`，后续版本需自行复核字段与弃用情况。

只针对自己组织已安装的开发应用，工作台本机入口提供 Client Credentials 导入；已有 Admin API token 可用于只读连接探针。这不能替代面向所有商户的 OAuth。服务端不把 token 返回给前端。

### 安全验证顺序

保持 `SHOPIFY_TEST_ONLY=true`，先验证只读同步，再使用 `order.test=true`、成功收款交易 `transaction.test=true` 的测试订单，以及带 `xiaokua-test` 标签的商品。退款必须满足测试交易和可退金额检查，所有写入仍需另一成员审批。不要把普通真实订单当成测试订单。

履约不发客户通知；物流更新只修改履约单信息；取消不自动退款，退款另行提出。库存使用 compare-and-set；旧数量变化会拒绝执行。外部写入中断或结果未知会转人工核实，禁止自动重复发送。

工作台“刷新数据”读取本地快照；点击订单/库存“同步”才能得到最新平台快照。结果待核实的审批记录提供“查询平台”，用于人工核实结案，包括平台异步取消结果。

本项目此前完成开发店铺 API 测试链路；没有对 Checkout 测试网关界面流程作验收。公开报告只保留结果摘要，不公开店铺地址、账号和资源标识。

## 飞书自建应用

每个组织使用自己的应用和独立连接。无需扩大通讯录访问范围来演示机器人。

1. 在飞书开放平台开启机器人能力，在测试范围发布应用。
2. 申请 `im:message:send_as_bot` 与 `im:message.p2p_msg:readonly`，配置接收消息 `im.message.receive_v1` 与现代卡片回传 `card.action.trigger`，发布版本使配置生效。
3. 负责人在工作台配置 App ID、App Secret 和接入方式，建议填写 Tenant Key。
4. 成员通过飞书授权绑定身份，或由负责人把经核实的 Open ID 绑定到对应组织成员。只有启用的成员能提交任务，只有负责人/审批人能审批，批准时还必须与申请人不同。

### 官方 SDK 长连接

在工作台选择长连接；不需要公网消息回调的 Verification Token 或 Encrypt Key。另开终端保持 SDK 进程：

```bash
uv run python -m backend.production.feishu_ws --connection-id YOUR_FEISHU_CONNECTION_ID
```

在飞书后台分别给“事件配置”和“回调配置”选择长连接接收，SDK 连接后保存，再发布应用。App Secret 仍需后端私密保存，不能写入网页。

长连接把事件写入数据库；任务 worker 与连接进程是不同职责。连接停用、接入方式改变或成员撤权后，后端重新校验权限。当前部署示例提供一个组织连接槽，多组织需分别启动连接进程。

### HTTPS 签名加密回调

使用固定 HTTPS 地址 `/hooks/feishu/CONNECTION_ID`，填写 Verification Token 和 Encrypt Key。后端校验 AES 信封、签名、时间戳、应用/租户身份、成员权限和事件去重；长连接模式不能从此地址接收事件。

这一路径有自动化签名测试，公开交付没有公网真实投递验收。成员 OAuth 回调仍为 `/api/feishu/callback`。聊天启动同步与分析；业务写入必须先在工作台提出结构化方案。

## DeepSeek 与结构化分析

在私有配置设置 `DEEPSEEK_API_KEY` 与服务商实际开放的 `DEEPSEEK_MODEL`。默认 `DEEPSEEK_ANALYSIS_FORMAT=json`，请求稳定 Chat Completions 的 JSON 对象；可以改为 `strict_tools`，调用官方 Beta `/beta/chat/completions` 的 strict function schema。更改 `.env` 后重启服务。

两种模式都在本地检查 `finding`、`evidence`、`limitations` 的类型、长度、多余字段和证据归属。strict 模式的 `submit_analysis` 只用于承载结果，不执行工具。Beta 不接受的长度限制由本地校验器实现。非法输出、鉴权失败和限流等原因会展示为明确的规则回退，不能视作真实推理成功。

每个角色最多两次请求、每次最多 1400 输出 tokens；只对短暂故障作有限重试。工作台显示服务商提供的输入/输出 tokens，没有价格或账单保证。模型内容不能批准订单或其他业务写入。

离线 `backend.demo` 始终使用规则模式。真实 `backend.service` 才调用研究者的模型；模型费用由自己的账号承担，不需要另行购买编排或记忆云服务。

模型探针只用合成输入。实际运行时，导入知识和客服资料应先去除个人信息；模型角色只接收受限上下文，运行结果仍属于组织私密资料。

## 隐私事件

Shopify 客户数据请求/删除请求进入负责人“运行设置”的人工处理队列。只保存关联订单标识，不持久化原始客户身份。清除时删除相关快照和操作内容，并阻止自动同步重新写入；正在执行或结果未知的操作先核实。

财务审计保留。导入客服记录、模型报告、其他系统与适用留存/披露义务仍需负责人逐项核对；人工“已核对”不代表自动完成全部法律义务。

## 官方资料

- [Shopify 独立应用认证](https://shopify.dev/docs/apps/build/authentication-authorization/authenticate-standalone-apps)
- [Shopify 受保护客户数据](https://shopify.dev/docs/apps/launch/protected-customer-data)
- [Shopify Webhook 订阅](https://shopify.dev/docs/apps/build/webhooks/subscribe)
- [Shopify GraphQL Admin API](https://shopify.dev/docs/api/admin-graphql/2026-10)
- [飞书创建消息](https://open.feishu.cn/document/server-docs/im-v1/message/create)
- [飞书官方 Python SDK 长连接样例](https://github.com/larksuite/oapi-sdk-python/blob/v2_main/samples/ws/sample.py)
- [飞书 Open ID 获取](https://open.feishu.cn/document/faq/trouble-shooting/how-to-obtain-openid)
- [DeepSeek API](https://api-docs.deepseek.com/)

结构化接入依据：[DeepSeek JSON Output](https://api-docs.deepseek.com/guides/json_mode/)、[strict tool calls](https://api-docs.deepseek.com/guides/tool_calls/)。
