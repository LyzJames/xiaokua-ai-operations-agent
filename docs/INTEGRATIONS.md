# 真实平台接入参考

此文档面向使用自己开发账号的研究者。离线演示无需配置这些内容；不要把演示账号用于公网服务。真实入口是 `backend.service:app`，离线入口是 `backend.demo`。

## 完整真实复现：先准备自己的研究环境

完整功能复现需要已安装 Shopify 应用的开发店铺、Amazon SP-API/LWA 沙箱凭据和可用模型 API Key。Web 可独立使用；飞书入口另外配置自己的自建应用。所有凭据只放私有 `.env` 或密钥文件；原型不会替你提供平台账号。

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

在工作台建立自己的平台连接，提交同步与分析任务，并核对报告中的模型模式、来源与限制。完整操作顺序见 [三个用户案例](USER_JOURNEYS.md)。

探针只读平台身份/订单访问，用合成资料测试模型，不发送消息或业务写入。输出只含状态和安全错误码；不会输出店铺、App ID、成员身份或 token。

## Shopify

1. 创建你自己的 Shopify 应用并安装到开发店铺。根据应用用途选择分发方式；公开分发、受保护客户数据许可及商店审核不是代码可以自动替代的步骤。
2. 在后端私有 `.env` 配置 Client ID、Client Secret 与固定 `PUBLIC_URL`。工作台采用独立应用的授权码 OAuth，不是嵌入式 Admin 应用。
3. 在平台配置 App URL 与 `/api/shopify/callback` 的 HTTPS 回调，使用 [通用 TOML 模板](../deploy/shopify.app.toml.example) 检查访问范围与事件订阅。模板中的 `ops.example.com`、`YOUR_SHOPIFY_CLIENT_ID` 都是占位符。
4. 组织负责人在“平台连接”输入自己的 `your-store.myshopify.com` 并授权。平台连接及同步结果归属当前组织；OAuth 状态、HMAC、域名和租户关系都由后端校验。
5. 订单访问可能需要在应用平台的 Protected customer data access 中说明用途并保存；公开应用还需要相应审查。本项目不查询客户姓名、邮箱、电话或地址。

权限包括订单、产品、库存、位置及商户管理的履约订单。可读取的订单范围受平台授权窗口与查询条件限制，不额外申请 `read_all_orders`。真实 API 使用 GraphQL Admin API `2026-10`，后续版本需自行复核字段与弃用情况。

只针对自己组织已安装的开发应用，工作台本机入口提供 Client Credentials 导入；已有 Admin API token 可用于只读连接探针。这不能替代面向所有商户的 OAuth。服务端不把 token 返回给前端。

### 安全验证顺序

保持 `SHOPIFY_TEST_ONLY=true`，先验证只读同步，再使用 `order.test=true`、成功收款交易 `transaction.test=true` 的测试订单，以及带 `xiaokua-test` 标签的商品。退款必须满足测试交易和可退金额检查，所有写入仍需另一成员审批。不要把普通真实订单当成测试订单。

履约不发客户通知；物流更新只修改履约单信息；取消不自动退款，退款另行提出。库存使用 compare-and-set；旧数量变化会拒绝执行。外部写入中断或结果未知会转人工核实，禁止自动重复发送。

工作台“刷新数据”读取本地快照；点击订单/库存“同步”才能得到最新平台快照。结果待核实的审批记录提供“查询平台”，用于人工核实结案，包括平台异步取消结果。

2026-10-12 已完成三个从买家前台测试下单开始的真实平台案例，包含消息、模型、独立审批、平台回读、通知与审计。步骤和通用核验脚本见 [USER_JOURNEYS.md](USER_JOURNEYS.md)，结果和图片见 [TEST_REPORT.md](TEST_REPORT.md)。公开报告不包含店铺地址、账号或资源标识。

### 前台测试结账的准备

只在自己的开发店铺启用 Test Payment Gateway，保持前台密码保护。创建三个独立测试商品，发布到在线商店；为德国和法国启用测试市场，给这些商品配置可配送的库存地点、配送地区及测试运费。本轮使用每件 19.90 USD、初始库存 10、零测试运费；不购买物流标签。

初始化脚本检查主地点的 `isActive`、`fulfillsOnlineOrders` 和 `shipsInventory`。有后台库存并不代表前台可以买，仍需检查商品的配送配置和市场。使用 [Shopify 官方测试下单说明](https://help.shopify.com/en/manual/checkout-settings/test-orders) 中的虚拟卡输入；不要填真实银行卡，也不要为了测试走真实支付再退款。买家邮箱可用保留的 `example.com`，本轮 `.invalid` 因域名校验被拒绝。

通用案例初始化会创建新的组织、两个不同角色成员、加密连接与三个 DRAFT 测试商品，不创建订单、不伪造消息，也不批准业务。你仍需在浏览器完成发布、结账、飞书消息及卡片审批；运行账号、会话、平台记录和原始图片均留在私有 `data/`。

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

这一路径有自动化签名测试，公开交付没有公网真实投递验收。成员 OAuth 回调仍为 `/api/feishu/callback`。聊天与 Web 使用公共指令分派，可提出明确金额、数量和对象的业务方案；所有写入仍需独立审批。多店使用“店铺[标签]”或“所有店铺”，不能让系统猜测目标商店。

## Amazon SP-API

在“平台连接”输入自己商户授权的 LWA Client ID、Client Secret、Refresh Token，明确选择 `sandbox` 或 `production`、区域和 Marketplace。服务端只使用官方固定端点，获取并缓存短期访问 token，凭据加密保存且不返回前端。本机 `AMAZON_*` 导入始终指向沙箱。

| 区域 | 生产 / 沙箱端点 | 示例站点 |
| --- | --- | --- |
| 北美 `na` | `sellingpartnerapi-na.amazon.com` / `sandbox.sellingpartnerapi-na.amazon.com` | 美国 `ATVPDKIKX0DER` |
| 欧洲 `eu` | `sellingpartnerapi-eu.amazon.com` / `sandbox.sellingpartnerapi-eu.amazon.com` | 德国 `A1PA6795UKMFR9` |
| 远东 `fe` | `sellingpartnerapi-fe.amazon.com` / `sandbox.sellingpartnerapi-fe.amazon.com` | 日本 `A1VC38T7YXB528` |

Order API 使用 `2026-01-01`；FBA Inventory 使用 v1；MCF 使用 Fulfillment Outbound `2026-07-04`。LWA/SP-API 认证、固定端点与访问 token 方式依据 [官方连接说明](https://developer-docs.amazon.com/sp-api/docs/connecting-to-the-selling-partner-api)。生产订单仅读取金额、履约、取消与包裹相关数据集，不请求买家/收件人身份字段。

**两种沙箱分开核验：** Orders 采用官方精确参数的静态样例，本区域样例站点可能与所选 FBA 站点不同。北美样例为巴西，欧洲为英国，远东为日本；适配器明确标注来源，只取匹配的固定样例页，不把示例 continuation token 当作完整动态订单分页。错误区域与样例站点组合会出现访问拒绝。读取到的固定样例不参与物理库存预留。

FBA/MCF 是动态沙箱。在“商品与库存”选择 Amazon 沙箱店，依次申请创建测试 SKU、增加虚拟库存、创建 MCF；每个步骤由另一成员审批。完成履约后，从执行结果复制包裹编号，再分别推进虚拟包裹到派送、交付；也可另建 MCF 验证取消。操作后同步与只读回查会记录平台实际状态。沙箱没有真实付款、包裹或平台通知，不可用来压测，也不能在 Seller Central 商家后台看这些沙箱订单。

动态 FBA 可售数量由平台维护，不能直接设置 FBA 实物库存。FBA 与自发货库存分别建池；当前不开放 Amazon 自发货库存设置。生产 Amazon 连接只读，真实 MCF 的目的地资料、角色、隐私范围、费用、通知和生产状态须单独验收后才能开放写入，不能通过改模型提示解除。

生产商户还要完成自己的开发者角色与商户授权；更改角色后按官方流程重新取得相应授权。多商户产品应完成平台要求的分发/审核与 OAuth 安装，不能共享原型作者的测试凭据。完整复现和已观察到的数据差异见 [三个完整业务验收](TEST_REPORT.md)。

## 新增平台

当前没有其他平台的占位连接器。取得资格后仍需实现实际协议；Amazon 联通和扩展步骤见 [平台目录 README](../backend/production/platforms/README.md)。

## DeepSeek 与结构化分析

在私有配置设置 `DEEPSEEK_API_KEY` 与服务商实际开放的 `DEEPSEEK_MODEL`。默认 `DEEPSEEK_ANALYSIS_FORMAT=strict_tools`，使用官方 Beta `/beta/chat/completions` 的严格 function schema。若自己的服务只支持 JSON 对象，可显式设置为 `json`；该兼容模式仍受本地 Schema 与证据校验约束。更改 `.env` 后重启服务。

两种模式都在本地检查 `finding`、`evidence`、`limitations` 的类型、长度、多余字段和证据归属。strict 模式的 `submit_analysis` 只用于承载结果，不执行工具。Beta 不接受的长度限制由本地校验器实现。非法输出、鉴权失败和限流等原因会展示为明确的规则回退，不能视作真实推理成功。

每个角色最多两次请求、每次最多 1400 输出 tokens；只对短暂故障作有限重试。工作台显示服务商提供的输入/输出 tokens，没有价格或账单保证。模型内容不能批准订单或其他业务写入。

离线 `backend.demo` 始终使用规则模式。真实 `backend.service` 才调用研究者的模型；模型费用由自己的账号承担，不需要另行购买编排或记忆云服务。

模型探针只用合成输入。实际运行时，导入知识和客服资料应先去除个人信息；模型角色只接收受限上下文，运行结果仍属于组织私密资料。

针对某笔订单，应先通过飞书同步最新订单及商品明细，再发送“分析德国订单 #你的订单编号 的能源效率咨询”。`#` 后填写店铺订单的数字部分；当前实现识别数字订单编号。模型上下文在调用前限定到该订单与相关商品。编号未知、同名不唯一或明细被截断时会拒绝分析；库存数量是已同步的跨地点汇总，实际履约仍需检查区域可配送性。

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
