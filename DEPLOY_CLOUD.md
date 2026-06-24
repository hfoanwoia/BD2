# 达人 CRM 云端部署说明

## 推荐部署方式

推荐先部署为云端 Web 后台，不建议第一步做微信小程序。

原因：当前系统已经是 FastAPI + Web 后台，部署后运营可直接用浏览器访问；微信小程序仍然需要同一套后端和数据库，还会增加小程序开发、审核、发布成本。

## Render 部署

1. 把本目录上传到 GitHub 仓库。
2. 在 Render 新建 Blueprint 或 Web Service。
3. 选择本仓库根目录。
4. 使用根目录 `render.yaml` 或 Dockerfile 部署。
5. 首次部署后访问：

```text
https://你的服务域名/influencer.html
```

## 必须保留的数据

本项目默认使用 SQLite：

```text
/app/data/commerce.db
```

云端部署必须配置持久化磁盘或云数据库，否则服务重启后数据可能丢失。

## 启动命令

Docker 会自动执行：

```bash
uvicorn backend.app:app --host 0.0.0.0 --port $PORT
```

## 当前演示账号

- admin / admin123
- manager / manager123
- bd / bd123

正式使用前建议修改默认账号密码。
