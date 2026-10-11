# 自行研究部署的参考

本次交付是本机可运行的研究原型，没有购买服务器、配置域名或公开部署。优先按 [README](../README.md) 运行离线 demo。

## 真实入口

`backend.service:app` 是自行接入开发平台的工作台；需建立自己的组织、独立审批成员和平台连接。默认本机 SQLite。`APP_ENV=production` 强制 PostgreSQL、HTTPS PUBLIC_URL 与独立加密密钥，缺少任一配置会拒绝启动。

## 仓库中的参考文件

- `Dockerfile`：前端构建、锁定 Python 依赖、非 root 服务。
- `compose.yaml`：PostgreSQL、API、持久 worker、可选飞书连接进程。
- `deploy/Caddyfile`：自行配置域名后的 HTTPS 反向代理参考。
- `scripts/prepare_deploy.py`：本机生成私有部署配置；产物在忽略的 `.deploy/`。
- `scripts/backup_database.py`：数据库备份参考。密钥需独立备份，数据库备份不能替代密钥恢复。
- `deploy/shopify.app.toml.example`：使用你自己的 Client ID 与域名填写应用配置。

这些文件不会自动申请平台分发、审核应用、开放支付或替你配置业务授权。实际运行前须自行审查网络入口、备份恢复、日志留存、监控、平台权限与容量。

## 本机容器实验

构建命令：

```bash
docker build -t xiaokua-research:local .
```

默认镜像运行真实工作台入口，不预置离线 demo 账号。依照 `.env.example`、Compose 的 secret 文件路径及平台接入文档设置你自己的实验环境；不要将固定演示密码暴露到公网。

此前已有容器内 SQLite/PostgreSQL、HTTPS 代理、备份恢复和持久任务验收摘要；本次公开交付未重新发布镜像或服务器。详见 [验收边界](ACCEPTANCE.md)。
