import asyncio
import json
import os
import html
import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update, BotCommand
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

DATA_DIR = Path("data")
STATE_FILE = DATA_DIR / "state.json"


@dataclass
class PlanItem:
    text: str
    done: bool
    created_ts: str


class JsonDataStore:
    def __init__(self, file_path: Path) -> None:
        self.file_path = file_path
        self._lock = asyncio.Lock()
        self._state: Dict[str, Any] = {"users": {}}
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if self.file_path.exists():
            try:
                self._state = json.loads(self.file_path.read_text(encoding="utf-8"))
                if not isinstance(self._state, dict) or "users" not in self._state:
                    self._state = {"users": {}}
            except Exception:
                self._state = {"users": {}}
        else:
            self._flush_sync()

    async def save(self) -> None:
        async with self._lock:
            self._flush_sync()

    def _flush_sync(self) -> None:
        tmp_path = self.file_path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(self.file_path)

    def ensure_user(self, user_id: int, chat_id: int) -> Dict[str, Any]:
        users = self._state.setdefault("users", {})
        user = users.get(str(user_id))
        if not user:
            user = {
                "chat_id": chat_id,
                "hourly_enabled": True,
                "interval_minutes": 60,
                "awaiting_report": False,
                "pending_report_ts": None,
                "entries": [],  # list of {ts, text, hero?}
                "plans": [],  # list of PlanItem dicts
            }
            users[str(user_id)] = user
            self._flush_sync()
        return user

    def get_user(self, user_id: int) -> Optional[Dict[str, Any]]:
        return self._state.get("users", {}).get(str(user_id))

    def all_users(self) -> List[Tuple[int, Dict[str, Any]]]:
        result: List[Tuple[int, Dict[str, Any]]] = []
        for uid_str, u in self._state.get("users", {}).items():
            try:
                result.append((int(uid_str), u))
            except ValueError:
                continue
        return result

    def set_hourly(self, user_id: int, enabled: bool) -> None:
        user = self.get_user(user_id)
        if user:
            user["hourly_enabled"] = enabled
            self._flush_sync()

    def get_interval_minutes(self, user_id: int) -> int:
        user = self.get_user(user_id)
        if not user:
            return 60
        try:
            return int(user.get("interval_minutes", 60))
        except Exception:
            return 60

    def set_interval_minutes(self, user_id: int, minutes: int) -> None:
        user = self.get_user(user_id)
        if user:
            user["interval_minutes"] = int(minutes)
            self._flush_sync()

    def mark_awaiting_report(self, user_id: int, awaiting: bool) -> None:
        user = self.get_user(user_id)
        if user:
            user["awaiting_report"] = awaiting
            user["pending_report_ts"] = datetime.utcnow().isoformat()
            self._flush_sync()

    def add_report_entry(self, user_id: int, text: str) -> None:
        user = self.get_user(user_id)
        if not user:
            return
        entry = {"ts": datetime.utcnow().isoformat(), "text": text, "hero": None}
        user["entries"].append(entry)
        self._flush_sync()

    def attach_hero_to_last_pending(self, user_id: int, hero: Optional[str]) -> Optional[Dict[str, Any]]:
        user = self.get_user(user_id)
        if not user:
            return None
        for entry in reversed(user.get("entries", [])):
            if entry.get("hero") in (None, ""):
                entry["hero"] = hero
                self._flush_sync()
                return entry
        return None

    def add_plan(self, user_id: int, text: str) -> PlanItem:
        user = self.get_user(user_id)
        if not user:
            raise ValueError("User not initialized")
        item = PlanItem(text=text, done=False, created_ts=datetime.utcnow().isoformat())
        user["plans"].append(item.__dict__)
        self._flush_sync()
        return item

    def list_active_plans(self, user_id: int) -> List[PlanItem]:
        user = self.get_user(user_id)
        if not user:
            return []
        items: List[PlanItem] = []
        for p in user.get("plans", []):
            if not p.get("done"):
                items.append(PlanItem(text=p.get("text", ""), done=False, created_ts=p.get("created_ts", "")))
        return items

    def complete_plan_by_index(self, user_id: int, index_1_based: int) -> Optional[PlanItem]:
        user = self.get_user(user_id)
        if not user:
            return None
        active_indices = [i for i, p in enumerate(user.get("plans", [])) if not p.get("done")]
        if index_1_based < 1 or index_1_based > len(active_indices):
            return None
        idx = active_indices[index_1_based - 1]
        user["plans"][idx]["done"] = True
        self._flush_sync()
        p = user["plans"][idx]
        return PlanItem(text=p.get("text", ""), done=True, created_ts=p.get("created_ts", ""))


class RecommendationEngine:
    HERO_SUGGESTIONS: Dict[str, List[str]] = {
        "Исследователь": [
            "выбери новую тему и сделай 15-минутный обзор",
            "проведи маленький эксперимент и запиши вывод",
            "прочитай 3-5 страниц по интересующей области",
        ],
        "Художник": [
            "сделай быстрый набросок/эскиз идеи",
            "потрать 20 минут на свободное творчество",
            "переработай один небольшой фрагмент предыдущей работы",
        ],
        "Стратег": [
            "разведай новую возможность или риск — запиши один инсайт",
            "разбей крупную задачу на 3 конкретных шага",
            "отточи инструмент: улучши функцию, навык или ресурс на 1 шаг",
            "укрепи союз: попроси помощь или дай поддержку одному союзнику",
        ],
        "Заботливый": [
            "сделай короткую паузу на восстановление (10 минут)",
            "напиши себе поддерживающее сообщение/рефлексию",
            "устной благодарностью поддержи коллегу или близкого",
        ],
        "Предприниматель": [
            "сделай маленькое действие, которое двигает к результату",
            "свяжись с одним человеком для обратной связи",
            "подготовь черновик предложения/поста",
        ],
        "Спортсмен": [
            "выполни короткую разминку/прогулку 10-15 минут",
            "сделай одну силовую/дыхательную серию",
            "проверь осанку/рабочее место и поправь посадку",
        ],
    }

    @classmethod
    def build_recommendation(cls, plans: List[PlanItem], hero: Optional[str]) -> str:
        plan_part: str
        if plans:
            first_plan = plans[0]
            plan_part = f"Чтобы не навредить планам: выдели 25 минут на: • {first_plan.text}"
        else:
            plan_part = "Чтобы поддержать планы: добавь один пункт через команду /addplan <текст>"

        desire_part: str
        if hero and hero in cls.HERO_SUGGESTIONS:
            suggestion = cls.HERO_SUGGESTIONS[hero][random.randrange(len(cls.HERO_SUGGESTIONS[hero]))]
            desire_part = f"Следуй желанию ({hero}): {suggestion}."
        else:
            desire_part = "Следуй желанию: выбери маленький шаг, который радует тебя сейчас."

        return f"{plan_part}\n{desire_part}"


data_store = JsonDataStore(STATE_FILE)

async def schedule_reminders_for_user(app: Application, user_id: int, chat_id: int) -> None:
    job_name = f"reminder_{chat_id}"
    for j in app.job_queue.get_jobs_by_name(job_name):
        j.schedule_removal()
    interval_minutes = max(1, data_store.get_interval_minutes(user_id))
    interval_seconds = interval_minutes * 60
    app.job_queue.run_repeating(
        hourly_prompt_job,
        interval=interval_seconds,
        first=interval_seconds,
        name=job_name,
        data={"chat_id": chat_id, "user_id": user_id},
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user is None or update.effective_chat is None:
        return
    user = data_store.ensure_user(update.effective_user.id, update.effective_chat.id)
    data_store.set_hourly(update.effective_user.id, True)
    await schedule_reminders_for_user(context.application, update.effective_user.id, user["chat_id"])
    await update.message.reply_text(
        "Привет! Я буду периодически спрашивать, что вы сделали, и предлагать выбрать героя (по умолчанию каждые 60 минут).\n"
        "Команды: /report, /setinterval, /getinterval, /pause, /resume, /addplan, /listplans, /doneplan, /heroes."
    )


async def pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user is None or update.effective_chat is None:
        return
    data_store.set_hourly(update.effective_user.id, False)
    job_name = f"reminder_{update.effective_chat.id}"
    for j in context.application.job_queue.get_jobs_by_name(job_name):
        j.schedule_removal()
    await update.message.reply_text("Напоминания приостановлены. Используйте /resume, чтобы возобновить.")


async def resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user is None or update.effective_chat is None:
        return
    data_store.set_hourly(update.effective_user.id, True)
    await schedule_reminders_for_user(context.application, update.effective_user.id, update.effective_chat.id)
    minutes = data_store.get_interval_minutes(update.effective_user.id)
    await update.message.reply_text(f"Напоминания возобновлены. Следующее придёт через ~{minutes} минут.")


async def heroes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    names = ", ".join(RecommendationEngine.HERO_SUGGESTIONS.keys())
    await update.message.reply_text(f"Доступные архетипы: {names}")


async def getinterval(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user is None:
        return
    minutes = data_store.get_interval_minutes(update.effective_user.id)
    await update.message.reply_text(f"Текущий интервал напоминаний: {minutes} минут.")


async def setinterval(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user is None or update.effective_chat is None:
        return
    if not context.args:
        cur = data_store.get_interval_minutes(update.effective_user.id)
        await update.message.reply_text("Использование: /setinterval <минуты> (от 5 до 1440).\n" f"Сейчас: {cur} минут.")
        return
    raw = context.args[0].strip()
    if not raw.isdigit():
        await update.message.reply_text("Пожалуйста, укажите целое число минут, например: /setinterval 45")
        return
    minutes = int(raw)
    if minutes < 5 or minutes > 1440:
        await update.message.reply_text("Интервал должен быть от 5 до 1440 минут.")
        return
    data_store.set_interval_minutes(update.effective_user.id, minutes)
    # Перепланируем задачу
    await schedule_reminders_for_user(context.application, update.effective_user.id, update.effective_chat.id)
    await update.message.reply_text(f"Интервал обновлен: {minutes} минут. Следующее напоминание через ~{minutes} минут.")


async def report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user is None or update.message is None or update.effective_chat is None:
        return
    user = data_store.ensure_user(update.effective_user.id, update.effective_chat.id)
    text = " ".join(context.args).strip()
    if not text:
        await update.message.reply_text(
            "Использование: /report <текст> — отправить ответ на вопрос независимо от напоминаний."
        )
        return
    data_store.add_report_entry(update.effective_user.id, text)
    # Не переводим в режим ожидания — сразу просим выбрать героя
    await update.message.reply_text(
        "С каким героем вы сейчас себя ассоциируете?",
        reply_markup=_build_hero_keyboard(),
    )


async def addplan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user is None:
        return
    text = " ".join(context.args).strip()
    if not text:
        await update.message.reply_text("Использование: /addplan текст задачи")
        return
    data_store.ensure_user(update.effective_user.id, update.effective_chat.id)
    item = data_store.add_plan(update.effective_user.id, text)
    await update.message.reply_text(f"Добавлен план: {item.text}")


async def listplans(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user is None:
        return
    plans = data_store.list_active_plans(update.effective_user.id)
    if not plans:
        await update.message.reply_text("Активных планов нет. Добавьте через /addplan <текст>.")
        return
    lines = [f"{i+1}. {p.text}" for i, p in enumerate(plans)]
    await update.message.reply_text("Активные планы:\n" + "\n".join(lines))


async def doneplan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user is None:
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Использование: /doneplan номер")
        return
    idx = int(context.args[0])
    item = data_store.complete_plan_by_index(update.effective_user.id, idx)
    if not item:
        await update.message.reply_text("Не удалось найти пункт с таким номером.")
        return
    await update.message.reply_text(f"Готово: {item.text}")


async def hourly_prompt_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    job_data = context.job.data or {}
    chat_id = job_data.get("chat_id")
    job_user_id = job_data.get("user_id")
    if chat_id is None:
        return
    # Find user by chat_id
    user_id: Optional[int] = None
    if isinstance(job_user_id, int):
        user = data_store.get_user(job_user_id)
        if user and user.get("chat_id") == chat_id and user.get("hourly_enabled", True):
            user_id = job_user_id
    if user_id is None:
        for uid, user in data_store.all_users():
            if user.get("chat_id") == chat_id and user.get("hourly_enabled", True):
                user_id = uid
                break
    if user_id is None:
        return

    try:
        interval_minutes = data_store.get_interval_minutes(user_id)
        await context.bot.send_message(chat_id=chat_id, text=f"Что вы сделали за последние {interval_minutes} минут? Используйте команду /report для ответа.")
        data_store.mark_awaiting_report(user_id, True)
    except Exception:
        return


def _build_hero_keyboard() -> InlineKeyboardMarkup:
    names = list(RecommendationEngine.HERO_SUGGESTIONS.keys())
    rows: List[List[InlineKeyboardButton]] = []
    row: List[InlineKeyboardButton] = []
    for name in names:
        row.append(InlineKeyboardButton(name, callback_data=f"hero:{name}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("Пропустить", callback_data="hero:")])
    return InlineKeyboardMarkup(rows)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user is None or update.message is None or update.effective_chat is None:
        return
    user = data_store.get_user(update.effective_user.id)
    if not user:
        return
    if not user.get("awaiting_report"):
        return

    text = update.message.text.strip()
    if not text:
        return

    data_store.add_report_entry(update.effective_user.id, text)
    data_store.mark_awaiting_report(update.effective_user.id, False)

    await update.message.reply_text(
        "С каким героем вы сейчас себя ассоциируете?",
        reply_markup=_build_hero_keyboard(),
    )


async def handle_hero_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.callback_query is None or update.effective_user is None:
        return
    query = update.callback_query
    await query.answer()

    data = query.data or ""
    hero = data.split(":", 1)[1] if ":" in data else ""
    hero = hero if hero else None

    entry = data_store.attach_hero_to_last_pending(update.effective_user.id, hero)

    plans = data_store.list_active_plans(update.effective_user.id)
    recommendation = RecommendationEngine.build_recommendation(plans, hero)

    parts: List[str] = []
    if entry:
        parts.append(f"Вы ответили: {html.escape(entry.get('text', ''))}")
    if hero:
        parts.append(f"Герой: {html.escape(hero)}")
    parts.append("")
    parts.append(recommendation)

    await query.edit_message_text("\n".join(parts), reply_markup=None)


async def on_startup(app: Application) -> None:
    commands = [
        BotCommand("start", "Начать"),
        BotCommand("pause", "Отключить напоминания"),
        BotCommand("resume", "Возобновить напоминания"),
        BotCommand("addplan", "Добавить пункт плана"),
        BotCommand("listplans", "Список активных планов"),
        BotCommand("doneplan", "Отметить пункт как выполненный"),
        BotCommand("heroes", "Список доступных архетипов"),
        BotCommand("getinterval", "Текущий интервал между напоминаниями"),
        BotCommand("setinterval", "Изменить интервал между напоминаниями"),
        BotCommand("report", "Отправить отчет вручную")
    ]
    await app.bot.set_my_commands(commands)

    for user_id, user in data_store.all_users():
        if user.get("hourly_enabled", True):
            await schedule_reminders_for_user(app, user_id, user.get("chat_id"))

async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print("Получено сообщение:", update.message.text)



def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не найден. Создайте .env с BOT_TOKEN=<токен> или задайте переменную окружения.")

    application: Application = ApplicationBuilder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("pause", pause))
    application.add_handler(CommandHandler("resume", resume))
    application.add_handler(CommandHandler("heroes", heroes))
    application.add_handler(CommandHandler("getinterval", getinterval))
    application.add_handler(CommandHandler("setinterval", setinterval))
    application.add_handler(CommandHandler("report", report))
    application.add_handler(CommandHandler("addplan", addplan))
    application.add_handler(CommandHandler("listplans", listplans))
    application.add_handler(CommandHandler("doneplan", doneplan))

    application.add_handler(CallbackQueryHandler(handle_hero_choice, pattern=r"^hero:"))
    application.add_handler(MessageHandler(filters.ALL, echo))

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    application.post_init = on_startup

    application.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
