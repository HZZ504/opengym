# OpenClaw × Telegram 俯卧撑支架训练系统 MVP (v0.1)

这是一个可执行的 MVP：工作日定时提醒 + 按钮打卡 + 超时判定 + 周报。

## 功能
- 周一～周五固定提醒
- 每次提醒附插位演示图 + Inline Buttons（完成/跳过/延后）
- 60分钟超时自动记为 timeout
- 每周一 10:25 发送个人周报

## 快速开始

```bash
cd /home/hzz/clawd/pushup_mvp
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
# 编辑 config.yaml，填入 bot_token / chat_id / 插位图片
python app.py
```

## Telegram Webhook 配置

将 Telegram webhook 指向你的服务器：

```bash
curl -X POST "https://api.telegram.org/bot<YOUR_TOKEN>/setWebhook" \
  -d "url=https://<YOUR_PUBLIC_URL>/webhook"
```

> 如果使用 Cloudflare Tunnel：
```bash
cloudflared tunnel --url http://localhost:8001
```

## 数据库
- SQLite: `pushup_mvp.db`
- 表：`tasks`, `events`

## 任务字段
- task_id, user_id, date, time, slot_id, status, created_at, timeout_at, clicked_at

## 状态
- pending / done / skip / snoozed / timeout

## 注意
- 插位图片可用 URL 或本地路径
- 时间与时区由 config.yaml 控制
