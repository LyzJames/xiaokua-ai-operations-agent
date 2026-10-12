# 平台连接实现与扩展

这里是小跨的真实电商平台适配层。当前只注册 **Shopify 和 Amazon**，没有淘宝、京东、拼多多、TEMU 等平台的占位连接器或可执行能力。新增平台需要按其官方协议实现，完成授权和验收后才能注册。

## Amazon 如何联通

1. 负责人在 Web「平台连接」选择沙箱或生产环境、区域和 Marketplace，并填写自己的 LWA Client ID、Client Secret 与 Refresh Token。连接创建时先验证 LWA 和 FBA 读取；凭据加密存储，不进入模型、公开报告或浏览器前端。
2. `AmazonAdapter.token()` 向 `https://api.amazon.com/auth/o2/token` 交换短期 Access Token；运行时以内存缓存管理到期时间。撤销或更换商户授权需重新配置有效 Refresh Token。
3. `ENDPOINTS` 固定北美、欧洲、远东的官方沙箱/生产主机；区域与 Marketplace 必须相符。请求使用 HTTPS、`x-amz-access-token`、受限超时和安全错误代码，不接受模型提供任意主机。
4. Orders `2026-01-01` 和 FBA Inventory v1 响应转换为公共 `OrderSnapshot` / `ProductSnapshot`。沙箱 Orders 是区域对应的官方静态样例；FBA 可用量和平台预留量分别来自动态沙箱；预留字段缺失时保留未知，不能写为零。客户姓名、邮箱、电话和地址不会加入公共快照。
5. Fulfillment Outbound `2026-07-04` 动态沙箱支持虚拟 SKU、虚拟入库、创建/取消 MCF、模拟订单状态和模拟包裹状态。合成收货国家可选择美国、德国或法国；系统使用固定合成地址，不读取真实买家地址。沙箱起运地址固定为美国；这不能证明生产账户的跨境配送资格。
6. 每项写入进入相同的任务、另一成员审批、Worker 和平台回读流程。持久保存执行意图后只发送一次；执行结果未知时等待核实。创建 MCF 回读唯一编号、SKU、数量及合成收货国家。

`amazon.py` 负责官方协议，`registry.py` 负责组织归属和注册，`contracts.py` 负责公共数据及能力。Web 和飞书均调用同一业务服务；共享库存与审批不会因为换平台而绕过。

## 沙箱与生产的区别

| 环境 | 当前应用能力 | 所需条件 |
| --- | --- | --- |
| Amazon 沙箱 | 官方静态订单、动态 FBA 与 MCF 状态流程 | 已注册的沙箱应用及沙箱 LWA 凭据；本次开发账号控制台显示为「沙盒」 |
| Amazon 生产只读 | 已实现生产端点的 Orders/FBA 读取代码，仍需真实账户验收 | 生产应用、对应 API 角色及卖家/供应商账户授予的授权 |
| Amazon 生产写入 | 当前关闭 | 补齐实际收件资料与目标业务接口，取得所需角色，验证真实账户的完整流程 |

沙箱凭据不能替代真实商户授权。私有卖家应用的官方注册要求包含 Professional selling account，具体主体条件取决于账户注册地和类型；不能概括为所有调用都必须企业营业执照。MCF 沙箱的状态推进只用于模拟，真实物流状态由仓库与承运商事件驱动。

本轮 UNFULFILLABLE 回读仍有 UNDELIVERABLE 包裹及可用 9、预留 1。订单失败不保证库存立即释放，应用保留实际指标与人工核实，不自动补库存。

FBA 作为平台仓独立管理；本项目未实现 Amazon 自发货可售量写入，也未实现 Amazon 广告、采购支付或完整的卖家退款工具。动态沙箱通过的结论限于上述已实现流程。

## 如何增加一个其他平台

1. **确定准入与业务范围。** 取得官方开发资格、商户授权及目标区域的文档，列明实际开放的订单、商品、库存、履约或售后能力。
2. **实现平台协议。** 完成官方认证/签名、授权续期与撤销、分页、限流和错误处理。仅调用固定官方主机；读取可以有限重试，写入未知结果不能自动重发。
3. **转换数据语义。** 实现 `CommerceAdapter` 的快照、库存地点、操作准备、执行和核实方法。核对付款、取消、履约数量、仓库与库存定义；缺失内容明确标为未知，不能默认为零或已完成。
4. **接入现有流程。** 注册实际 builder 与能力，增加负责人配置/OAuth 入口。保持组织隔离、明确店铺范围、逐项独立审批、执行前检查、平台回读与审计。
5. **验证后开放。** 先在本机验证协议与故障边界，再用已授权店铺从买家下单走到最终库存、业务状态和审计。没有实际权限时，只能证明代码或模拟协议，不能宣称真实平台已联通。

公共 `CommerceAdapter` 同时服务已经实现的两个平台；这里不预注册尚未实现的平台。操作步骤见 [接入指南](../../../docs/INTEGRATIONS.md)，共享库存规则见 [设计文档](../../../docs/SHARED_INVENTORY_DESIGN.md)，完整业务证据见 [测试报告](../../../docs/TEST_REPORT.md)。

官方依据：[沙箱首次调用](https://developer-docs.amazon/sp-api/docs/onboarding-step-5-make-your-first-call-to-the-sp-api-sandbox)、[生产调用条件](https://developer-docs.amazon/sp-api/docs/onboarding-step-8-call-the-sp-api-in-production)、[私有应用授权](https://developer-docs.amazon/sp-api/docs/self-authorization)、[注册类型](https://developer-docs.amazon.com/sp-api/lang-en_EN/docs/sp-api-registration-overview)、[MCF 动态沙箱](https://developer-docs.amazon/sp-api/docs/fulfillment-outbound-dynamic-sandbox-guide)。
