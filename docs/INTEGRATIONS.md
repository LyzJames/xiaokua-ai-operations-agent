# 真实平台接入参考

此文档面向使用自己开发账号的研究者。离线演示无需配置这些内容；不要把演示账号用于公网服务。真实入口是 `backend.service:app`，离线入口是 `backend.demo`。

## Shopify

1. 创建你自己的 Shopify 应用并安装到开发店铺。根据应用用途选择分发方式；公开分发、受保护客户数据许可及商店审核不是代码可以自动替代的步骤。
2. 在后端私有 `.env` 配置 Client ID、Client Secret 与固定 `PUBLIC_URL`。工作台采用独立应用的授权码 OAuth，不是嵌入式 Admin 应用。
3. 在平台配置 App URL 与 `/api/shopify/callback` 的 HTTPS 回调，使用 [通用 TOML 模板](../deploy/shopify.app.toml.example) 检查访问范围与事件订阅。模板中的 `ops.example.com`、`YOUR_SHOPIFY_CLIENT_ID` 都是占位符。
4. 组织负责人在“平台连接”输入自己的 `your-store.myshopify.com` 并授权。平台连接及同步结果归属当前组织；OAuth 状态、HMAC、域名和租户关系都由后端校验。
5. 订单访问可能需要在应用平台的 Protected customer data access 中说明用途并保存；公开应用还需要相应审查。本项目不查询客户姓名、邮箱、电话或地址。

权限包括订单、产品、库存、位置及商户管理的履约订单。默认查询近 30 天订单，不需要为了这个范围申请 `read_all_orders`。真实 API 使用 GraphQL Admin API `2026-10`，后续版本需自行复核字段与弃用情况。

只针对自己组织已安装的开发应用，本机还提供 Client Credentials 或现有 Admin API token 的导入路径；这不能替代面向所有商户的 OAuth。服务端不把 token 返回给前端。

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

## DeepSeek

在后端私有配置设置 `DEEPSEEK_API_KEY` 与服务商实际开放的 `DEEPSEEK_MODEL`。适配器调用 `https://api.deepseek.com/chat/completions`，使用 JSON 对象响应。密钥和模型回答不进入审批权限判断。

模型只辅助三个分析角色，以同步统计与已确认知识为上下文。失败时报告标记规则回退，不能声称已发生真实推理。离线 demo 强制规则模式，即使本机已有密钥也不会调用模型。真实模型计费由研究者自己的账号承担。

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
