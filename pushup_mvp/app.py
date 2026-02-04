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


def main_menu_keyboard():
    return {
        "keyboard": [
            [{"text": "📌 今日计划"}, {"text": "🗓️ 工作日完整计划"}],
        ],
        "resize_keyboard": True,
        "one_time_keyboard": False,
    }


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

    return format_range_plan(user_id, start, end, title)


def format_range_plan(user_id: str, start_date, end_date, title: str) -> str:
    rotation = config["rotation"]
    times = config["reminders"]["times"]
    weekday_map = {0: "mon", 1: "tue", 2: "wed", 3: "thu", 4: "fri"}

    plan = []
    d = start_date
    while d <= end_date:
        wk = weekday_map.get(d.weekday())
        if wk in rotation:
            for t in times:
                slot_id = rotation[wk].get(t)
                if slot_id:
                    plan.append((d.isoformat(), t, slot_id))
        d = d + timedelta(days=1)

    with db_connect() as conn:
        rows = conn.execute(
            """
            SELECT date, time, slot_id, status
            FROM tasks
            WHERE user_id = ? AND date BETWEEN ? AND ?
            """,
            (str(user_id), start_date.isoformat(), end_date.isoformat()),
        ).fetchall()
    status_map = {(r["date"], r["time"], r["slot_id"]): r["status"] for r in rows}

    lines = [title, "", "🎯 计划总览（含完成情况）"]
    if not plan:
        lines.append("（暂无计划）")
        return "\n".join(lines)

    def status_icon(s: str) -> str:
        return {"done": "✅", "skip": "⏭️", "timeout": "⏳", "snoozed": "🕒", "pending": "▫️", None: "▫️"}.get(s, "▫️")

    counts = {"done": 0, "skip": 0, "timeout": 0, "snoozed": 0, "pending": 0}
    for date_str, time_str, slot_id in plan:
        status = status_map.get((date_str, time_str, slot_id), "pending")
        counts[status] = counts.get(status, 0) + 1
        slot = SLOT_INDEX.get(slot_id, {"name": slot_id})
        lines.append(f"{status_icon(status)} {date_str} {time_str} · {slot['name']}")

    total = len(plan)
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


def format_weekday_plan() -> str:
    rotation = config["rotation"]
    times = config["reminders"]["times"]
    order = ["mon", "tue", "wed", "thu", "fri"]
    names = {"mon": "周一", "tue": "周二", "wed": "周三", "thu": "周四", "fri": "周五"}
    lines = ["🗓️ 工作日完整计划", ""]
    for wk in order:
        if wk not in rotation:
            continue
        line = [names[wk]]
        for t in times:
            line.append(rotation[wk].get(t, "-"))
        lines.append(" / ".join(line))
    lines += ["", "说明：C1/C2 已映射为背部 D1/D2（按固定表显示 D1/D2）"]
    return "\n".join(lines)


def today_plan_message(user_id: str):
    now = datetime.now(TZ)
    title = "🌤️ 今日训练计划（可修改状态）"
    return format_range_plan(user_id, now.date(), now.date(), title)


def today_plan_buttons(user_id: str):
    now = datetime.now(TZ).date()
    rotation = config["rotation"]
    times = config["reminders"]["times"]
    weekday_map = {0: "mon", 1: "tue", 2: "wed", 3: "thu", 4: "fri"}
    wk = weekday_map.get(now.weekday())
    if wk not in rotation:
        return None
    buttons = []
    for t in times:
        slot_id = rotation[wk].get(t)
        if not slot_id:
            continue
        base = f"set:{now.isoformat()}|{t}|{slot_id}|"
        buttons.append([
            {"text": f"✅ {t}", "callback_data": base + "done"},
            {"text": f"⏭️ {t}", "callback_data": base + "skip"},
            {"text": f"▫️ {t}", "callback_data": base + "pending"},
        ])
    return buttons


def next_action_message(user_id: str) -> str:
    now = datetime.now(TZ)
    rotation = config["rotation"]
    times = config["reminders"]["times"]
    weekday_map = {0: "mon", 1: "tue", 2: "wed", 3: "thu", 4: "fri"}
    wk = weekday_map.get(now.weekday())
    if wk not in rotation:
        return "今天非工作日，没有计划动作。"

    for t in times:
        hour, minute = map(int, t.split(":"))
        t_dt = datetime.combine(now.date(), dtime(hour, minute), tzinfo=TZ)
        if t_dt >= now:
            slot_id = rotation[wk].get(t)
            slot = SLOT_INDEX.get(slot_id, {"name": slot_id})
            cues = slot.get("cues", "核心收紧，背部中立，动作稳定。")
            return (
                f"⏭️ 下一个动作\n"
                f"时间：{t}\n"
                f"动作：{slot['exercise']}（{slot['name']}）\n"
                f"目标：{slot['reps']}\n"
                f"提示：{cues}"
            )

    return "今天的动作已完成。"


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


def upsert_task(user_id: str, date_str: str, time_str: str, slot_id: str, status: str):
    now = int(datetime.now(TZ).timestamp())
    with db_connect() as conn:
        row = conn.execute(
            "SELECT task_id FROM tasks WHERE user_id=? AND date=? AND time=? AND slot_id=?",
            (str(user_id), date_str, time_str, slot_id),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE tasks SET status=?, clicked_at=? WHERE task_id=?",
                (status, now, row["task_id"]),
            )
            return row["task_id"]
        task_id = str(uuid.uuid4())
        timeout_at = int((datetime.now(TZ) + timedelta(minutes=config["reminders"]["timeout_minutes"])).timestamp())
        conn.execute(
            """
            INSERT INTO tasks (task_id, user_id, date, time, slot_id, status, created_at, timeout_at, clicked_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (task_id, str(user_id), date_str, time_str, slot_id, status, now, timeout_at, now),
        )
        return task_id


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


def weekly_report_json(user_id: str, start_date, end_date) -> Dict[str, Any]:
    rotation = config["rotation"]
    times = config["reminders"]["times"]
    weekday_map = {0: "mon", 1: "tue", 2: "wed", 3: "thu", 4: "fri"}

    plan = []
    d = start_date
    while d <= end_date:
        wk = weekday_map.get(d.weekday())
        if wk in rotation:
            for t in times:
                slot_id = rotation[wk].get(t)
                if slot_id:
                    plan.append((d.isoformat(), t, slot_id))
        d = d + timedelta(days=1)

    with db_connect() as conn:
        rows = conn.execute(
            """
            SELECT task_id, date, time, slot_id, status, created_at, timeout_at, clicked_at
            FROM tasks
            WHERE user_id = ? AND date BETWEEN ? AND ?
            """,
            (str(user_id), start_date.isoformat(), end_date.isoformat()),
        ).fetchall()

    status_map = {(r["date"], r["time"], r["slot_id"]): r["status"] for r in rows}

    counts = {"done": 0, "skip": 0, "timeout": 0, "snoozed": 0, "pending": 0}
    by_day = {}
    by_slot = {}

    for date_str, time_str, slot_id in plan:
        status = status_map.get((date_str, time_str, slot_id), "pending")
        counts[status] = counts.get(status, 0) + 1

        by_day.setdefault(date_str, {"total_tasks": 0, "done": 0, "skipped": 0, "timeout": 0})
        by_day[date_str]["total_tasks"] += 1
        if status == "done":
            by_day[date_str]["done"] += 1
        elif status == "skip":
            by_day[date_str]["skipped"] += 1
        elif status == "timeout":
            by_day[date_str]["timeout"] += 1

        by_slot.setdefault(time_str, {"total_tasks": 0, "done": 0, "skipped": 0, "timeout": 0})
        by_slot[time_str]["total_tasks"] += 1
        if status == "done":
            by_slot[time_str]["done"] += 1
        elif status == "skip":
            by_slot[time_str]["skipped"] += 1
        elif status == "timeout":
            by_slot[time_str]["timeout"] += 1

    total = len(plan)
    done = counts.get("done", 0)
    skip = counts.get("skip", 0)
    timeout = counts.get("timeout", 0)
    snoozed = counts.get("snoozed", 0)
    pending = counts.get("pending", 0)
    done_rate = round(done / total, 2) if total else 0.0

    # streak days (consecutive days with done > 0 in the period)
    streak = 0
    best_time_slot = None
    worst_time_slot = None

    # compute best/worst time slot
    slot_rates = {}
    for t, stats in by_slot.items():
        if stats["total_tasks"]:
            slot_rates[t] = stats["done"] / stats["total_tasks"]
    if slot_rates:
        best_time_slot = max(slot_rates, key=slot_rates.get)
        worst_time_slot = min(slot_rates, key=slot_rates.get)

    # streak: count consecutive days from end_date backwards with done>0
    d = end_date
    while d >= start_date:
        day_stats = by_day.get(d.isoformat(), {})
        if day_stats.get("done", 0) > 0:
            streak += 1
            d = d - timedelta(days=1)
        else:
            break

    # tasks list
    tasks = []
    for r in rows:
        def ts_to_iso(ts):
            if ts is None:
                return None
            return datetime.fromtimestamp(ts, TZ).isoformat()
        tasks.append({
            "task_id": r["task_id"],
            "date": r["date"],
            "time_slot": r["time"],
            "slot_id": r["slot_id"],
            "status": r["status"],
            "created_at": ts_to_iso(r["created_at"]),
            "timeout_at": ts_to_iso(r["timeout_at"]),
            "clicked_at": ts_to_iso(r["clicked_at"]),
        })

    # assemble json
    week_id = f"{start_date.isocalendar().year}-W{start_date.isocalendar().week:02d}"

    # suggestion logic
    suggestion_text = ""
    if done_rate >= 0.8:
        suggestion_text = "A. 完成率 ≥ 80% 表现很稳！下周保持节奏即可。\n建议：把“最轻松的那次”加 2 次（或下放慢 3 秒）提升效果。"
    elif done_rate >= 0.5:
        suggestion_text = "B. 完成率 50%～79% 不错！下周目标：完成率冲到 80%。\n建议：优先保证 10:40 和 16:30 两次（最能缓解久坐）。"
    else:
        suggestion_text = "C. 完成率 < 50% 这周比较忙也没关系，下周先把习惯建立起来。\n建议：只要完成每天任意 2 次就算赢（优先 10:40 + 16:30）。"

    if worst_time_slot:
        suggestion_text += f"\nD. 你最容易错过的是 {worst_time_slot}。\n建议：看到提醒先点“延后10分钟”，别让它直接超时。"

    report = {
        "report_type": "weekly_workout_report",
        "version": "v0.1",
        "generated_at": datetime.now(TZ).isoformat(),
        "timezone": config.get("timezone", "UTC"),
        "user": {
            "user_id": f"tg:{user_id}",
            "display_name": next((u["name"] for u in config["telegram"]["users"] if str(u["chat_id"]) == str(user_id)), "user"),
        },
        "period": {
            "week_id": week_id,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "workdays_only": True,
        },
        "schedule_config": {
            "workout_times": times,
            "timeout_minutes": config["reminders"]["timeout_minutes"],
        },
        "summary": {
            "total_tasks": total,
            "done": done,
            "skipped": skip,
            "timeout": timeout,
            "done_rate": done_rate,
            "streak_days": streak,
        },
        "by_day": [
            {
                "date": d,
                "weekday": datetime.fromisoformat(d).weekday() + 1,
                **stats
            } for d, stats in sorted(by_day.items())
        ],
        "by_time_slot": [
            {
                "time_slot": t,
                **stats,
                "done_rate": round(stats["done"] / stats["total_tasks"], 2) if stats["total_tasks"] else 0.0,
            } for t, stats in sorted(by_slot.items())
        ],
        "tasks": tasks,
        "insights": {
            "best_time_slot": best_time_slot or "",
            "worst_time_slot": worst_time_slot or "",
            "most_common_status": max(counts, key=counts.get) if total else "",
        },
        "suggestion": {
            "level": "medium",
            "text": suggestion_text,
        },
    }

    return report


def weekly_report_text(user_id: str, start_date, end_date) -> str:
    report = weekly_report_json(user_id, start_date, end_date)

    week_range = f"{report['period']['start_date']} ~ {report['period']['end_date']}"
    user_name = report['user']['display_name']
    total_tasks = report['summary']['total_tasks']
    done_count = report['summary']['done']
    skip_count = report['summary']['skipped']
    timeout_count = report['summary']['timeout']
    done_rate = int(report['summary']['done_rate'] * 100)
    streak_days = report['summary']['streak_days']

    slot_map = {r['time_slot']: r for r in report['by_time_slot']}
    def slot_line(t):
        r = slot_map.get(t, {"done": 0, "total_tasks": 0})
        return f"- {t}：完成 {r['done']}/{r['total_tasks']}"

    suggestion_text = report['suggestion']['text']

    text = (
        f"📊 上周训练周报（{week_range}）\n"
        f"👤 用户：{user_name}\n"
        f"📅 统计：周一～周五（共 {total_tasks} 次提醒）\n"
        f"✅ 完成：{done_count}\n"
        f"⏭️ 跳过：{skip_count}\n"
        f"⏰ 超时未做：{timeout_count}\n"
        f"📈 完成率：{done_rate}%\n"
        f"🔥 连续打卡：{streak_days} 天\n\n"
        f"🧩 时间段表现\n"
        f"{slot_line('10:40')}\n"
        f"{slot_line('11:40')}\n"
        f"{slot_line('14:00')}\n"
        f"{slot_line('16:30')}\n"
        f"{slot_line('19:10')}\n\n"
        f"🎯 下周建议（MVP）\n"
        f"{suggestion_text}\n\n"
        f"继续加油！本周从 10:40 第一条开始打卡💪"
    )
    return text


def weekly_report():
    now = datetime.now(TZ)
    start = (now - timedelta(days=now.weekday()+7)).date()
    end = (start + timedelta(days=6))

    for user in config["telegram"]["users"]:
        text = weekly_report_text(user["chat_id"], start, end)
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

        if action == "set":
            # set:YYYY-MM-DD|HH:MM|SLOT|status
            try:
                date_str, time_str, slot_id, status = task_id.split("|", 3)
                upsert_task(chat_id, date_str, time_str, slot_id, status)
                answer_callback(cb_id, f"已更新：{status}")
            except Exception:
                answer_callback(cb_id, "更新失败")
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
        if "@" in text and text.startswith("/"):
            text = text.split("@", 1)[0]
        if text in ["/calendar", "calendar", "日历", "计划"]:
            send_telegram_message(chat_id, "📅 请选择查看范围：", buttons=calendar_buttons())
            return {"ok": True}
        if text in ["/next", "next", "下一个动作"]:
            msg = next_action_message(chat_id)
            # send with image if next slot exists
            now = datetime.now(TZ)
            rotation = config["rotation"]
            times = config["reminders"]["times"]
            weekday_map = {0: "mon", 1: "tue", 2: "wed", 3: "thu", 4: "fri"}
            wk = weekday_map.get(now.weekday())
            image = None
            if wk in rotation:
                for t in times:
                    hour, minute = map(int, t.split(":"))
                    t_dt = datetime.combine(now.date(), dtime(hour, minute), tzinfo=TZ)
                    if t_dt >= now:
                        slot_id = rotation[wk].get(t)
                        slot = SLOT_INDEX.get(slot_id)
                        if slot:
                            image = slot.get("image")
                        break
            send_telegram_message(chat_id, msg, image=image)
            return {"ok": True}
        if text in ["/today", "today", "今日计划"]:
            send_telegram_message(chat_id, today_plan_message(chat_id))
            buttons = today_plan_buttons(chat_id)
            if buttons:
                send_telegram_message(chat_id, "点击下方按钮修改完成状态：", buttons=buttons)
            return {"ok": True}
        if text in ["/weekday", "weekday", "工作日完整计划"]:
            send_telegram_message(chat_id, format_weekday_plan())
            return {"ok": True}
        if text in ["/weekreport", "weekreport", "周报"]:
            now = datetime.now(TZ)
            start = (now - timedelta(days=now.weekday()+7)).date()
            end = (start + timedelta(days=6))
            send_telegram_message(chat_id, weekly_report_text(chat_id, start, end))
            return {"ok": True}
        if text in ["/start", "start", "菜单", "帮助"]:
            send_telegram_message(chat_id, "📍 请选择功能：", buttons=None, image=None)
            # set reply keyboard
            token = config["telegram"]["bot_token"]
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data={
                    "chat_id": chat_id,
                    "text": "📌 功能菜单已开启",
                    "reply_markup": json.dumps(main_menu_keyboard()),
                },
            )
            return {"ok": True}
        if text == "📌 今日计划":
            send_telegram_message(chat_id, today_plan_message(chat_id))
            buttons = today_plan_buttons(chat_id)
            if buttons:
                send_telegram_message(chat_id, "点击下方按钮修改完成状态：", buttons=buttons)
            return {"ok": True}
        if text == "🗓️ 工作日完整计划":
            send_telegram_message(chat_id, format_weekday_plan())
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
