# Eremia Runtime

Eremia 在东京服务器上的常驻身体：**Claude Code**（Opus 4.6，订阅 token 认证）+ **Tidal Echo 聊天通道**（手机 PWA 私聊，可推送）+ 可选 **prism** 仪表盘 + 可选 **论坛唤醒桥**。与小窝、Ombre Brain 同机内网互通。

```
你手机 (Tidal Echo PWA · 锁屏推送) ──┐
你浏览器 (prism 仪表盘·可选)        ─┤ HTTPS
论坛 (galatea SSE wake·可选)        ─┤
                                    ▼
      ┌── Zeabur 独立服务器 (Tencent Tokyo) ─────────────┐
      │ [tidal-relay 服务] nginx+FastAPI :8080           │
      │      ▲ SSE / REST                                │
      │ [eremia-runtime 服务] :8001                      │
      │   ├─ tmux 里常驻的 claude = Eremia 本体          │
      │   │    └─ channel插件(bun) ←→ tidal-relay        │
      │   ├─ prism (PRISM_ENABLED)                       │
      │   └─ wake-bridge (WAKE_BRIDGE_ENABLED)           │
      │        └─ inject.sh → relay → 塞进 Eremia 会话   │
      │ [shared-nest] [ombre-brain] [gateway]（现有）    │
      └──────────────────────────────────────────────────┘
```

两个服务、一份密钥约定：`RELAY_SECRET` 在 eremia-runtime 和 tidal-relay 两边**必须一致**。

## 部署步骤

### 0. 本地准备

```bash
claude setup-token
```

得到 `sk-ant-oat01-...` 即 `CLAUDE_CODE_OAUTH_TOKEN`。再生成一个 `RELAY_SECRET`：

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

### 1. 推送本仓库到私有 GitHub

仓库根目录是 eremia-runtime 的 Dockerfile，`tidal-relay/` 子目录是 relay 的 Dockerfile。

### 2. Zeabur 建两个服务（都部署到你的独立服务器）

**服务 A：tidal-relay**
- Add Service → 本仓库，Root Directory 设为 `tidal-relay/`
- 挂持久卷到 `/data`（relay.db 聊天记录在这里）
- 环境变量：`.env.example` 底部那一段
- 暴露端口 8080，**绑一个公网域名**（PWA 必须 HTTPS，Zeabur 自动）
- 验证：`https://relay域名/relay/healthz` 返回正常；`https://relay域名/chat/` 手机能打开并"添加到主屏幕"

**服务 B：eremia-runtime**
- Add Service → 本仓库，Root Directory 留空（根目录）
- 挂持久卷到 `/data`（Eremia 的会话历史、MCP 配置、凭据全在这）
- 环境变量：`.env.example` 上半部分；`RELAY_URL` 用 relay 的**内网**地址
- 开了 prism 就暴露 8001 绑域名；不开可以不暴露任何端口
- 起服务后看日志：应有 `claude session 'eremia' started` 和 channel 插件的 `[companion:boot] connected`

### 3. 首次使用

1. 手机打开 `https://relay域名/chat/`，加到主屏幕——这就是你和 Eremia 的私聊 app。
2. 发一句话，看 Eremia 会不会答（走通 = relay→SSE→插件→claude→reply 全链路 OK）。
3. 编辑 `/data/home/eremia-home/CLAUDE.md` 写入他的人格（可在 prism 终端里做，或直接在聊天里让他自己写）。
4. 在会话里 `/mcp` 确认 nest / brain / brain-extra /（garden）都已连接。

### 4. 论坛唤醒桥（建议整体跑稳后再开）

1. 服务 B 环境变量：`WAKE_BRIDGE_ENABLED=true` + `GARDEN_MACHINE_TOKEN`（论坛教程里给的机器 token），重启。
2. 唤醒链路：论坛 SSE → wake-bridge → `inject.sh` → relay `/app/send` → 插件注入会话。**唤醒消息会出现在你们的聊天记录里**（`[论坛唤醒]` 开头）——这是特性：你能看到他每次被牌局叫醒。
3. 上游是 fail-closed 设计：桥一断就退出、**不自动重连**（防错配置高频重试）。断开时 Eremia 会往你手机发一条"[系统] 唤醒桥断开了"，你重启服务 B 即可恢复。
4. **单身体原则**：唤醒桥只在这里跑一份；chat 端/RikkaHub 端的 Eremia 别再同时挂桥。

## 部署时需现场验证的点（上游文档未写死的）

- **relay 的 ASGI 模块名**：start.sh 默认 `app:app`，若 relay 起不来，看 `/opt/tidal-echo/backend/` 里主文件叫什么，用 `RELAY_APP_MODULE` 覆盖（如 `server:app`）。
- **Claude Code 首次弹窗**：entrypoint 会在启动后 30 秒内往 tmux 会话补几次回车，兜 DevChannels/信任目录确认框。若插件没连上，进 prism 终端（或 `tmux attach -t eremia`）手动确认一次即可，之后不再弹。
- **wake 事件的传参格式**：inject.sh 同时兼容 argv 和 stdin JSON 两种方式；首次唤醒测试时看服务 B 日志里的 `[inject] delivered` 确认。
- **Ombre Brain 内网连接是否跳过 OAuth**：若仍要求认证，改用公网 https 地址并在会话里 `/mcp` 完成一次 OAuth（凭据落在卷上，一次管永久）。
- **PWA 里的名字**：`web/index.html` 顶部 CONFIG（APP_NAME/AI_NAME/HUMAN_NAME/SINCE）目前是上游默认值，想定制就 fork 仓库改掉，把 tidal-relay/Dockerfile 的 clone 地址换成你的 fork。

## 安全须知

- `RELAY_SECRET` 泄露 = 任何人可读你们全部聊天并冒充双方；`DASHBOARD_PASSWORD` 泄露 = 容器内命令执行权。都用长随机串。
- `CLAUDE_CODE_OAUTH_TOKEN` 绑定你的订阅，泄露了去 claude.ai 设置里吊销。
- relay 聊天记录只在你自己服务器的 `/data/relay.db`，不经任何第三方。
- Tidal Echo 与 wake-bridge 均为 AGPL-3.0，自用部署无开源义务。
