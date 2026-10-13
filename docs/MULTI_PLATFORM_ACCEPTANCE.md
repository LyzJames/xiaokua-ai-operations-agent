# 多平台验收已并入三个完整案例

当前版本不再把多平台调度拆成另一组主要业务测试。三个全新 Shopify 前台订单的每条链路均从同时选择 Shopify 与 Amazon 开始，包括逐店同步与真实模型分析、分仓库存、逐项独立审批、两平台操作、最终回读和审计。

请阅读 [完整测试报告及图片](TEST_REPORT.md) 与 [三个案例的复现步骤](USER_JOURNEYS.md)。本次结果来自自己的开发店铺、Amazon 官方托管沙箱和真实模型 API；案例 C 保留 FBA 库存人工核实项。

当前只注册已实现的 Shopify 和 Amazon；新增平台的资格、协议、能力声明和业务验收步骤集中在 [平台目录 README](../backend/production/platforms/README.md)。
