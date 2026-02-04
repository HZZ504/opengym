import os
import uuid
import json
import sqlite3
from datetime import datetime, timedelta, time as dtime
from typing import Dict, Any, Optional

import yaml
import requests
from fastapi import FastAPI, Request, HTTPException
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from zoneinfo import ZoneInfo

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "pushup_mvp.db")
CONFIG_PATH = os.path.join(BASE_DIR, "config.yaml")

app = FastAPI(title="OpenClaw Telegram Pushup MVP")


def load_config() -> Dict[str, Any]:
    if not os.path.exists(CONFIG_PATH):
        raise FileNotFoundError("config.yaml not found. Copy config.example.yaml to config.yaml and fill values.")
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


config = load_config()
TZ = ZoneInfo(config.get("timezone", "UTC"))


def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db_connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                date TEXT NOT NULL,
                time TEXT NOT NULL,
                slot_id TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                timeout_at INTEGER NOT NULL,
                clicked_at INTEGER
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT,
                user_id TEXT,
                event_type TEXT,
                created_at INTEGER NOT NULL,
                meta TEXT
            )
            """
        )


def slot_index(slots: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    index = {}
    for group in slots.values():
        for slot in group:
            index[slot["id"]] = slot
    return index


SLOT_INDEX = slot_index(config["slots"])


def send_telegram_message(chat_id: str, text: str, buttons: Optional[list] = None, image: Optional[str] = None):
    token = config["telegram"]["bot_token"]
    if not token:
        raise RuntimeError("Missing Telegram bot_token in config.yaml")

    reply_markup = {"inline_keyboard": buttons} if buttons else None

    if image:
        url = f"https://api.telegram.org/bot{token}/sendPhoto"
        payload = {
            "chat_id": chat_id,
            "caption": text,
            "parse_mode": "HTML",
        }
        if reply_markup:
            payload["reply_markup"] = json.dumps(reply_markup)
        files = None
        if image.startswith("http://") or image.startswith("https://"):
            payload["photo"] = image
        else:
            image_path = image
            if not os.path.isabs(image_path):
                image_path = os.path.join(BASE_DIR, image_path)
            files = {"photo": open(image_path, "rb")}
        resp = requests.post(url, data=payload, files=files)
    else:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = json.dumps(reply_markup)
        resp = requests.post(url, data=payload)

    if not resp.ok:
        raise HTTPException(status_code=500, detail=f"Telegram send failed: {resp.text}")


def answer_callback(callback_id: str, text: str):
    token = config["telegram"]["bot_token"]
    if not token or not callback_id:
        return
    url = f"https://api.telegram.org/bot{token}/answerCallbackQuery"
    requests.post(url, data={"callback_query_id": callback_id, "text": text, "show_alert": False})


def build_message(slot: Dict[str, Any], time_str: str) -> str:
    return (
        f"⏰ {time_str} 训练提醒（{slot['name']}）\n"
        f"动作：{slot['exercise']}\n"
        f"目标：{slot['reps']}\n"
        f"插位：照图插手柄👇\n\n"
        f"⏳ 60分钟内未打卡 = 自动记为未完成"
    )


def calendar_buttons():
    return [[
        {"text": "📅 今天", "callback_data": "cal:today"},
        {"text": "📅 本周", "callback_data": "cal:week"},
        {"text": "📅 本月", "callback_data": "cal:month"},
    ]]


def format_plan(user_id: str, mode: str) -> str:
    now = datetime.now(TZ)
    if mode == "today":
        start = now.date()
        end = now.date()
        title = "🌤️ 今日训练计划"
    elif mode == "week":
        start = (now - timedelta(days=now.weekday())).date()
        end = start + timedelta(days=6)
        title = "🗓️ 本周训练计划"
    else:
        start = now.replace(day=1).date()
        if start.month == 12:
            end = start.replace(year=start.year + 1, month=1, day=1) - timedelta(days=1)
        else:
            end = start.replace(month=start.month + 1, day=1) - timedelta(days=1)
        title = "🗓️ 本月训练计划"

    with db_connect() as conn:
        rows = conn.execute(
            """
            SELECT date, time, slot_id, status
            FROM tasks
            WHERE user_id = ? AND date BETWEEN ? AND ?
            ORDER BY date ASC, time ASC
            """,
            (str(user_id), start.isoformat(), end.isoformat()),
        ).fetchall()

    lines = [title, "", "🎯 计划总览（含完成情况）"]
    if not rows:
        lines.append("（暂无记录）")
        return "\n".join(lines)

    def status_icon(s: str) -> str:
        return {"done": "✅", "skip": "⏭️", "timeout": "⏳", "snoozed": "🕒", "pending": "▫️"}.get(s, "▫️")

    for r in rows:
        slot = SLOT_INDEX.get(r["slot_id"], {"name": r["slot_id"]})
        lines.append(f"{status_icon(r['status'])} {r['date']} {r['time']} · {slot['name']}")

    # summary
    counts = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    total = len(rows)
    done = counts.get("done", 0)
    skip = counts.get("skip", 0)
    timeout = counts.get("timeout", 0)
    snoozed = counts.get("snoozed", 0)
    pending = counts.get("pending", 0)
    rate = f"{(done/total*100):.0f}%" if total else "0%"

    lines += [
        "",
        "—" * 18,
        f"完成率：{rate}",
        f"完成 {done} / 跳过 {skip} / 超时 {timeout} / 延后 {snoozed} / 待完成 {pending}",
        "—" * 18,
    ]

    return "\n".join(lines)


def create_task(user_id: str, time_str: str, slot_id: str) -> str:
    now = datetime.now(TZ)
    task_id = str(uuid.uuid4())
    timeout_at = int((now + timedelta(minutes=config["reminders"]["timeout_minutes"])).timestamp())
    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO tasks (task_id, user_id, date, time, slot_id, status, created_at, timeout_at, clicked_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                str(user_id),
                now.strftime("%Y-%m-%d"),
                time_str,
                slot_id,
                "pending",
                int(now.timestamp()),
                timeout_at,
                None,
            ),
        )
    return task_id


def update_task_status(task_id: str, status: str):
    now = int(datetime.now(TZ).timestamp())
    with db_connect() as conn:
        conn.execute(
            "UPDATE tasks SET status = ?, clicked_at = ? WHERE task_id = ?",
            (status, now, task_id),
        )
        conn.execute(
            "INSERT INTO events (task_id, user_id, event_type, created_at, meta) VALUES (?, ?, ?, ?, ?)",
            (task_id, None, status, now, None),
        )


def log_event(msg: str):
    with open(os.path.join(BASE_DIR, "events.log"), "a", encoding="utf-8") as f:
        f.write(f"{datetime.now(TZ).isoformat()} {msg}\n")


def send_reminder_for_user(user: Dict[str, Any], time_str: str, slot_id: str):
    task_id = create_task(user["chat_id"], time_str, slot_id)
    slot = SLOT_INDEX[slot_id]
    buttons = [[
        {"text": "✅ 完成", "callback_data": f"done:{task_id}"},
        {"text": "⏭️ 跳过", "callback_data": f"skip:{task_id}"},
        {"text": "🕒 延后10分钟", "callback_data": f"snooze10:{task_id}"},
    ]]
    send_telegram_message(
        chat_id=user["chat_id"],
        text=build_message(slot, time_str),
        buttons=buttons,
        image=slot.get("image")
    )


def schedule_daily_reminders(scheduler: BackgroundScheduler):
    rotation = config["rotation"]
    for weekday, times in rotation.items():
        for time_str, slot_id in times.items():
            hour, minute = map(int, time_str.split(":"))
            scheduler.add_job(
                func=send_reminders_batch,
                trigger=CronTrigger(day_of_week=weekday, hour=hour, minute=minute, timezone=TZ),
                args=[time_str, slot_id],
                id=f"reminder_{weekday}_{time_str}",
                replace_existing=True,
            )


def send_reminders_batch(time_str: str, slot_id: str):
    for user in config["telegram"]["users"]:
        send_reminder_for_user(user, time_str, slot_id)


def timeout_scan():
    now = int(datetime.now(TZ).timestamp())
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT task_id FROM tasks WHERE status = 'pending' AND timeout_at <= ?",
            (now,),
        ).fetchall()
        for row in rows:
            conn.execute(
                "UPDATE tasks SET status = 'timeout', clicked_at = ? WHERE task_id = ?",
                (now, row["task_id"]),
            )


def weekly_report():
    now = datetime.now(TZ)
    start = (now - timedelta(days=7)).date().isoformat()
    end = now.date().isoformat()

    for user in config["telegram"]["users"]:
        with db_connect() as conn:
            rows = conn.execute(
                """
                SELECT status, COUNT(*) as cnt
                FROM tasks
                WHERE user_id = ? AND date BETWEEN ? AND ?
                GROUP BY status
                """,
                (str(user["chat_id"]), start, end),
            ).fetchall()

        counts = {r["status"]: r["cnt"] for r in rows}
        total = sum(counts.values())
        done = counts.get("done", 0)
        skip = counts.get("skip", 0)
        timeout = counts.get("timeout", 0)
        snoozed = counts.get("snoozed", 0)
        completion_rate = f"{(done / total * 100):.0f}%" if total else "0%"

        text = (
            f"📊 本周训练周报\n"
            f"完成率：{completion_rate}\n"
            f"完成：{done}\n"
            f"跳过：{skip}\n"
            f"超时：{timeout}\n"
            f"延后：{snoozed}\n"
            f"总任务：{total}\n"
        )
        send_telegram_message(chat_id=user["chat_id"], text=text)


@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()
    if "callback_query" in data:
        cb = data["callback_query"]
        cb_id = cb.get("id")
        cb_data = cb.get("data", "")
        if ":" not in cb_data:
            return {"ok": True}
        action, task_id = cb_data.split(":", 1)

        chat_id = str(cb["message"]["chat"]["id"])
        log_event(f"callback action={action} task_id={task_id} chat_id={chat_id} cb_id={cb_id}")

        if action == "cal":
            plan_text = format_plan(chat_id, task_id)
            answer_callback(cb_id, "已生成计划 📅")
            send_telegram_message(chat_id, plan_text)
            return {"ok": True}

        if action == "done":
            update_task_status(task_id, "done")
            answer_callback(cb_id, "已记录：完成 ✅")
            send_telegram_message(chat_id, "已记录：完成 ✅")
        elif action == "skip":
            update_task_status(task_id, "skip")
            answer_callback(cb_id, "已记录：跳过 ⏭️")
            send_telegram_message(chat_id, "已记录：跳过 ⏭️")
        elif action == "snooze10":
            update_task_status(task_id, "snoozed")
            answer_callback(cb_id, "已延后10分钟 🕒")
            send_telegram_message(chat_id, "已延后10分钟 🕒")
            # Create new task 10 minutes later
            snooze_minutes = config["reminders"]["snooze_minutes"]
            for user in config["telegram"]["users"]:
                if str(user["chat_id"]) == chat_id:
                    now = datetime.now(TZ) + timedelta(minutes=snooze_minutes)
                    time_str = now.strftime("%H:%M")
                    # Reuse original slot
                    with db_connect() as conn:
                        row = conn.execute("SELECT slot_id FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
                    if row:
                        send_reminder_for_user(user, time_str, row["slot_id"])

        return {"ok": True}

    if "message" in data:
        text = (data.get("message", {}).get("text") or "").strip().lower()
        chat_id = str(data.get("message", {}).get("chat", {}).get("id"))
        if text in ["/calendar", "calendar", "日历", "计划"]:
            send_telegram_message(chat_id, "📅 请选择查看范围：", buttons=calendar_buttons())
            return {"ok": True}

    return {"ok": True}


@app.get("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    init_db()
    scheduler = BackgroundScheduler(timezone=TZ)
    schedule_daily_reminders(scheduler)
    scheduler.add_job(timeout_scan, "interval", minutes=1, id="timeout_scan", replace_existing=True)

    # Weekly report schedule
    weekly = config["weekly_report"]
    w_hour, w_min = map(int, weekly["time"].split(":"))
    scheduler.add_job(
        weekly_report,
        trigger=CronTrigger(day_of_week=weekly["day_of_week"], hour=w_hour, minute=w_min, timezone=TZ),
        id="weekly_report",
        replace_existing=True,
    )

    scheduler.start()

    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8001)
