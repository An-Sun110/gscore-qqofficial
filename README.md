# gscore-qqofficial

直接连接 [gsuid_core](https://github.com/Genshin-bots/gsuid_core) 与 [QQ 官方机器人](https://bot.q.qq.com/wiki/) 的轻量适配器，不依赖 NoneBot、OneBot、NapCat 或本地 QQ 客户端。

> 本项目由 AI 生成。

支持 QQ 群聊全量消息、单聊、频道和频道私信，包含文本/图片收发、引用图片解析、断线恢复、限流重试、SQLite 状态持久化及 Docker/systemd 部署。

> QQ 群聊和单聊采用被动回复机制，回复必须关联约 5 分钟内收到的消息，因此不适合无上下文的主动推送。

## 配置

在 QQ 开放平台创建机器人并启用需要的消息事件，然后复制配置：

```bash
cp .env.example .env
```

```dotenv
QQ_APP_ID=你的AppID
QQ_APP_SECRET=你的AppSecret
GSCORE_URL=ws://127.0.0.1:8765/ws/QQOfficial
GSCORE_TOKEN=
QQ_ADMIN_IDS=
```

`GSCORE_TOKEN` 应与 core 的 `WS_TOKEN` 一致。`QQ_ADMIN_IDS` 可填写管理员 OpenID，多个使用英文逗号分隔。不要提交 `.env`。

默认允许主动消息：core 下发带 `target_type` 和 `target_id` 的消息时，即使没有最近的回复上下文也会直接发送。设置 `PROACTIVE_ENABLED=false` 可关闭。

群聊全量消息需要群主在 QQ 中允许机器人接收群内全部消息。普通群消息不会伪装成 `@机器人`，是否触发由 gsuid_core 插件的命令前缀决定。

## Docker Compose

推荐使用 Docker Compose：

```bash
docker compose up -d --build
docker compose logs -f gscore-qq
```

根据 gsuid_core 的位置设置 `GSCORE_URL`：

| 位置 | 地址 |
|---|---|
| Docker 宿主机 | `ws://host.docker.internal:8765/ws/QQOfficial` |
| 同一 Compose 网络，服务名为 `gscore` | `ws://gscore:8765/ws/QQOfficial` |
| 远程服务器 | `ws://服务器IP:8765/ws/QQOfficial` |

远程连接时，core 必须监听外部地址并配置 `WS_TOKEN`。Compose 使用持久卷保存 SQLite 状态。

## Linux systemd

```bash
sudo sh deploy/install-systemd.sh
sudo nano /etc/gscore-qqofficial.env
sudo systemctl restart gscore-qq
sudo journalctl -u gscore-qq -f
```

服务以独立低权限用户运行，状态保存于 `/var/lib/gscore-qqofficial/state.db`。

## Python 运行

要求 Python 3.10+：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
gscore-qq
```

Windows 使用 `.venv\Scripts\Activate.ps1` 激活环境。长期运行可执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install-windows-task.ps1
```

## 管理命令

配置 `QQ_ADMIN_IDS` 后，管理员可向机器人发送：

```text
/重启
/更新
```

`/更新` 仅支持 Git 源码部署。Docker 请在宿主机运行：

```bash
git pull
docker compose up -d --build
```

## 引用图片

QQ 引用事件使用扩展字段 `message_type=103`、`message_scene.ext` 和 `msg_elements`。适配器按以下顺序获取图片：

1. 本次事件 `msg_elements[0].attachments` 中的新链接；
2. SQLite 中按 `msg_idx/ref_msg_idx` 保存的图片；
3. 同一会话最近 5 分钟的图片。

图片最终以普通 gscore `image` 消息段上报，因此插件可从 `event.image` / `event.image_list` 直接读取。

## 常见问题

- **无法连接 core**：确认端口 `8765`、监听地址和 `GSCORE_TOKEN`；容器访问宿主机不能使用 `localhost`。
- **连接正常但不响应**：确认 QQ 开放平台已启用对应事件；公域群机器人通常需要 `@机器人`。
- **图片或引用异常**：确认启动日志版本为最新，并使用 `LOG_LEVEL=DEBUG` 查看原始事件处理日志。
- **DNS 错误**：检查 `nslookup bots.qq.com` 和 `curl -I https://bots.qq.com`。

## 开发

```bash
pip install -e ".[test]"
pytest -q
```

协议参考：[QQ 官方文档](https://bot.q.qq.com/wiki/) · [QQ 官方 Python SDK](https://github.com/tencent-connect/botpy) · [gsuid_core 适配器文档](https://github.com/Genshin-bots/gsuid_core/tree/master/docs/skills/gscore-adapter-development)
