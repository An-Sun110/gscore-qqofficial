# gscore-qqofficial

轻量级 QQ 官方机器人适配器，直接连接 [gsuid_core](https://github.com/Genshin-bots/gsuid_core) 与 [QQ 开放平台](https://bot.q.qq.com/wiki/)。不依赖 NoneBot、OneBot、NapCat 或本地 QQ 客户端。

## 功能

- QQ 群 `GROUP_AT_MESSAGE_CREATE`
- QQ 单聊 `C2C_MESSAGE_CREATE`
- QQ 频道 `AT_MESSAGE_CREATE`
- QQ 频道私信 `DIRECT_MESSAGE_CREATE`
- gscore 文本、图片消息收发
- AccessToken 自动刷新
- Gateway 心跳、ACK 超时检测和会话恢复
- 限流、服务端错误和临时网络错误重试
- 带随机抖动的指数退避重连
- 单条消息异常隔离
- Docker、systemd 和 Windows 计划任务部署

QQ 官方机器人采用被动回复模型。群聊和单聊回复必须引用约 5 分钟内收到的 `msg_id`，因此无消息上下文的定时推送可能无法发送。这是 QQ 开放平台限制。

## 准备工作

1. 在 QQ 开放平台创建机器人，取得 AppID 和 AppSecret。
2. 按机器人类型在开放平台启用群聊、单聊或频道消息事件。
3. 启动 gsuid_core，默认地址为 `ws://127.0.0.1:8765/ws/QQOfficial`。
4. 复制配置模板并填写参数：

```bash
cp .env.example .env
```

```dotenv
QQ_APP_ID=你的AppID
QQ_APP_SECRET=你的AppSecret
GSCORE_URL=ws://127.0.0.1:8765/ws/QQOfficial
GSCORE_TOKEN=
LOG_LEVEL=INFO
```

不要提交 `.env`。它已经包含在 `.gitignore` 和 `.dockerignore` 中。

## Docker Compose

推荐大多数用户使用 Docker Compose：

```bash
docker compose up -d --build
docker compose logs -f gscore-qq
```

常见网络布局：

| gsuid_core 位置 | `GSCORE_URL` |
|---|---|
| Docker 宿主机 | `ws://host.docker.internal:8765/ws/QQOfficial` |
| 同一 Compose 网络，服务名为 `gscore` | `ws://gscore:8765/ws/QQOfficial` |
| 另一台服务器 | `ws://服务器IP:8765/ws/QQOfficial` |

Linux 下 Compose 已配置 `host-gateway`。当 core 不在本机回环地址时，需要让 gsuid_core 监听外部地址，并强烈建议配置 `WS_TOKEN`。

停止和升级：

```bash
docker compose down
git pull
docker compose up -d --build
```

镜像以非 root 用户运行，使用只读根文件系统，并启用 `no-new-privileges`。

## Linux systemd

不使用 Docker 时，可安装为独立 systemd 服务：

```bash
sudo sh deploy/install-systemd.sh
sudo nano /etc/gscore-qqofficial.env
sudo systemctl restart gscore-qq
sudo systemctl status gscore-qq
sudo journalctl -u gscore-qq -f
```

安装器会创建无登录权限的 `gscore` 用户，程序安装到 `/opt/gscore-qqofficial`，配置保存在 `/etc/gscore-qqofficial.env`，权限为 `0600`。

升级时在新代码目录重新执行安装脚本，然后重启服务：

```bash
sudo sh deploy/install-systemd.sh
sudo systemctl restart gscore-qq
```

## Python 直接运行

要求 Python 3.10 或更高版本：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
gscore-qq
```

Windows 激活虚拟环境使用：

```powershell
.venv\Scripts\Activate.ps1
gscore-qq
```

Windows 长期运行可使用管理员 PowerShell 安装计划任务：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install-windows-task.ps1
```

## 配置

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `QQ_APP_ID` | 必填 | QQ 开放平台 AppID |
| `QQ_APP_SECRET` | 必填 | QQ 开放平台 AppSecret |
| `GSCORE_URL` | `ws://127.0.0.1:8765/ws/QQOfficial` | gsuid_core WebSocket 地址 |
| `GSCORE_TOKEN` | 空 | 与 core `config.json` 中的 `WS_TOKEN` 一致 |
| `QQ_API_BASE` | `https://api.sgroup.qq.com` | QQ OpenAPI 地址 |
| `LOG_LEVEL` | `INFO` | 日志级别 |

系统环境变量优先于 `.env` 文件。

## 故障排查

### 无法连接 `bots.qq.com`

先检查 DNS 和 HTTPS：

```bash
nslookup bots.qq.com
curl -I https://bots.qq.com
```

程序在 Windows 上显式使用系统 DNS 解析器，以规避部分 `aiodns` 环境无法联系 DNS 的问题。

### 无法连接 gsuid_core

- 确认 core 已启动并监听 `8765`。
- 容器内不能使用 `localhost` 访问宿主机，使用 `host.docker.internal`。
- 核对 `GSCORE_TOKEN` 与 core 的 `WS_TOKEN`。
- 跨服务器部署时检查防火墙和 core 监听地址。

### 能连接但没有回复

- 确认机器人已在 QQ 开放平台订阅对应事件。
- 公域机器人通常只会收到 `@机器人` 的消息。
- 检查回复是否超过 QQ 被动回复有效期。
- 使用 `LOG_LEVEL=DEBUG` 查看详细日志。

## 开发测试

```bash
pip install -e ".[test]"
pytest -q
```

适配器按照 gsuid_core 的 `MessageReceive` / `MessageSend` 协议发送二进制 JSON WebSocket 帧。QQ 侧使用官方 AccessToken、Gateway 与 OpenAPI，不模拟或控制 QQ 客户端。

## 参考资料

- [QQ 机器人开放平台](https://bot.q.qq.com/wiki/)
- [QQ 官方 Python SDK](https://github.com/tencent-connect/botpy)
- [gsuid_core 适配器开发文档](https://github.com/Genshin-bots/gsuid_core/tree/master/docs/skills/gscore-adapter-development)
