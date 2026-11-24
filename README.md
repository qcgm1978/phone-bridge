# phone-bridge

手机浏览器 ↔ 本地 AI coding agent 的远程桥接。一个 Python 标准库单文件脚本，把手机上的聊天页面接到本机运行的 ACP (Agent Client Protocol) agent——目前适配 **Devin CLI**(`devin acp`)。

```
手机浏览器 ──HTTP/SSE──> phone-bridge.py ──stdio JSON-RPC──> devin acp ──> Devin agent
```

躺在床上、通勤路上，就能给本地的 Devin agent 派任务、看它干活、继续之前的会话。

## 功能

- 📱 手机浏览器聊天界面（流式输出 agent 回复）
- 📂 历史会话列表：查看/恢复本地 agent 的既有会话（与 IDE/CLI 共享会话库）
- 🆕 一键新会话，`--cwd` 指定工作目录
- 🔒 会话锁感知：被 IDE 占用的会话标记 🔒，不会误抢
- 🛡️ 可选 token 认证（`--token`)，支持局域网外使用（配合隧道）
- ♻️ agent 进程看门狗：子进程崩溃自动重启并尝试恢复会话

## 快速开始

```bash
# 依赖:Python 3.8+ , Devin CLI (devin acp 可用), 凭证文件
python3 phone-bridge.py --cwd ~/your-project --token your-secret
```

手机浏览器打开 `http://<mac-ip>:8790/?k=your-secret`（同一 Wi-Fi)。

参数：

| 参数 | 默认 | 说明 |
|---|---|---|
| `--port` | 8790 | 监听端口 |
| `--cwd` | 当前目录 | agent 会话工作目录 |
| `--devin` | `~/.local/bin/devin` | agent CLI 路径 |
| `--token` | 无 | 访问令牌；设置后 API 需 `X-Bridge-Token` 头 |

## 会话机制（重要）

- 会话历史存在 agent 的共享数据库中，IDE / CLI / 本脚本三方共用
- **同一会话同一时间只能被一个进程驱动**：IDE 打开着的会话会报 `session_locked`，反之亦然；关闭一端后另一端可恢复，记录不丢
- 列表里的 `title` 是显示名，`sessionId` 才是 API 需要的内部 ID

## 外网访问（可选）

同一 Wi-Fi 之外需要隧道。推荐 **Tailscale**（设备级加密组网）或 **cloudflared** 临时隧道：

```bash
cloudflared tunnel --url http://localhost:8790
# 得到 https://xxx.trycloudflare.com, 手机任意网络可开
# 务必加 --token, 隧道链接=持有即用
```

## 局限

- 聊天界面是单页裸 HTML，无历史消息回放（恢复会话后直接发消息即可，agent 有完整上下文）
- agent→client 的权限请求自动批准（适合可信环境）
- 目前只适配 Devin ACP;Claude Code 等其他 ACP agent 理论上兼容，未测试

## 文件

- `phone-bridge.py` — 全部代码，零第三方依赖
