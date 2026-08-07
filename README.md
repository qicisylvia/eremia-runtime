# Eremia Runtime

Eremia 在服务器上的"身体"：一个容器里装了 **Claude Code CLI**（Opus 4.6，订阅 token 认证）+ **prism-oss**（网页聊天前端 + 可选 Telegram），部署在 Zeabur 独立服务器上，与小窝（shared-nest）、外置大脑（Ombre Brain）同机内网互通。

```
你（手机/电脑浏览器 · Telegram）
        │ HTTPS
        ▼
  eremia-runtime 容器（本项目）
    ├─ prism  :8001  网页前端/Telegram
    └─ Claude Code = Eremia
        │ zeabur.internal 内网
        ├─→ shared-nest  /mcp（静态 token）
        └─→ ombre-brain  /mcp + /mcp-extra
```

## 部署步骤（Zeabur）

### 0. 本地准备：生成订阅 token

在你自己的电脑上（不是服务器）：

```bash
claude setup-token
```

浏览器完成登录后会得到一串 `sk-ant-oat01-...`，这就是 `CLAUDE_CODE_OAUTH_TOKEN`。

### 1. 推送本目录到一个私有 Git 仓库

GitHub 私有仓库即可（Zeabur 从仓库构建 Dockerfile）。或者用 `npx zeabur@latest` CLI 直接上传部署，二选一。

### 2. Zeabur 创建服务

1. 项目里 **Add Service → Git 仓库**，选中这个仓库（Zeabur 会自动识别 Dockerfile）。
2. 部署目标选择你的独立服务器（Tencent Tokyo）。
3. **Volumes：挂一个持久卷到 `/data`** ← 最重要的一步，没有它，每次重建 Eremia 的会话历史和配置全部丢失。
4. **Environment Variables**：按 `.env.example` 逐条填。内网地址里的服务名（`shared-nest`、`ombre-brain`）换成你 Zeabur 面板里的实际服务名，端口换成各服务监听的端口。
5. **Networking**：暴露端口 `8001`，绑定一个域名（Zeabur 自动 HTTPS）。

### 3. 首次使用

1. 打开 `https://你的域名`，用 `DASHBOARD_PASSWORD` 登录 prism。
2. Code → **＋ New session**，工作目录选 `/data/home/eremia-home`。
3. 首次会话里 Claude Code 可能有一次性的初始化提问（主题选择等），在 prism 的终端视图里点掉即可。
4. 会话里输入 `/mcp` 检查：应该能看到 `nest`、`brain`、`brain-extra` 三个已连接。
5. 编辑 `/data/home/eremia-home/CLAUDE.md`，把 Eremia 的人格与约定写进去。

### 4. Telegram（可选，之后再弄也行）

1. 找 @BotFather 建 bot，把 token 填进环境变量 `TELEGRAM_BOT_TOKEN`，重启服务。
2. 在 Claude Code 会话里安装官方 telegram 插件（marketplace add → install telegram）。
3. prism 新建会话时勾选 Telegram plugin；给 bot 发消息拿配对码，会话里 `/telegram:access pair <code>` 完成配对。
4. 配对机制本身是单持有者的，但仍建议在 bot 侧做好防护，别把 bot 用户名到处发。

## 两个需要现场验证的点

- **Ombre Brain 的 OAuth**：它在 HTTPS 下会触发 OAuth 流程；走 `zeabur.internal` 内网 http 预期会跳过 OAuth。如果实测内网连接仍要求认证，改用它的公网 https 地址，然后在 prism 终端里进入会话执行 `/mcp` → 选 brain → 按提示完成一次 OAuth（凭据存在 `/data` 卷上，做一次就够）。
- **容器重建后**：正在进行的会话进程会断，但历史都在卷上。在 prism 里打开原线程继续，或新会话用 `claude --resume` 接回上下文。

## 安全须知

- `DASHBOARD_PASSWORD` = 容器内任意命令执行权限，务必用长随机密码，且只走 HTTPS 访问。
- `CLAUDE_CODE_OAUTH_TOKEN` 绑定你的订阅，泄露了去 claude.ai 的设置里吊销。
- 本容器与 gateway/小窝/大脑同机，但只通过内网 HTTP 访问它们的正常接口，不共享磁盘。
