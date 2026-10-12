# 试运营与部署指南

本文面向准备把小跨接入自己店铺的维护者。先按照 [README](../README.md) 完成本机安装与 [平台接入](INTEGRATIONS.md)，再准备私有试运营环境。建议从一个店铺、少量商品和两名实际参与的运营/审批成员开始。

仓库提供部署配置和应用源码，目前的真实业务验收使用开发店铺与测试网关。自己的公网安装、真实订单、付款网关和业务规模，需要在试运营环境中分别验证。

## 1. 确定账号与业务范围

- Shopify：拥有可授权的店铺和应用，配置订单、商品、库存、履约权限，以及平台要求的受保护订单数据访问。自己组织内的应用与其他商户应用的认证方式不同，按 [Shopify 独立应用认证](https://shopify.dev/docs/apps/build/authentication-authorization/authenticate-standalone-apps) 和 [自己组织内的应用认证](https://shopify.dev/docs/apps/build/authentication-authorization/client-credentials-grant) 选择。
- 飞书：启用自建应用机器人，开放发送消息与读取单聊权限，配置消息事件和卡片回调，发布到目标成员范围。把真实成员身份绑定到工作台，至少配备一名不同于申请人的审批人。
- 模型：准备自己的 DeepSeek API Key，填写实际可用的模型标识；确认调用额度和费用。导入模型上下文前去除客服个人信息。
- 业务：明确履约地点、库存口径、可配送区域、退款责任与异常核实人员。当前取消和退款是两个独立操作，运单更新只写入 Shopify 信息。

完整权限和回调配置见 [接入文档](INTEGRATIONS.md)。先使用测试订单验证 [三个完整用户案例](USER_JOURNEYS.md)，再准备真实业务。

## 2. 准备私有运行环境

需要支持 Docker Compose 的主机、可访问的域名、持久磁盘，以及访问 Shopify、飞书和 DeepSeek 的网络。HTTPS 代理使用 80/443 端口；数据库仅在 Compose 内部网络提供服务。

生产入口为 `backend.service:app`，设置 `APP_ENV=production` 时必须具备 PostgreSQL、HTTPS `PUBLIC_URL` 和独立加密密钥。缺少这些配置会拒绝启动。

仓库提供的文件：

| 文件 | 用途 |
| --- | --- |
| [Dockerfile](../Dockerfile) | 构建前端和 Python 运行环境，以非 root 用户启动 |
| [compose.yaml](../compose.yaml) | 数据库、迁移、API、Worker、可选飞书长连接和 HTTPS 代理 |
| [Caddyfile](../deploy/Caddyfile) | 域名 HTTPS 与 API 反向代理 |
| [prepare_deploy.py](../scripts/prepare_deploy.py) | 生成私有配置和独立密钥，已存在的文件不会被覆盖 |
| [backup_database.py](../scripts/backup_database.py) | 数据库备份、校验与向独立数据库恢复 |
| [Shopify 配置示例](../deploy/shopify.app.toml.example) | 应用权限与自己的回调域名配置 |

在仓库根目录生成私有部署配置，示例域名要换成自己的实际域名：

```bash
uv run python scripts/prepare_deploy.py --domain ops.example.com
```

编辑 `.deploy/production.env`，填写 `DEEPSEEK_API_KEY`、`DEEPSEEK_MODEL`、`SHOPIFY_CLIENT_ID` 和 `SHOPIFY_CLIENT_SECRET`。`PUBLIC_HOST` 与 `PUBLIC_URL` 应与实际域名一致。飞书凭据在工作台建立连接时录入；稍后再填该连接的 `FEISHU_CONNECTION_ID`。

配置产物位于 `.deploy/`，密钥位于 `.deploy/secrets/`。这些文件和数据库都应保留在私有环境，并另行备份；公开仓库不包含它们。域名需正确解析，主机的 80/443 端口需可达，HTTPS 代理才能为实际域名申请证书。

## 3. 启动服务并完成初始化

初次启动保持测试保护：

```bash
docker compose --env-file .deploy/production.env up -d --build
docker compose --env-file .deploy/production.env ps
```

数据库就绪后，`migrate` 完成迁移并退出，API 和 Worker 随后启动。访问自己的 `PUBLIC_URL`，使用 API 容器 `/app/data/setup.token` 中的私有初始设置码创建负责人账号，再邀请独立审批人。生产环境不自动显示设置码，也不预置演示账号。

维护者可在自己的服务器终端读取设置码，完成初始化后将其按凭据保管：

```bash
docker compose --env-file .deploy/production.env exec api cat /app/data/setup.token
```

通过工作台建立 Shopify 与飞书连接，绑定成员。若使用飞书 SDK 长连接：

1. 工作台连接的接收方式选择 `websocket`。
2. 将连接 ID 填入 `.deploy/production.env` 的 `FEISHU_CONNECTION_ID`。
3. 在飞书开放平台配置消息事件和卡片回调为长连接并发布。
4. 启动可选服务：

```bash
docker compose --env-file .deploy/production.env --profile feishu-ws up -d feishu_ws
```

这里的 `--env-file` 同时用于 Compose 命令中的连接 ID 插值，不能省略。当前模板启动一条飞书连接；新增连接需安排对应的独立连接进程。

使用 HTTPS 回调时，配置实际回调地址、Verification Token 和 Encrypt Key，无需启动长连接服务。Shopify OAuth 回调和应用 URL 也必须使用自己的固定 HTTPS 地址。

## 4. 验证后开放真实订单

默认 `SHOPIFY_TEST_ONLY=true`。测试保护要求订单及退款收款交易为测试资源，商品写入要求 `xiaokua-test` 标签。

Compose 在服务的 `environment` 中固定设置了测试保护，优先于 `.deploy/production.env`。要开放真实写入，在私有环境创建下面的 `compose.pilot.yaml`，对实际运行的三个应用进程使用一致配置：

```yaml
services:
  api:
    environment:
      SHOPIFY_TEST_ONLY: "false"
  worker:
    environment:
      SHOPIFY_TEST_ONLY: "false"
  feishu_ws:
    environment:
      SHOPIFY_TEST_ONLY: "false"
```

应用覆盖配置（长连接模式）：

```bash
docker compose --env-file .deploy/production.env -f compose.yaml -f compose.pilot.yaml --profile feishu-ws up -d
```

HTTPS 回调模式去掉 `--profile feishu-ws`。后续更新服务时继续使用同一组配置文件，避免不同进程的运行模式不一致。恢复测试保护可将三个值一起改回 `"true"`，并使用同一命令重建对应服务。

更改此值会取消测试资源限制，所有业务写入仍需独立审批。它不会改变 Shopify 支付网关是否处于测试模式，也不会扩大平台权限或自动完成商户分发。启用前确认没有待执行或结果未知的旧方案，并重新创建本轮试运营所需的方案。

## 5. 试运营核对

开始时控制店铺、商品和操作范围，核对以下实际行为：

- 飞书消息进入正确组织，申请人与审批人是不同的真实成员，重复审批不会重复执行。
- 同步快照与 Shopify 一致，订单分析引用正确商品；地点库存和收货区域可配送性按自己的业务核实。
- 履约、运单更正、库存和商品说明先各验证一笔，审批前后差异与最终平台状态一致。
- 退款核实真实网关、币种和可退金额；取消另行执行并检查库存。开发店铺测试退款结果不能替代真实网关验收。
- Worker、长连接和平台通知持续工作，任务失败或结果未知时有人处理；在“运行设置”查看状态，定期检查 `/api/health`、`/api/ready` 及进程日志。
- 已验证数据库与密钥恢复，审计导出、隐私请求和客服记录的留存处理由负责人安排。

多商户分发、公网 OAuth 安装、真实支付网关和生产容量尚需在自己的环境验收。承运商实际轨迹、发货后逆向物流、Amazon、Meta 广告和采购支付需单独扩展接入。

## 6. 备份与恢复

数据库备份：

```bash
uv run python scripts/backup_database.py backup
uv run python scripts/backup_database.py verify --file backups/YOUR_BACKUP.dump
```

将文件名替换为实际生成的备份。脚本同时生成校验文件；备份不包含加密密钥。独立保存 `.deploy/secrets/`、私有平台配置和应用数据卷，明确恢复时使用的代码与迁移版本。

恢复演练使用新的独立数据库名，原数据库保留：

```bash
uv run python scripts/backup_database.py restore --file backups/YOUR_BACKUP.dump --target-db xiaokua_restore_trial
```

`--target-db` 必须以 `xiaokua_restore_` 开头且尚不存在。恢复后在隔离环境验证数据和凭据解密，关闭 Worker 与长连接，避免演练实例处理真实任务；确认后再安排切换。数据库备份单独恢复无法解密丢失了密钥的连接凭据。

现有验证记录与流程图片见 [测试报告](TEST_REPORT.md)。
