"""Telegram-бот: согласие и сбор кейса, подтверждение автором, решение редактора, расписание.

Запуск: python -m app.bot
В проде этот сценарий встраивается в @osresume_bot после бесплатной проверки резюме.
"""

import asyncio
import json
import logging

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, FSInputFile, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from .config import settings
from .db import DB, iso_week
from .pipeline import Pipeline

log = logging.getLogger("molodets")
router = Router()
db = DB(settings.db_path)
pipeline = Pipeline(db)

CONSENT_TEXT = (
    "Можно анонимно разобрать твоё резюме в рубрике «Молодец, надо переписать»?\n\n"
    "Как это работает:\n"
    "• мы уберём имя, компании, контакты и всё, по чему тебя можно узнать;\n"
    "• перед публикацией пришлём текст тебе — без твоего «да» он не выйдет;\n"
    "• исходный фрагмент удалим через {days} дней, данные хранятся в России;\n"
    "• передумать можно в любой момент: команда /otozvat."
)


class Collect(StatesGroup):
    fragment = State()
    story = State()


class EditorEdit(StatesGroup):
    text = State()


def is_editor(user_id: int) -> bool:
    return user_id in settings.editor_ids


def kb(*buttons: tuple[str, str]):
    b = InlineKeyboardBuilder()
    for text, data in buttons:
        b.button(text=text, callback_data=data)
    b.adjust(2)
    return b.as_markup()


# ---------- пользователь ----------

@router.message(CommandStart())
async def start(message: Message, command: CommandObject, state: FSMContext):
    await state.clear()
    tag = command.args or ""
    await state.update_data(source_tag=tag)
    db.log(None, "start", tag)
    await message.answer(
        "Привет! Это Career OS: показываем резюме глазами рекрутера.\n\n"
        "Хочешь поучаствовать в рубрике «Молодец, надо переписать»? Набери /razbor.")


@router.message(Command("razbor"))
async def razbor(message: Message):
    await message.answer(CONSENT_TEXT.format(days=settings.retention_days),
                         reply_markup=kb(("Да, можно", "consent:yes"), ("Нет, спасибо", "consent:no")))


@router.callback_query(F.data.startswith("consent:"))
async def consent(call: CallbackQuery, state: FSMContext):
    if call.data == "consent:no":
        await call.message.edit_text("Хорошо, ничего не сохраняем. Удачи с откликами!")
        return
    await state.update_data(consent=True)
    await state.set_state(Collect.fragment)
    await call.message.edit_text("Спасибо! Пришли фрагмент резюме: одно место работы, как оно написано сейчас.")


@router.message(Collect.fragment, F.text)
async def got_fragment(message: Message, state: FSMContext):
    await state.update_data(fragment=message.text)
    await state.set_state(Collect.story)
    await message.answer("А теперь своими словами: что ты на самом деле сделал на этом месте? "
                         "Результаты, цифры, задачи, которые закрыл сверх обязанностей.")


@router.message(Collect.story, F.text)
async def got_story(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    await state.clear()
    case_id = db.add_case("molodets", data["fragment"], message.text, user_id=message.from_user.id,
                          source_tag=data.get("source_tag", ""), consent=data.get("consent", False))
    await message.answer("Принято! Готовим черновик, это займёт пару минут.")
    try:
        result = await asyncio.to_thread(pipeline.run, case_id)
    except Exception as e:  # модель недоступна и резерв тоже — уходит редактору на ручную сборку
        log.exception("pipeline failed")
        db.log(case_id, "error", str(e))
        await notify_editor(bot, f"⚠️ Кейс #{case_id}: конвейер упал ({e}). Собрать вручную по шаблону.")
        await message.answer("Черновик соберём вручную и пришлём тебе на проверку.")
        return

    if result.status == "waiting_author":
        await message.answer(
            "Вот как будет выглядеть разбор. Здесь всё верно и тебя не узнать?\n\n" + result.texts["tg"],
            reply_markup=kb(("Всё верно", f"author:yes:{result.draft_id}"),
                            ("Не публиковать", f"author:no:{result.draft_id}")))
    elif result.status == "checks_failed":
        await message.answer("Черновик не прошёл нашу проверку, редактор доработает его и пришлёт тебе.")
        await send_to_editor(bot, result.draft_id, failed=True)
    else:
        await message.answer("Спасибо! Этот кейс не подойдёт для рубрики, но ты можешь проверить резюме в боте.")


@router.callback_query(F.data.startswith("author:"))
async def author_decision(call: CallbackQuery, bot: Bot):
    _, answer, draft_id = call.data.split(":")
    status = pipeline.author_decision(int(draft_id), answer == "yes")
    if status == "waiting_editor":
        await call.message.edit_reply_markup()
        await call.message.answer("Спасибо! Передали редактору.")
        await send_to_editor(bot, int(draft_id))
    else:
        await call.message.edit_reply_markup()
        await call.message.answer("Поняли, не публикуем.")


@router.message(Command("otozvat"))
async def revoke(message: Message, bot: Bot):
    ids = db.revoke_user(message.from_user.id)
    await message.answer("Готово: удалили твои данные и сняли разборы с публикации." if ids else
                         "У нас нет твоих данных.")
    if ids:
        await notify_editor(bot, f"Автор отозвал согласие: кейсы {ids}. Если выпуск уже вышел — удалить пост.")


# ---------- редактор ----------

async def notify_editor(bot: Bot, text: str):
    if settings.editor_chat_id:
        await bot.send_message(settings.editor_chat_id, text)


async def send_to_editor(bot: Bot, draft_id: int, failed: bool = False):
    if not settings.editor_chat_id:
        log.warning("EDITOR_CHAT_ID не задан, черновик #%s ждёт в базе", draft_id)
        return
    d = db.get_draft(draft_id)
    texts = json.loads(d["texts"])
    problems = json.loads(d["problems"] or "[]")
    head = f"{'❌ Не прошло проверку' if failed else '✅ Автор подтвердил'} · черновик #{draft_id}"
    if problems:
        head += "\n\nЗамечания:\n" + "\n".join(f"• {p}" for p in problems)
    if d["card_path"]:
        await bot.send_document(settings.editor_chat_id, FSInputFile(d["card_path"]), caption=head[:1000])
    else:
        await bot.send_message(settings.editor_chat_id, head)
    for platform, text in texts.items():
        await bot.send_message(settings.editor_chat_id, f"— {platform.upper()} —\n{text}")
    buttons = [("Править TG", f"ed:edit:{draft_id}:tg"), ("Отклонить", f"ed:no:{draft_id}")]
    if not failed:
        buttons.insert(0, ("Утвердить", f"ed:ok:{draft_id}"))
    await bot.send_message(settings.editor_chat_id, "Решение?", reply_markup=kb(*buttons))


@router.callback_query(F.data.startswith("ed:"))
async def editor_decision(call: CallbackQuery, state: FSMContext):
    if not is_editor(call.from_user.id):
        await call.answer("Только для редакторов", show_alert=True)
        return
    parts = call.data.split(":")
    action, draft_id = parts[1], int(parts[2])
    if action == "ok":
        try:
            week = pipeline.approve(draft_id, call.from_user.id)
            await call.message.edit_text(f"Утверждено, выпуск в слоте недели {week}. Собираю ролики…")
            await make_and_route_reels(call.bot, draft_id)
        except ValueError as e:
            await call.answer(str(e), show_alert=True)
    elif action == "no":
        pipeline.reject(draft_id, call.from_user.id)
        await call.message.edit_text("Отклонено.")
    elif action == "edit":
        await state.set_state(EditorEdit.text)
        await state.update_data(draft_id=draft_id, platform=parts[3])
        await call.message.answer("Пришли новый текст целиком, со ссылкой на бота. Правила проверят его ещё раз.")


@router.message(EditorEdit.text, F.text)
async def editor_text(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    await state.clear()
    problems = pipeline.apply_edit(data["draft_id"], data["platform"], message.text)
    if problems:
        await message.answer("Правка не прошла правила:\n" + "\n".join(f"• {p}" for p in problems))
    await send_to_editor(bot, data["draft_id"], failed=bool(problems))


# ---------- ролики: 2–3 на героя, герой и редактор видят их до публикации ----------

async def make_and_route_reels(bot: Bot, draft_id: int):
    try:
        reel_ids = await asyncio.to_thread(pipeline.make_reels, draft_id)
    except Exception as e:
        log.exception("reels failed")
        await notify_editor(bot, f"⚠️ Ролики для черновика #{draft_id} не собрались ({e}).")
        return
    case = db.get_case(db.get_draft(draft_id)["case_id"])
    for rid in reel_ids:
        r = db.get_reel(rid)
        if r["status"] == "waiting_author" and case["user_id"]:
            await send_reel(bot, case["user_id"], rid, "Из твоей истории сделали ролик. Всё верно и тебя не узнать?",
                            (("Всё верно", f"rla:yes:{rid}"), ("Не публиковать", f"rla:no:{rid}")))
        else:
            await send_reel_to_editor(bot, rid)


async def send_reel(bot: Bot, chat_id: int, reel_id: int, caption: str, buttons):
    r = db.get_reel(reel_id)
    path = r["video_path"]
    if path and path.endswith(".mp4"):
        await bot.send_video(chat_id, FSInputFile(path), caption=caption, reply_markup=kb(*buttons))
    else:
        scenes = json.loads(r["storyboard"]).get("scenes", [])
        text = caption + "\n\n" + "\n".join(f"• {s['caption']}" for s in scenes)
        await bot.send_message(chat_id, text[:4000], reply_markup=kb(*buttons))


async def send_reel_to_editor(bot: Bot, reel_id: int):
    if not settings.editor_chat_id:
        return
    r = db.get_reel(reel_id)
    problems = json.loads(r["problems"] or "[]")
    head = f"🎬 Ролик #{reel_id} · угол {r['angle']}"
    if problems:
        head += "\n\nНе прошёл проверку:\n" + "\n".join(f"• {p}" for p in problems)
        buttons = (("Отклонить", f"rl:no:{reel_id}"),)
    else:
        buttons = (("Утвердить", f"rl:ok:{reel_id}"), ("Отклонить", f"rl:no:{reel_id}"))
    await send_reel(bot, settings.editor_chat_id, reel_id, head[:1000], buttons)


@router.callback_query(F.data.startswith("rla:"))
async def reel_author(call: CallbackQuery, bot: Bot):
    _, answer, rid = call.data.split(":")
    status = pipeline.reel_author_decision(int(rid), answer == "yes")
    await call.message.edit_reply_markup()
    await call.message.answer("Спасибо! Передали редактору." if status == "waiting_editor" else "Поняли, не публикуем.")
    if status == "waiting_editor":
        await send_reel_to_editor(bot, int(rid))


@router.callback_query(F.data.startswith("rl:"))
async def reel_editor(call: CallbackQuery):
    if not is_editor(call.from_user.id):
        await call.answer("Только для редакторов", show_alert=True)
        return
    _, action, rid = call.data.split(":")
    if action == "ok":
        try:
            week = pipeline.approve_reel(int(rid))
            await call.message.edit_reply_markup()
            await call.message.answer(f"Ролик #{rid} утверждён, неделя {week}.")
        except ValueError as e:
            await call.answer(str(e), show_alert=True)
    else:
        db.update_reel(int(rid), status="rejected")
        await call.message.edit_reply_markup()
        await call.message.answer(f"Ролик #{rid} отклонён.")


@router.message(Command("queue"))
async def queue(message: Message):
    if not is_editor(message.from_user.id):
        return
    rows = db.drafts_by_status("approved")
    week = iso_week()
    lines = [f"#{r['id']} · неделя {r['week']}" for r in rows] or ["очередь пуста"]
    await message.answer(f"Сейчас неделя {week}.\n" + "\n".join(lines))


# ---------- расписание ----------

async def weekly_reminder(bot: Bot):
    """Понедельник: напоминаем редактору, какой выпуск идёт в слот «Почему не зовут»."""
    week = iso_week()
    this_week = [r for r in db.drafts_by_status("approved") if r["week"] == week]
    if this_week:
        await notify_editor(bot, f"Неделя {week}: в слот «Почему не зовут» идёт выпуск #{this_week[0]['id']}.")
    else:
        await notify_editor(bot, f"Неделя {week}: подходящего кейса нет — в слот ставим обычный разбор.")


def purge_job():
    n = db.purge_old(settings.retention_days)
    log.info("purged %s cases", n)


async def main():
    logging.basicConfig(level=logging.INFO)
    if not settings.bot_token:
        raise SystemExit("Укажи BOT_TOKEN в .env (токен от @BotFather)")
    bot = Bot(settings.bot_token)
    dp = Dispatcher()
    dp.include_router(router)
    scheduler = AsyncIOScheduler(timezone=settings.timezone)
    scheduler.add_job(weekly_reminder, "cron", day_of_week="mon", hour=10, args=[bot])
    scheduler.add_job(purge_job, "cron", hour=3)
    scheduler.start()
    log.info("LLM: %s", "заглушка (нет ключа)" if settings.use_mock_llm else settings.llm_base_url)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
