import asyncio
import os
import logging
import hashlib
import hmac
import aiosqlite
import csv, io
from datetime import datetime
from typing import Optional
from decimal import Decimal

import defusedxml.ElementTree as ET
import requests as req_lib
from flask import Flask

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    filters,
    ContextTypes,
)

# ==================== НАСТРОЙКИ ====================
DB_NAME = '/app/data/bot_data.db'
TOKEN = os.getenv("BOT_TOKEN")
EASYPAY_MERCHANT_ID = os.getenv("EASYPAY_MERCHANT_ID", "ВАШ_MERCHANT_ID")
EASYPAY_SECRET_KEY = os.getenv("EASYPAY_SECRET_KEY", "ВАШ_SECRET_KEY")
EASYPAY_SERVICE_ID = os.getenv("EASYPAY_SERVICE_ID", "ВАШ_SERVICE_ID")
PRIVATE_CHANNEL_INVITE_LINK = "https://t.me/+DKi4P0URBy40ZTky"
PRIVATE_CHANNEL_ID = -1003921507515
EXPERT_USERNAME = "Elena_lagodzich"
EXPERT_CHANNEL_LINK = "https://t.me/lagodzich"
WELCOME_PHOTO_ID = "AgACAgIAAxkBAAICT2nvi1KApvmCnUqGtMHf5xo_RdKmAAIYGWsb0QqBS9ZRDEWka5-DAQADAgADeAADOwQ"   

# ID видео в Telegram (получить через @getidsbot)
VIDEO_1_FILE_ID = "BQACAgIAAxkBAAPtaeP82oFM3nVLgOJk6PSHpT3BPMcAAhKjAAIWfSBLaj7yaTknOuA7BA"  # видео для вопроса 1 (нет/не всегда)
VIDEO_2_FILE_ID = "BQACAgIAAxkBAAPvaeP9NqxoD1_shLr1Af2yX1scG-wAAhOjAAIWfSBLSROB1giNwzc7BA"  # видео для вопроса 4 (да)
VIDEO_3_FILE_ID = "BQACAgIAAxkBAAPxaeP9k2a1UDTL0bnZj4Sq8Hha4F0AAhWjAAIWfSBLNVR39jpWdJY7BA"  # видео для вопроса 5 (да)
DEFAULT_VIDEO_FILE_ID = "BQACAgIAAxkBAAPvaeP9NqxoD1_shLr1Af2yX1scG-wAAhOjAAIWfSBLSROB1giNwzc7BA"  # общее видео, если условия не сработали

# ==================== ДЛЯ УПРАВЛЕНИЯ ====================
ADMIN_USER_IDS = [675468047, 753375245]

# ==================== СОСТОЯНИЯ ====================
(
    ASK_NAME,
    ASK_QUESTION_1,
    ASK_QUESTION_2,
    ASK_QUESTION_3,
    ASK_QUESTION_4,
    ASK_QUESTION_5,
    WATCH_VIDEO,
    ASK_VIDEO_FEEDBACK,
    SELF_REFLECTION_1,
    SELF_REFLECTION_2,
    SELF_REFLECTION_3,
) = range(11)

async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS answers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT,
                name TEXT,
                question_id INTEGER NOT NULL,
                answer TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        # Таблица для платежей и статуса рефлексии
        await db.execute('''
            CREATE TABLE IF NOT EXISTS payments (
                order_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                amount REAL,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        await db.commit()
        logger.info("База данных инициализирована успешно.")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
telegram_app: Optional[Application] = None

@app.route("/health", methods=["GET"])
def health():
    return "OK", 200

async def grant_access_after_payment(user_id: int, bot):
    """Создаёт одноразовую ссылку и отправляет пользователю."""
    try:
        # Создаём уникальную ссылку с лимитом 1 использование
        invite_link = await bot.create_chat_invite_link(
            chat_id=PRIVATE_CHANNEL_ID,
            member_limit=1,
            name=f"Order_{user_id}"
        )
        safe_link = invite_link.invite_link.replace('_', r'\_')
        await bot.send_message(
            chat_id=user_id,
            text=(
                f"✅ *Оплата прошла успешно!*\n\n"
                f"Ваш доступ к мастер-классу открыт.\n"
                f"Переходите по ссылке:\n"
                f"{safe_link}\n\n"
                f"Ссылка действительна только для вас. Пожалуйста, не передавайте её."
            ),
            parse_mode="Markdown",
        )
        logger.info(f"Одноразовая ссылка отправлена пользователю {user_id}")
    except Exception as e:
        logger.error(f"Ошибка отправки доступа пользователю {user_id}: {e}")
        # Запасной вариант – отправить общую ссылку, если создание не удалось
        try:
            safe_general_link = PRIVATE_CHANNEL_INVITE_LINK.replace('_', r'\_')
            await bot.send_message(
                chat_id=user_id,
                text=(
                f"✅ *Оплата прошла успешно!*\n\n"
                f"Ваш доступ к мастер-классу открыт.\n"
                f"Переходите по ссылке:\n"
                f"{safe_general_link}\n\n"
                f"Если ссылка не работает, обратитесь к @Elena_lagodzich."
                ),
                parse_mode="Markdown",
            )
        except Exception as fallback_e:
            logger.error(f"Не удалось отправить даже общую ссылку: {fallback_e}")
            pass

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
        'UPDATE payments SET status = ? WHERE order_id = ?',
        ('paid')
        )
        await db.commit()

    if telegram_app and telegram_app.job_queue:
        telegram_app.job_queue.run_once(
            check_watched_mk,
            when=6 * 3600,  # 6 часов
            chat_id=user_id,
            user_id=user_id,
            name=f"watch_check_{user_id}"
        )
        logger.info(f"Задача опроса запланирована для пользователя {user_id}")
    else:
        logger.error("JobQueue недоступен, опрос не запланирован")

async def check_watched_mk(context: ContextTypes.DEFAULT_TYPE):
    """Отправляет пользователю вопрос: посмотрел ли он МК?"""
    job = context.job
    user_id = job.user_id
    chat_id = job.chat_id
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text="Вы уже посмотрели мастер-класс?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Да", callback_data="watched_yes"),
                 InlineKeyboardButton("❌ Нет", callback_data="watched_no")]
            ])
        )
        logger.info(f"Вопрос о просмотре отправлен пользователю {user_id}")
    except Exception as e:
        logger.error(f"Ошибка отправки вопроса о просмотре: {e}")

async def handle_watched_response(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает ответ на вопрос 'посмотрели ли МК'."""
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = update.effective_user.id

    if data == "watched_yes":
        # Сохраняем, что пользователь приступил к рефлексии
        context.user_data['reflection_stage'] = 1
        context.user_data['reflection_answers'] = []

        await query.edit_message_text(
            "Отлично, вы большой молодец, что сделали этот шаг! "
            "Теперь ответьте на 3 вопроса после МК, чтобы оценить полученный результат.\n\n"
            "❓ *1. Я понимаю, что здоровые отношения требуют работы и осознанности.*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Да", callback_data="reflection_1_yes"),
                 InlineKeyboardButton("❌ Нет", callback_data="reflection_1_no"),
                 InlineKeyboardButton("🤷 Не знаю", callback_data="reflection_1_idk")]
            ])
        )
        logger.info(f"Пользователь {user_id} начинает рефлексию")
    elif data == "watched_no":
        await query.edit_message_text("Хорошо, вернусь к вам позже.")
        # Планируем повторный запрос через 24 часа
        if telegram_app and telegram_app.job_queue:
            telegram_app.job_queue.run_once(
                check_watched_mk,
                when=24 * 3600,
                chat_id=update.effective_chat.id,
                user_id=user_id,
                name=f"watch_check_retry_{user_id}"
            )
            logger.info(f"Повторный опрос запланирован для {user_id}")

async def handle_reflection_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает ответы на 3 вопроса рефлексии."""
    query = update.callback_query
    await query.answer()
    data = query.data  # формат: reflection_{q_num}_{answer}
    _, q_str, ans = data.split('_')
    q_num = int(q_str)

    # Сохраняем ответ
    user_data = context.user_data
    if 'reflection_answers' not in user_data:
        user_data['reflection_answers'] = []
    user_data['reflection_answers'].append({
        'question': q_num,
        'answer': ans
    })

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            'INSERT INTO answers (user_id, username, name, question_id, answer) VALUES (?, ?, ?, ?, ?)',
            (
                update.effective_user.id,
                update.effective_user.username,
                context.user_data.get("name", "Неизвестный"),
                100 + q_num,               # 101, 102, 103 – вопросы рефлексии
                ans
            )
        )
        await db.commit()

    if q_num < 3:
        # Следующий вопрос
        next_q = q_num + 1
        questions = {
            2: "2. После обучения я чувствую себя более подготовленным(ой) к развитию здоровых отношений.",
            3: "3. Мастер-класс помог мне понять свои собственные потребности в отношениях."
        }
        question_text = f"❓ *{questions[next_q]}*"
        await query.edit_message_text(
            question_text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Да", callback_data=f"reflection_{next_q}_yes"),
                 InlineKeyboardButton("❌ Нет", callback_data=f"reflection_{next_q}_no"),
                 InlineKeyboardButton("🤷 Не знаю", callback_data=f"reflection_{next_q}_idk")]
            ])
        )
    else:
        # Последний вопрос отвечен
        await query.edit_message_text(
            f"Спасибо за вашу обратную связь, она важна для нас!\n"
            f"Если у вас остался вопрос, вы можете задать его напрямую [Елене Лагодич](https://t.me/{EXPERT_USERNAME})",
            parse_mode="Markdown"
        )
        # Дополнительное сообщение с кнопкой
        await asyncio.sleep(1.5)
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="*Не хотите пропустить новые мастер-классы – следите за обновлениями в телеграм-канале!*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔔 Перейти", url=EXPERT_CHANNEL_LINK)]
            ])
        )
        # Очищаем данные рефлексии
        user_data.pop('reflection_stage', None)
        user_data.pop('reflection_answers', None)
        logger.info(f"Рефлексия завершена для пользователя {update.effective_user.id}")


# ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Приветствие и запрос имени."""
    user = update.effective_user
    context.user_data.clear()
    context.user_data["user_id"] = user.id
    context.user_data["username"] = user.username
    context.user_data["started_at"] = datetime.now().isoformat()

    welcome_caption = (
        f"👋 *Здравствуйте!*\n\n"
        "Я — помощник Елены Лагодич, эксперта в области психологии с многолетним опытом работы. Я помогу вам лучше понять себя и свои отношения.\n\n"
        "🔒 *Конфиденциальность гарантирована.* Все ваши ответы останутся между нами. Они нужны только для того, чтобы сделать нашу работу максимально точной и полезной для вас.\n\n"
        "👉 *Давайте познакомимся.* Как я могу к вам обращаться? Напишите ваше имя."
    )
    await update.message.reply_photo(
        photo=WELCOME_PHOTO_ID, 
        caption=welcome_caption,
        parse_mode="Markdown"
    )
    return ASK_NAME

async def ask_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Сохраняет имя, приветствует пользователя и начинает анкету."""
    # Сохраняем имя в user_data
    context.user_data["name"] = update.message.text

    # Промежуточное сообщение — благодарность и введение в опрос
    thank_you_text = (
        f"Спасибо, *{context.user_data['name']}*!\n\n"
        "Давайте перейдём к короткому опросу из 5 вопросов, чтобы я мог лучше понять вашу ситуацию. "
        "Пожалуйста, выбирайте один из вариантов ответа на каждый вопрос.\n\n"
    )
    await update.message.reply_text(thank_you_text, parse_mode="Markdown")

    # Первый вопрос анкеты
    question = "❓ *1. Могу открыто говорить о своих чувствах и желаниях.*"
    keyboard = [
        [
            InlineKeyboardButton("✅ Да", callback_data="answer_yes"),
            InlineKeyboardButton("❌ Нет", callback_data="answer_no"),
            InlineKeyboardButton("🤷 Не всегда", callback_data="answer_not_always"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(question, reply_markup=reply_markup, parse_mode="Markdown")

    # Устанавливаем счётчик вопросов
    context.user_data["current_question"] = 0
    return ASK_QUESTION_1

async def handle_question_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обрабатывает ответы на 5 вопросов, сохраняет и переходит к следующему."""
    query = update.callback_query
    await query.answer()
    answer = query.data

    if "answers" not in context.user_data:
        context.user_data["answers"] = []
    q_num = context.user_data.get("current_question", 0) + 1
    context.user_data["answers"].append({"question": q_num, "answer": answer})

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
        'INSERT INTO answers (user_id, username, name, question_id, answer) VALUES (?, ?, ?, ?, ?)',
        (update.effective_user.id, update.effective_user.username, context.user_data.get("name"), q_num, answer)
        )
        await db.commit()

    questions = [
        "1. Могу открыто говорить о своих чувствах и желаниях.",
        "2. Мне кажется, что я полностью понимаю свои потребности в отношениях.",
        "3. Я знаю, что нужно делать, чтобы сделать свои отношения лучше.",
        "4. Я часто задаюсь вопросом, правильно ли я поступаю в своих отношениях.",
        "5. Иногда мне кажется, что я слишком много даю и мало получаю взамен.",
    ]

    if q_num < 5:
        next_q = q_num
        context.user_data["current_question"] = next_q
        question_text = f"❓ *{questions[next_q]}*"
        keyboard = [
            [
                InlineKeyboardButton("✅ Да", callback_data="answer_yes"),
                InlineKeyboardButton("❌ Нет", callback_data="answer_no"),
                InlineKeyboardButton("🤷 Не всегда", callback_data="answer_not_always"),
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(question_text, reply_markup=reply_markup, parse_mode="Markdown")
        return [ASK_QUESTION_1, ASK_QUESTION_2, ASK_QUESTION_3, ASK_QUESTION_4, ASK_QUESTION_5][next_q]
    else:
        # Анкета завершена
        await query.edit_message_text("Спасибо за ответы! Подбираю для вас видео...")
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
        await asyncio.sleep(1.5)
        return await send_video_based_on_answers(update, context)

async def send_video_based_on_answers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Определяет, какое видео отправить, и продолжает воронку."""
    answers = context.user_data.get("answers", [])
    ans1 = next((a["answer"] for a in answers if a["question"] == 1), None)
    ans4 = next((a["answer"] for a in answers if a["question"] == 4), None)
    ans5 = next((a["answer"] for a in answers if a["question"] == 5), None)

    video_id = DEFAULT_VIDEO_FILE_ID

    if ans1 in ("answer_no", "answer_not_always"):
        video_id = VIDEO_1_FILE_ID
        logger.info("Отправка видео 1 (вопрос 1 нет/не всегда)")
    elif ans4 == "answer_yes":
        video_id = VIDEO_2_FILE_ID
        logger.info("Отправка видео 2 (вопрос 4 да)")
    elif ans5 == "answer_yes":
        video_id = VIDEO_3_FILE_ID
        logger.info("Отправка видео 3 (вопрос 5 да)")

    # Отправляем файл (документ)
    try:
        await context.bot.send_document(
            chat_id=update.effective_chat.id,
            document=video_id,
            caption=(
             "Посмотрите этот небольшой отрывок из *мастер‑класса Елены Лагодич про психологию отношений и близости*.\n\n"
             "В нём вы сможете найти полезную информацию о своих отношениях с друзьями и близкими!"
            ),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Ошибка при отправке документа: {e}")
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="Извините, произошла ошибка при загрузке видео. Попробуйте позже или обратитесь к администратору."
        )
        return ConversationHandler.END

    # После любого видео задаём вопрос и продолжаем воронку
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    await asyncio.sleep(60.0)
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="Откликнулась ли вам эта информация?",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Да", callback_data="video_feedback_yes"),
             InlineKeyboardButton("❌ Нет", callback_data="video_feedback_no")]
        ])
    )
    return WATCH_VIDEO

async def handle_video_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обрабатывает ответ на вопрос о видео."""
    query = update.callback_query
    await query.answer()
    feedback = query.data
    context.user_data["video_feedback"] = feedback

    if feedback == "video_feedback_no":
        await query.edit_message_text(
            "Спасибо за проявленный интерес! 😊\n"
            "В скором времени появятся мастер-классы и на другие темы, будем рады видеть вас снова!"
        )
        return ConversationHandler.END
    else:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
        await asyncio.sleep(1.5)
        offer_text = (
            "Значит вы попали сюда не зря!\n\n"
            "Приглашаю вас посмотреть углубленный мастер-класс «Искусство здоровых отношений» от Елены Лагодич.\n\n"
            "На нем вы узнаете:\n"
            "✅ Как выстроить гармоничные отношения\n"
            "✅ Где брать ресурс и энергию\n"
            "✅ Техники, которые помогут вашим отношениям уже сегодня\n\n"
            "А также получите презентацию с полезными лайфхаками и упражнениями!\n\n"
            "Стоимость доступа — всего 50 бел. руб.\n\n"
            "👇 Нажмите кнопку ниже для оплаты."
        )
        keyboard = [[InlineKeyboardButton("💳 Перейти к оплате", callback_data="start_payment")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(offer_text, reply_markup=reply_markup, parse_mode="Markdown")
        return ASK_VIDEO_FEEDBACK


def _make_soap_envelope(body_xml: str) -> str:
    """Оборачивает тело запроса в SOAP-конверт."""
    return f"""<?xml version="1.0" encoding="windows-1251"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    {body_xml}
  </soap:Body>
</soap:Envelope>"""

async def start_payment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    name = context.user_data.get("name", "Участник")
    order_id = f"mk{user_id % 10000:04d}_{int(datetime.now().timestamp() * 1000)}"

    MER_NO = os.getenv("EASYPAY_MERCHANT_ID")
    PASS = os.getenv("EASYPAY_SECRET_KEY")

    # Формируем XML вручную
    body_xml = f"""<EP_CreateInvoice xmlns="http://easypay.by/">
      <mer_no>{MER_NO}</mer_no>
      <pass>{PASS}</pass>
      <order>{order_id}</order>
      <sum>50.00</sum>
      <exp>3</exp>
      <card>PT_EPOS</card>
      <comment>{f"Мастер-класс по отношениям для {name}"[:50]}</comment>
      <info>Доступ к закрытому каналу с видео мастер-класса</info>
    </EP_CreateInvoice>"""
    soap_xml = _make_soap_envelope(body_xml)

    headers = {
        "Content-Type": "text/xml; charset=windows-1251",
        "SOAPAction": "http://easypay.by/EP_CreateInvoice",
    }

    try:
        resp = req_lib.post(
            "https://ssl.easypay.by/xml/server.php",
            data=soap_xml.encode("windows-1251"),
            headers=headers,
            timeout=15
        )
        if resp.status_code != 200:
            logger.error(f"HTTP ошибка: {resp.status_code}\n{resp.text}")
            await query.edit_message_text("Сервис оплаты временно недоступен.")
            return ConversationHandler.END

        # Парсим ответ
        root = ET.fromstring(resp.content)
        ns = {"easypay": "http://easypay.by/"}
        status = root.find(".//easypay:status", ns)
        if status is None:
            raise ValueError("Не найден статус в ответе")

        code = int(status.find("easypay:code", ns).text)
        message = status.find("easypay:message", ns).text or ""

        if code == 200:
            epos_order = root.find(".//easypay:epos_order", ns).text
            qrcode_url = root.find(".//easypay:qrcode", ns).text
    
            await context.bot.send_photo(
                chat_id=update.effective_chat.id,
                photo=qrcode_url,
                caption=(
                    "✅ *Счёт успешно создан!*\n\n"
                    "Для оплаты:\n"
                    "Отсканируйте этот QR‑код\n"
                    "Или зайдите в свой интернет‑банкинг ➡️ Услуги ЕРИП ➡️ Сервис E-POS ➡️ E-POS - оплата товаров и услуг ➡️ В поле Лицевой счет вставьте номер счета ниже\n\n"
                    f"`{epos_order}`"
                ),
                parse_mode="Markdown"
            )
            
            context.user_data["pending_order_id"] = order_id
            asyncio.create_task(
                check_payment_loop(
                    order_id=order_id,
                    chat_id=update.effective_chat.id,
                    user_id=user_id,
                    bot=context.bot,
                    mer_no=MER_NO,
                    passwd=PASS
                )
            )
        else:
            logger.error(f"Ошибка Easypay: {code} - {message}")
            await query.edit_message_text("Ошибка создания счёта. Попробуйте позже.")
            return ConversationHandler.END

    except Exception as e:
        logger.error(f"Ошибка запроса к Easypay: {e}")
        await query.edit_message_text("Сервис оплаты временно недоступен. Попробуйте позже.")
        return ConversationHandler.END

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
        'INSERT OR REPLACE INTO payments (order_id, user_id, amount, status) VALUES (?, ?, ?, ?)',
        (order_id, user_id, 50.00, 'created')
        )
        await db.commit()

    await asyncio.sleep(5)
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="После оплаты ожидайте ее подтверждения. Обычно это занимает около 2-3 минут."
    )
    return ConversationHandler.END

async def check_payment_status(order_id: str, mer_no: str, passwd: str) -> bool:
    body_xml = f"""<EP_IsInvoicePaid xmlns="http://easypay.by/">
      <mer_no>{mer_no}</mer_no>
      <pass>{passwd}</pass>
      <order>{order_id}</order>
    </EP_IsInvoicePaid>"""
    soap_xml = _make_soap_envelope(body_xml)

    headers = {
        "Content-Type": "text/xml; charset=windows-1251",
        "SOAPAction": "http://easypay.by/EP_IsInvoicePaid",
    }

    try:
        resp = req_lib.post(
            "https://ssl.easypay.by/xml/server.php",
            data=soap_xml.encode("windows-1251"),
            headers=headers,
            timeout=10
        )
        if resp.status_code != 200:
            logger.error(f"HTTP ошибка при проверке: {resp.status_code}")
            return False

        root = ET.fromstring(resp.content)
        ns = {"easypay": "http://easypay.by/"}
        status = root.find(".//easypay:status", ns)
        code = int(status.find("easypay:code", ns).text)
        return code == 200
    except Exception as e:
        logger.error(f"Ошибка проверки оплаты: {e}")
        return False

async def check_payment_loop(order_id: str, chat_id: int, user_id: int, bot,
                             mer_no: str, passwd: str, max_attempts=10, interval=300):
    for attempt in range(1, max_attempts + 1):
        await asyncio.sleep(interval)
        logger.info(f"Проверка платежа {order_id}, попытка {attempt}/{max_attempts}")
        if await check_payment_status(order_id, mer_no, passwd):
            logger.info(f"Платёж {order_id} оплачен!")
            await grant_access_after_payment(user_id, bot)
            return
    logger.warning(f"Платёж {order_id} не был оплачен за {max_attempts * interval} сек.")

# ==================== КОМАНДЫ УПРАВЛЕНИЯ ====================

async def restart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Перезапуск воронки по /start."""
    context.user_data.clear()
    return await start(update, context)

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Отмена диалога."""
    await update.message.reply_text("Диалог прерван. Для начала напишите /start.")
    return ConversationHandler.END

async def send_message_to_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отправляет сообщение пользователю по его ID.
       Использование: /sendmsg <user_id> <текст сообщения>"""
    ADMIN_USER_IDS = [675468047, 753375245]  # ваши ID

    if update.effective_user.id not in ADMIN_USER_IDS:
        await update.message.reply_text("У вас нет прав на эту команду.")
        return

    # Проверяем аргументы
    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "Использование: /sendmsg <user_id> <текст сообщения>\n"
            "Пример: /sendmsg 675468047 Здравствуйте! У нас новый мастер-класс."
        )
        return

    try:
        target_user_id = int(context.args[0])
        message_text = ' '.join(context.args[1:])
    except ValueError:
        await update.message.reply_text("Неверный user_id. Он должен быть числом.")
        return

    try:
        await context.bot.send_message(
            chat_id=target_user_id,
            text=message_text
        )
        await update.message.reply_text(f"✅ Сообщение отправлено пользователю {target_user_id}.")
        logger.info(f"Админ {update.effective_user.id} отправил сообщение пользователю {target_user_id}")
    except Exception as e:
        await update.message.reply_text(f"Ошибка отправки: {e}")
        logger.error(f"Ошибка отправки сообщения: {e}")

async def broadcast_to_all_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Рассылает сообщение ВСЕМ пользователям, которые запускали бота."""
    ADMIN_USER_IDS = [675468047, 753375245]   # ← замените на свои ID

    if update.effective_user.id not in ADMIN_USER_IDS:
        await update.message.reply_text("У вас нет прав на эту команду.")
        return

    if not context.args:
        await update.message.reply_text(
            "Использование: /broadcast_all <текст сообщения>\n"
            "Пример: /broadcast_all Приглашаем на новый мастер-класс!"
        )
        return

    message_text = ' '.join(context.args)

    # Собираем все уникальные user_id из таблицы answers
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT DISTINCT user_id FROM answers")
        all_users = await cursor.fetchall()

    sent = 0
    failed = 0
    for (user_id,) in all_users:
        try:
            await context.bot.send_message(chat_id=user_id, text=message_text)
            sent += 1
            await asyncio.sleep(0.05)   # ≈ 20 сообщений в секунду — безопасно
        except Exception as e:
            logger.warning(f"Не удалось отправить пользователю {user_id}: {e}")
            failed += 1

    await update.message.reply_text(
        f"✅ Рассылка завершена: отправлено {sent}, ошибок {failed}."
    )

async def broadcast_all_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Рассылает фото всем пользователям. 
       Нужно ответить на фотографию командой /broadcast_all_photo <caption>"""
    ADMIN_USER_IDS = [675468047, 123456789]   # замените на свои ID

    if update.effective_user.id not in ADMIN_USER_IDS:
        await update.message.reply_text("У вас нет прав на эту команду.")
        return

    # Проверяем, что команда отправлена как ответ на сообщение с фото
    if not update.message.reply_to_message or not update.message.reply_to_message.photo:
        await update.message.reply_text(
            "Отправьте фото, затем ответьте на него командой:\n"
            "/broadcast_all_photo <текст подписи>\n"
            "Пример: /broadcast_all_photo Приглашаем на новый мастер-класс!"
        )
        return

    # Получаем file_id самого большого размера фото
    photo = update.message.reply_to_message.photo[-1]
    photo_id = photo.file_id

    # Текст подписи — всё после команды
    caption = ' '.join(context.args) if context.args else ""

    # Собираем всех уникальных пользователей
    import aiosqlite
    DB_NAME = 'data/bot_data.db'   # ваш путь к базе
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT DISTINCT user_id FROM answers")
        all_users = await cursor.fetchall()

    sent = 0
    failed = 0
    for (user_id,) in all_users:
        try:
            await context.bot.send_photo(
                chat_id=user_id,
                photo=photo_id,
                caption=caption if caption else None
            )
            sent += 1
            await asyncio.sleep(0.05)
        except Exception as e:
            logger.warning(f"Не удалось отправить фото пользователю {user_id}: {e}")
            failed += 1

    await update.message.reply_text(
        f"✅ Рассылка фото завершена: отправлено {sent}, ошибок {failed}."
    )

async def export_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Экспорт всех данных с расшифровкой и фильтрацией по дате."""
    if update.effective_user.id not in ADMIN_USER_IDS:
        await update.message.reply_text("❌ У вас нет прав.")
        return

    args = context.args
    start_date = end_date = None
    if len(args) == 2:
        try:
            start_date = datetime.strptime(args[0], "%Y-%m-%d").strftime("%Y-%m-%d 00:00:00")
            end_date = datetime.strptime(args[1], "%Y-%m-%d").strftime("%Y-%m-%d 23:59:59")
        except ValueError:
            await update.message.reply_text("Формат даты: ГГГГ-ММ-ДД. Пример: /export_all 2025-04-01 2025-04-30")
            return

    question_texts = {
        1: "1. Могу открыто говорить о своих чувствах",
        2: "2. Понимаю свои потребности в отношениях",
        3: "3. Знаю, что нужно делать для улучшения отношений",
        4: "4. Часто сомневаюсь в правильности поступков",
        5: "5. Слишком много даю и мало получаю взамен",
        101: "Реф1: Отношения требуют работы и осознанности",
        102: "Реф2: Чувствую себя более подготовленным(ой)",
        103: "Реф3: Понял(а) свои собственные потребности",
    }
    answer_map = {
        "answer_yes": "Да", "answer_no": "Нет", "answer_not_always": "Не всегда",
        "yes": "Да", "no": "Нет", "not_always": "Не всегда",
        "idk": "Не знаю"
    }
    payment_status_map = {"created": "Счёт выставлен", "paid": "Оплачен", "pending": "Ожидает"}

    output = io.StringIO()
    writer = csv.writer(output)

    # ---------- ANSWERS ----------
    writer.writerow(["=== ОТВЕТЫ НА ВОПРОСЫ ==="])
    writer.writerow(["ID", "User ID", "Username", "Имя", "Вопрос", "Ответ", "Дата"])

    query = "SELECT id, user_id, username, name, question_id, answer, created_at FROM answers"
    params = []
    if start_date and end_date:
        query += " WHERE created_at BETWEEN ? AND ?"
        params = [start_date, end_date]
    query += " ORDER BY created_at"

    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(query, params)
        rows = await cursor.fetchall()
        for row in rows:
            q_id = row[4]
            ans = row[5]
            writer.writerow([
                row[0], row[1], row[2] or "", row[3] or "",
                question_texts.get(q_id, f"Вопрос {q_id}"),
                answer_map.get(ans, ans)
            ] + [row[6]])

        # ---------- PAYMENTS ----------
        writer.writerow([])
        writer.writerow(["=== ПЛАТЕЖИ ==="])
        writer.writerow(["Order ID", "User ID", "Сумма (BYN)", "Статус", "Дата"])

        query = "SELECT order_id, user_id, amount, status, created_at FROM payments"
        params = []
        if start_date and end_date:
            query += " WHERE created_at BETWEEN ? AND ?"
            params = [start_date, end_date]
        query += " ORDER BY created_at"

        cursor = await db.execute(query, params)
        rows = await cursor.fetchall()
        for row in rows:
            writer.writerow([row[0], row[1], row[2], payment_status_map.get(row[3], row[3]), row[4]])

    output.seek(0)
    filename = "export.csv"
    if start_date and end_date:
        filename = f"export_{args[0]}_{args[1]}.csv"
    await context.bot.send_document(
        chat_id=update.effective_chat.id,
        document=output.getvalue().encode('utf-8-sig'),
        filename=filename,
        caption="📊 Данные экспортированы" + (f" за период {args[0]} – {args[1]}" if start_date else "")
    )


async def simulate_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Симулирует успешную оплату для тестирования всей воронки.
       Доступно только админу (проверка по user_id)."""
    
    if update.effective_user.id not in ADMIN_USER_IDS:
        await update.message.reply_text("У вас нет прав на эту команду.")
        return

    user_id = update.effective_user.id
    # Выдаём доступ (как после реального платежа)
    await grant_access_after_payment(user_id, context.bot)

    await update.message.reply_text(
        "(для теста) ✅ Доступ выдан (симулировано). Ожидайте вопрос через 6 часов.\n"
        "Хотите сократить время ожидания? Напишите /fast_forward. (для теста)"
    )

async def fast_forward(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Немедленно отправляет вопрос о просмотре МК (для тестирования)."""
    
    if update.effective_user.id not in ADMIN_USER_IDS:
        await update.message.reply_text("У вас нет прав на эту команду.")
        return

    # Эмулируем вызов задачи JobQueue
    job_context = {
        "chat_id": update.effective_chat.id,
        "user_id": update.effective_user.id,
        "bot": context.bot
    }
    # Имитируем объект job, необходимый для функции check_watched_mk
    class FakeJob:
        def __init__(self, chat_id, user_id):
            self.chat_id = chat_id
            self.user_id = user_id
    context.job = FakeJob(update.effective_chat.id, update.effective_user.id)
    await check_watched_mk(context)
    await update.message.reply_text("(для теста) Вопрос о просмотре отправлен. (для теста)")

# ==================== Нерелевантные отеты ====================

async def handle_unrelated_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отвечает на любое неожиданное сообщение."""
    text = update.message.text
    if text.startswith('/'):
        return  # команды обрабатываются отдельно
    await update.message.reply_text(
        "Не понял вас :(\n\n"
        "Пожалуйста, нажмите /start в меню, чтобы начать заново.\n"
        "Или, если у вас возникли трудности, напишите напрямую @Elena_lagodzich."
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in ADMIN_USER_IDS:
        text = (
            "<b>🛠️ Команды администратора</b>\n\n"
            "/start - Начать воронку заново\n"
            "/cancel - Прервать диалог\n"
            "/sendmsg ID текст - Отправить сообщение пользователю\n"
            "/broadcast_all текст - Текстовая рассылка всем\n"
            "/broadcast_all_photo подпись - Рассылка фото с подписью\n"
            "/export_all - Экспорт всех данных в CSV\n"
            "/export_all ГГГГ-ММ-ДД ГГГГ-ММ-ДД - Экспорт за период\n"
            "/test_payment - Симуляция оплаты\n"
            "/fast_forward - Быстрый переход к рефлексии\n"
            "/help - Показать это сообщение"
        )
    else:
        text = (
            "<b>👋 Помощь по боту</b>\n\n"
            "/start - Начать все заново\n"
            "/cancel - Прервать диалог\n"
            "/help - Показать это сообщение\n\n"
            "Если команды не помогают решить вашу проблему, напишите напрямую @Elena_lagodzich."
        )
    await update.message.reply_text(text, parse_mode="HTML")

async def get_file_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.video:
        fid = update.message.video.file_id
        await update.message.reply_text(f"🎬 video file_id:\n`{fid}`", parse_mode="Markdown")
    elif update.message.document:
        fid = update.message.document.file_id
        await update.message.reply_text(f"📎 document file_id:\n`{fid}`", parse_mode="Markdown")
    else:
        await update.message.reply_text("Отправьте видео (как видео или как файл).")

# ==================== MAIN ====================

async def main():
    """Асинхронная точка входа для запуска бота и веб-сервера."""
    global telegram_app

    telegram_app = Application.builder().token(TOKEN).build()

    telegram_app.add_handler(CallbackQueryHandler(handle_watched_response, pattern='^watched_'))
    telegram_app.add_handler(CallbackQueryHandler(handle_reflection_answer, pattern='^reflection_'))
    
    # ==================== КОМАНДЫ УПРАВЛЕНИЯ ====================
    telegram_app.add_handler(CommandHandler("sendmsg", send_message_to_user))
    telegram_app.add_handler(CommandHandler("broadcast_all", broadcast_to_all_users))
    telegram_app.add_handler(CommandHandler("broadcast_all_photo", broadcast_all_photo))
    telegram_app.add_handler(CommandHandler("export_all", export_all))
    telegram_app.add_handler(CommandHandler("test_payment", simulate_payment))
    telegram_app.add_handler(CommandHandler("fast_forward", fast_forward))

    telegram_app.add_handler(CommandHandler("help", help_command))

    telegram_app.add_handler(MessageHandler(filters.VIDEO | filters.Document.ALL, get_file_id), group=1)
   
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            ASK_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_name)],
            ASK_QUESTION_1: [CallbackQueryHandler(handle_question_answer, pattern="^answer_")],
            ASK_QUESTION_2: [CallbackQueryHandler(handle_question_answer, pattern="^answer_")],
            ASK_QUESTION_3: [CallbackQueryHandler(handle_question_answer, pattern="^answer_")],
            ASK_QUESTION_4: [CallbackQueryHandler(handle_question_answer, pattern="^answer_")],
            ASK_QUESTION_5: [CallbackQueryHandler(handle_question_answer, pattern="^answer_")],
            WATCH_VIDEO: [
                CallbackQueryHandler(handle_video_feedback, pattern="^video_feedback_"),
            ],
            ASK_VIDEO_FEEDBACK: [
                CallbackQueryHandler(start_payment, pattern="^start_payment$"),
            ],
            SELF_REFLECTION_1: [],
            SELF_REFLECTION_2: [],
            SELF_REFLECTION_3: [],
        },
        fallbacks=[
            CommandHandler("start", restart),
            CommandHandler("cancel", cancel),
        ],
    )
    telegram_app.add_handler(conv_handler)

    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_unrelated_message))

    await init_db()
    
    # Инициализация и запуск бота
    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.updater.start_polling()
    logger.info("Бот запущен в режиме polling")
    
    port = int(os.environ.get("PORT", 5000))
    from hypercorn.asyncio import serve
    from hypercorn.config import Config
    config = Config()
    config.bind = [f"0.0.0.0:{port}"]
    await serve(app, config)

    await telegram_app.stop()

if __name__ == "__main__":
    asyncio.run(main())
