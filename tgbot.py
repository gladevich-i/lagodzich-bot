import asyncio
import os
import logging
import json
import hashlib
import hmac
from datetime import datetime
from typing import Optional

import requests
from flask import Flask, request, jsonify

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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
TOKEN = os.getenv("BOT_TOKEN")
EASYPAY_API_URL = "https://api.easypay.by/v1/"  # или тестовый https://api.easypay.by/sandbox/v1/
EASYPAY_MERCHANT_ID = os.getenv("EASYPAY_MERCHANT_ID", "ВАШ_MERCHANT_ID")
EASYPAY_SECRET_KEY = os.getenv("EASYPAY_SECRET_KEY", "ВАШ_SECRET_KEY")
EASYPAY_SERVICE_ID = os.getenv("EASYPAY_SERVICE_ID", "ВАШ_SERVICE_ID")
EASYPAY_WEBHOOK_URL = "https://lagodzichbot.bothost.ru/webhook"  # если что, добавить easypay-webhook
PRIVATE_CHANNEL_INVITE_LINK = "https://t.me/+aBcDeFgHiJkLmNoPqRs"  # ссылка в закрытый канал
EXPERT_USERNAME = "Elena_lagodzich"  # без @

# ID видео в Telegram (получить через @getidsbot)
VIDEO_1_FILE_ID = "AAMCAgADGQEDBVQ-aeI1YfhpB6_pMcUVxufyRdel34sAAn2ZAAL1YxBL6JU6WbVHrbwBAAdtAAM7BA"  # видео для вопроса 1 (нет/не всегда)
VIDEO_2_FILE_ID = "AAMCAgADGQEDBVRDaeI1aEV455B1p_N_CeKxYElnQ8QAAn6ZAAL1YxBLu6DRzeeH0PgBAAdtAAM7BA"  # видео для вопроса 4 (да)
VIDEO_3_FILE_ID = "AAMCAgADGQEDBVRHaeI1buY8leSZ9neSfzhoYxmDzG4AAn-ZAAL1YxBLdQmX3RJMRxQBAAdtAAM7BA"  # видео для вопроса 5 (да)
DEFAULT_VIDEO_FILE_ID = "AAMCAgADGQEDBVRDaeI1aEV455B1p_N_CeKxYElnQ8QAAn6ZAAL1YxBLu6DRzeeH0PgBAAdtAAM7BA"  # общее видео, если условия не сработали

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

# ==================== ЛОГИРОВАНИЕ ====================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ==================== FLASK ДЛЯ ВЕБХУКА EASYPAY ====================
app = Flask(__name__)

# Глобальная ссылка на приложение бота (заполняется в main)
telegram_app: Optional[Application] = None

def verify_easypay_signature(request_data: bytes, signature_header: str) -> bool:
    """Проверяет подпись вебхука EasyPay."""
    if not signature_header:
        return False
    expected = hmac.new(
        EASYPAY_SECRET_KEY.encode(), request_data, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header)

@app.route("/webhook", methods=["POST"]) # если что, добавить easypay-webhook
def easypay_webhook():
    """Принимает уведомления об оплате от EasyPay."""
    signature = request.headers.get("X-Signature")
    if not verify_easypay_signature(request.data, signature):
        logger.warning("Неверная подпись вебхука EasyPay")
        return "Invalid signature", 403

    data = request.json
    logger.info(f"Получен вебхук EasyPay: {data}")

    # Обрабатываем только успешные платежи
    if data.get("status") == "successful":
        order_id = data.get("order_id")
        # Из order_id извлекаем user_id (формат: mk_{user_id}_{timestamp})
        try:
            user_id = int(order_id.split("_")[1])
        except (IndexError, ValueError):
            logger.error(f"Не удалось извлечь user_id из order_id: {order_id}")
            return "Invalid order_id", 400

        # Выдаём доступ в канал
        if telegram_app:
            telegram_app.create_task(
                grant_access_after_payment(user_id, telegram_app.bot)
            )
        else:
            logger.error("telegram_app не инициализирован")

    return "OK", 200

async def grant_access_after_payment(user_id: int, bot):
    """Отправляет пользователю пригласительную ссылку в канал."""
    try:
        await bot.send_message(
            chat_id=user_id,
            text=(
                f"✅ *Оплата прошла успешно!*\n\n"
                f"Ваш доступ к мастер-классу открыт.\n"
                f"Перейдите по ссылке и присоединяйтесь:\n"
                f"{PRIVATE_CHANNEL_INVITE_LINK}\n\n"
                f"Ссылка действительна только для вас. Пожалуйста, не передавайте её."
            ),
            parse_mode="Markdown",
        )
        logger.info(f"Доступ отправлен пользователю {user_id}")
    except Exception as e:
        logger.error(f"Ошибка отправки доступа пользователю {user_id}: {e}")

# ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Приветствие и запрос имени."""
    user = update.effective_user
    context.user_data.clear()
    context.user_data["user_id"] = user.id
    context.user_data["username"] = user.username
    context.user_data["started_at"] = datetime.now().isoformat()

    welcome_text = (
        f"👋 *Здравствуйте!*\n\n"
        "Я — помощник Елены Лагодич, эксперта в области психологии отношений с многолетним опытом работы. Я помогу вам лучше понять себя и свои отношения.\n\n"
        "🔒 *Конфиденциальность гарантирована.* Все ваши ответы останутся между нами. Они нужны только для того, чтобы сделать нашу работу максимально точной и полезной для вас.\n\n"
        "👉 *Давайте познакомимся.* Как я могу к вам обращаться? Напишите ваше имя."
    )
    await update.message.reply_text(welcome_text, parse_mode="Markdown")
    return ASK_NAME

async def ask_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Сохраняет имя, приветствует пользователя и начинает анкету."""
    # Сохраняем имя в user_data
    context.user_data["name"] = update.message.text

    # Промежуточное сообщение — благодарность и введение в опрос
    thank_you_text = (
        f"Спасибо, *{context.user_data['name']}*!\n\n"
        "Давайте перейдём к короткому опросу, чтобы я мог лучше понять вашу ситуацию. "
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
        return await send_video_based_on_answers(update, context)

async def send_video_based_on_answers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Определяет, какой файл отправить, и отправляет его как документ."""
    answers = context.user_data.get("answers", [])
    ans1 = next((a["answer"] for a in answers if a["question"] == 1), None)
    ans4 = next((a["answer"] for a in answers if a["question"] == 4), None)
    ans5 = next((a["answer"] for a in answers if a["question"] == 5), None)

    video_id = DEFAULT_VIDEO_FILE_ID
    stop_funnel = False

    if ans1 in ("answer_no", "answer_not_always"):
        video_id = VIDEO_1_FILE_ID
        stop_funnel = True
        logger.info("Отправка видео 1 (вопрос 1 нет/не всегда)")
    elif ans4 == "answer_yes":
        video_id = VIDEO_2_FILE_ID
        stop_funnel = True
        logger.info("Отправка видео 2 (вопрос 4 да)")
    elif ans5 == "answer_yes":
        video_id = VIDEO_3_FILE_ID
        stop_funnel = True
        logger.info("Отправка видео 3 (вопрос 5 да)")

    # Отправляем файл как документ с обработкой ошибок
    try:
        await context.bot.send_document(
            chat_id=update.effective_chat.id,
            document=video_id,
            caption="Отрывок из мастер-класса Елены Лагодич"
        )
    except Exception as e:
        logger.error(f"Ошибка при отправке документа: {e}")
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="Извините, произошла ошибка при загрузке видео. Попробуйте позже или обратитесь к администратору."
        )
        return ConversationHandler.END

    if stop_funnel:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="Благодарим за участие! Если захотите пройти анкету снова, нажмите /start."
        )
        return ConversationHandler.END
    else:
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
        offer_text = (
            "Значит вы попали сюда не зря!*\n\n"
            "Приглашаю вас посмотреть углубленный мастер-класс по отношениям от Елены Лагодич.\n"
            "На нем вы узнаете:\n"
            "✅ Как выстроить гармоничные отношения\n"
            "✅ Где брать ресурс и энергию\n"
            "✅ Техники, которые помогут уже сегодня\n"
            "✅ Презентацию с полезными лайфхаками и упражнениями\n\n"
            "Стоимость доступа — всего 50 бел. руб.\n\n"
            "👇 Нажмите кнопку ниже для оплаты."
        )
        keyboard = [[InlineKeyboardButton("💳 Перейти к оплате", callback_data="start_payment")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(offer_text, reply_markup=reply_markup, parse_mode="Markdown")
        return ASK_VIDEO_FEEDBACK

async def start_payment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Создаёт платёжную ссылку EasyPay и отправляет пользователю."""
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    name = context.user_data.get("name", "Участник")
    order_id = f"mk_{user_id}_{int(datetime.now().timestamp())}"

    # Формируем запрос к EasyPay API для создания платежа
    payload = {
        "merchant_id": EASYPAY_MERCHANT_ID,
        "service_id": EASYPAY_SERVICE_ID,
        "order_id": order_id,
        "amount": "50.00",  # Сумма в BYN (или RUB, зависит от настроек)
        "currency": "BYN",
        "description": f"Мастер-класс по отношениям ({name})",
        "customer": {
            "first_name": name,
            "telegram_id": str(user_id)
        },
        "notification_url": EASYPAY_WEBHOOK_URL,
        "success_url": "https://t.me/lagodzich_bot",  # после оплаты пользователь вернётся в Telegram
        "cancel_url": "https://t.me/lagodzich_bot",
    }

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {EASYPAY_SECRET_KEY}"
    }

    try:
        response = requests.post(
            f"{EASYPAY_API_URL}payment",
            json=payload,
            headers=headers,
            timeout=10
        )
        response.raise_for_status()
        payment_data = response.json()
        payment_url = payment_data.get("payment_url")

        if payment_url:
            await query.edit_message_text(
                f"✅ *Ссылка для оплаты готова!*\n\n"
                f"[Нажмите сюда, чтобы оплатить]({payment_url})\n\n"
                f"После успешной оплаты доступ к мастер-классу придёт автоматически.",
                parse_mode="Markdown",
                disable_web_page_preview=True
            )
            context.user_data["pending_order_id"] = order_id
        else:
            await query.edit_message_text("Ошибка создания платежа. Попробуйте позже.")
            return ConversationHandler.END
    except Exception as e:
        logger.error(f"Ошибка EasyPay: {e}")
        await query.edit_message_text("Сервис оплаты временно недоступен. Попробуйте позже.")
        return ConversationHandler.END

    # Ждём вебхук, но пользователю уже отправлена ссылка, диалог завершается
    # (дальнейшее взаимодействие — через вебхук)
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="Ожидайте подтверждения оплаты. Обычно это занимает до минуты."
    )
    return ConversationHandler.END

async def restart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Перезапуск воронки по /start."""
    context.user_data.clear()
    return await start(update, context)

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Отмена диалога."""
    await update.message.reply_text("Диалог прерван. Для начала напишите /start.")
    return ConversationHandler.END

# ==================== MAIN ====================

async def main():
    """Асинхронная точка входа для запуска бота и веб-сервера."""
    global telegram_app

    # Создаём приложение Telegram
    telegram_app = Application.builder().token(TOKEN).build()

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
    
    async def get_document_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    document = update.message.document
    if document:
        await update.message.reply_text(f"📎 file_id документа:\n`{document.file_id}`", parse_mode="Markdown")

    # Временно регистрируем обработчик
    telegram_app.add_handler(MessageHandler(filters.Document.ALL, get_document_id))
    
    

    # Инициализация и запуск бота
    await telegram_app.initialize()
    await telegram_app.start()
    # В ptb v20 polling запускается через updater
    await telegram_app.updater.start_polling()
    logger.info("Бот запущен в режиме polling")

    # Запускаем Flask-сервер для приёма уведомлений от EasyPay
    port = int(os.environ.get("PORT", 5000))
    from hypercorn.asyncio import serve
    from hypercorn.config import Config
    config = Config()
    config.bind = [f"0.0.0.0:{port}"]
    await serve(app, config)

    await telegram_app.stop()

if __name__ == "__main__":
    asyncio.run(main())
