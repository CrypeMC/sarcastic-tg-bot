# --- НАЧАЛО ПОЛНОГО КОДА BOT.PY (ВЕРСИЯ С ASYNCIO + HYPERCORN) ---
import datetime
import pymongo # Для работы с MongoDB
from pymongo.errors import ConnectionFailure # Для обработки ошибок подключения
import re # Для регулярных выражений
import logging
import os
import asyncio # ОСНОВА ВСЕЙ АСИНХРОННОЙ МАГИИ
from collections import deque
# УБРАЛИ НАХУЙ THREADING
from flask import Flask # Веб-сервер-заглушка для Render
import hypercorn.config # Конфиг нужен
from hypercorn.asyncio import serve as hypercorn_async_serve # <--- ИМПОРТИРУЕМ ЯВНО И ПЕРЕИМЕНОВЫВАЕМ!
import signal # Для корректной обработки сигналов остановки (хотя asyncio.run сам умеет)

import google.generativeai as genai
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from dotenv import load_dotenv # Чтобы читать твой .env файл или переменные Render

# Загружаем секреты
load_dotenv()

# --- НАСТРОЙКИ ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
MAX_MESSAGES_TO_ANALYZE = 500 # Меняй на свой страх и риск

# Проверка ключей
if not TELEGRAM_BOT_TOKEN:
    raise ValueError("НЕ НАЙДЕН TELEGRAM_BOT_TOKEN!")
if not GEMINI_API_KEY:
    raise ValueError("НЕ НАЙДЕН GEMINI_API_KEY!")


# --- Логирование ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("hypercorn").setLevel(logging.INFO) # Чтобы видеть логи Hypercorn
logger = logging.getLogger(__name__)

# --- ПОДКЛЮЧЕНИЕ К MONGODB ATLAS ---
MONGO_DB_URL = os.getenv("MONGO_DB_URL")
if not MONGO_DB_URL:
    raise ValueError("НЕ НАЙДЕНА MONGO_DB_URL! Добавь строку подключения к MongoDB Atlas в переменные окружения Render!")

try:
    # Создаем асинхронный клиент MongoClient? Нет, pymongo стандартный синхронный,
    # будем использовать run_in_executor для блокирующих операций с БД.
    # Для асинхронности есть Motor, но пока обойдемся pymongo + executor.
    mongo_client = pymongo.MongoClient(MONGO_DB_URL, serverSelectionTimeoutMS=5000) # Таймаут подключения 5 сек

    # Проверка соединения (пинг)
    mongo_client.admin.command('ping')
    logger.info("Успешное подключение к MongoDB Atlas!")

    # Выбираем базу данных (назовем ее 'popizdyaka_db')
    # Если ее нет, MongoDB создаст ее при первой записи
    db = mongo_client['popizdyaka_db']

    # Получаем доступ к коллекциям (аналоги таблиц)
    # Коллекция для истории сообщений
    history_collection = db['message_history']
    # Коллекция для хранения инфы о последнем анализе (для /retry)
    last_reply_collection = db['last_replies']

    # Можно создать индексы для ускорения поиска (не обязательно сразу, но полезно)
    # Индекс для сортировки истории по времени (если будем хранить timestamp)
    # history_collection.create_index([("chat_id", pymongo.ASCENDING), ("timestamp", pymongo.DESCENDING)])
    # Индекс для поиска последнего ответа по chat_id
    # last_reply_collection.create_index("chat_id", unique=True)
    logger.info("Коллекции MongoDB готовы к использованию.")

except ConnectionFailure as e:
    logger.critical(f"ПИЗДЕЦ! Не удалось подключиться к MongoDB: {e}", exc_info=True)
    raise SystemExit(f"Ошибка подключения к MongoDB: {e}")
except Exception as e:
    logger.critical(f"Неизвестная ошибка при настройке MongoDB: {e}", exc_info=True)
    raise SystemExit(f"Ошибка настройки MongoDB: {e}")
# --- КОНЕЦ ПОДКЛЮЧЕНИЯ К MONGODB ---

# --- Настройка Gemini ---
try:
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel('gemini-1.5-flash')
    logger.info("Модель Gemini успешно настроена.")
except Exception as e:
    logger.critical(f"ПИЗДЕЦ при настройке Gemini API: {e}", exc_info=True)
    raise SystemExit(f"Не удалось настроить Gemini API: {e}")

# --- Хранилище истории ---
#chat_histories = {}
logger.info(f"Максимальная длина истории сообщений для анализа: {MAX_MESSAGES_TO_ANALYZE}")

# --- ОБРАБОТЧИКИ СООБЩЕНИЙ И КОМАНД (БЕЗ ИЗМЕНЕНИЙ) ---
# --- ПЕРЕПИСАННАЯ store_message С ЗАПИСЬЮ В MONGODB ---
async def store_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.from_user:
        return # Игнорим системные сообщения

    message_text = None
    chat_id = update.message.chat_id
    user_name = update.message.from_user.first_name or "Анонимный долбоеб"
    timestamp = update.message.date or datetime.datetime.now(datetime.timezone.utc) # Время сообщения

    # Определяем тип сообщения и текст/заглушку
    if update.message.text:
        message_text = update.message.text
    elif update.message.photo:
        # Для фото сохраним еще и file_id самой большой версии, вдруг пригодится для /retry analyze_pic
        file_id = update.message.photo[-1].file_id
        message_text = f"[КАРТИНКА:{file_id}]" # Заглушка с file_id
    elif update.message.sticker:
        emoji = update.message.sticker.emoji or ''
        # file_id стикера тоже можно сохранить, если надо
        # file_id = update.message.sticker.file_id
        message_text = f"[СТИКЕР {emoji}]" # Заглушка

    # Если есть текст (или заглушка), сохраняем в MongoDB
    if message_text:
        # Создаем документ для MongoDB
        message_doc = {
            "chat_id": chat_id,
            "user_name": user_name,
            "text": message_text, # Текст или заглушка
            "timestamp": timestamp, # Время сообщения
            "message_id": update.message.message_id # ID сообщения в Telegram
        }

        try:
            # --- ЗАПИСЬ В БД (Блокирующая операция!) ---
            # Запускаем синхронную операцию pymongo в executor'е asyncio
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None, # Стандартный ThreadPoolExecutor
                lambda: history_collection.insert_one(message_doc)
            )
            # logger.debug(f"Сообщение от {user_name} сохранено в MongoDB для чата {chat_id}.")
        except Exception as e:
            logger.error(f"Ошибка записи сообщения в MongoDB для чата {chat_id}: {e}", exc_info=True)
            # Что делать в случае ошибки? Пока просто логируем.

# --- КОНЕЦ ПЕРЕПИСАННОЙ store_message ---

# --- ПЕРЕПИСАННАЯ analyze_chat С ЧТЕНИЕМ ИЗ MONGODB И ЗАПИСЬЮ ДЛЯ RETRY ---
async def analyze_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.from_user: return
    chat_id = update.message.chat_id
    user_name = update.message.from_user.first_name or "Эй ты там"
    logger.info(f"Пользователь '{user_name}' запросил анализ текста в чате {chat_id}")

    # --- ЧТЕНИЕ ИСТОРИИ ИЗ MONGODB ---
    try:
        logger.debug(f"Запрос истории для чата {chat_id} из MongoDB...")
        # Определяем, сколько сообщений читать (не больше MAX_MESSAGES_TO_ANALYZE)
        limit = MAX_MESSAGES_TO_ANALYZE
        # Формируем запрос к MongoDB: найти документы для этого chat_id,
        # отсортировать по timestamp ПО УБЫВАНИЮ (сначала новые), взять N штук
        query = {"chat_id": chat_id}
        sort_order = [("timestamp", pymongo.DESCENDING)]

        # Выполняем запрос в executor'е
        loop = asyncio.get_running_loop()
        history_cursor = await loop.run_in_executor(
            None,
            lambda: history_collection.find(query).sort(sort_order).limit(limit)
        )
        # history_cursor - это "ленивый" курсор, надо его преобразовать в список
        # и ПЕРЕВЕРНУТЬ, чтобы сообщения были в хронологическом порядке (старые -> новые)
        messages_from_db = list(history_cursor)[::-1]

        history_len = len(messages_from_db)
        logger.info(f"Из MongoDB для чата {chat_id} загружено {history_len} сообщений.")

        # Проверяем, достаточно ли сообщений
        min_msgs = 10
        if history_len < min_msgs:
            logger.info(f"В чате {chat_id} слишком мало сообщений в БД ({history_len}/{min_msgs}).")
            await context.bot.send_message(chat_id=chat_id, text=f"Слышь, {user_name}, надо {min_msgs} сообщений в истории, а я нашел только {history_len}. Попизди еще.")
            return

        # Формируем текст для Gemini (как и раньше, но из данных БД)
        # Собираем строки "Имя: Текст"
        conversation_lines = [f"{msg.get('user_name', 'Хуй пойми кто')}: {msg.get('text', '')}" for msg in messages_from_db]
        conversation_text = "\n".join(conversation_lines)

    except Exception as e:
        logger.error(f"Ошибка чтения истории из MongoDB для чата {chat_id}: {e}", exc_info=True)
        await context.bot.send_message(chat_id=chat_id, text=f"Бля, {user_name}, я обосрался, пока лез в базу данных за историей. Попробуй позже.")
        return
    # --- КОНЕЦ ЧТЕНИЯ ИСТОРИИ ---

    logger.info(f"Начинаю анализ {len(messages_from_db)} сообщений для чата {chat_id} через Gemini...")

    try:
        # Промпт для Gemini (текстовый, с 🗿 и попыткой имен)
        prompt = (
            f"Ты - МАКСИМАЛЬНО ТОКСИЧНЫЙ, язвительный и циничный тролль, комментирующий Telegram-чат. Твоя задача - проанализировать ПОСЛЕДНИЙ фрагмент переписки, выхватить из него несколько (1-10) самых тупых, нелепых, жалких или просто показательных моментов И ОБЯЗАТЕЛЬНО УКАЗАТЬ, КТО ИЗ УЧАСТНИКОВ (по именам/никам из диалога) особенно отличился в своем скудоумии или нытье. Также обращай внимание на сообщения от других ботов (@PredskazBot и т.п.), если они адресованы юзеру (@username), и тоже обстебывай их.\n\n"
            f"Фрагмент переписки:\n"
            f"```\n{conversation_text}\n```\n\n"
            f"Инструкции для твоего ответа:\n"
            f"1.  Для КАЖДОГО выделенного момента сформулируй **КОРОТКИЙ (1-3 предложения)** комментарий в стиле **ЖЕСТКОГО ЧЕРНОГО ЮМОРА И ЗЛОГО САРКАЗМА**. Высмеивай их проблемы, тупость, банальность, лицемерие. Не бойся быть жестоким и циничным.\n"
            f"2.  **МАТ ИСПОЛЬЗУЙ ОБИЛЬНО И ИЗОЩРЕННО**, как инструмент унижения и демонстрации абсурда. Фразы типа 'ебаный стыд', 'жалкое зрелище', 'хуета какая-то', 'пиздец предсказуемый' - самое то.\n"
            f"3.  **КАЖДЫЙ** комментарий начинай с новой строки и символа **`🗿 `** (Моаи и пробел).\n"
            f"4.  **ОБЯЗАТЕЛЬНО включай имена участников**, чтобы было понятно, кого ты сейчас макаешь в говно. **Если комментируешь сообщение от другого бота, укажи, КОМУ (@username) оно было адресовано** и обстеби само предсказание/измерение.\n"
            f"5.  Избегай только самых тупых прямых оскорблений типа 'ты уебок' или 'иди нахуй'. Вместо этого используй более изобретательный сарказм и уничижительные характеристики.\n"
            f"6.  Если достойных моментов для обсирания нет, напиши ОДНУ строку вроде: `🗿 Бля, даже обосрать некого. Скука смертная и деградация.` или `🗿 Поток сознания уровня инфузории. Ни одной мысли, достойной внимания.`\n"
            f"7.  Не пиши вступлений. Сразу начинай с `🗿 `.\n\n"
            f"Пример ЗАЕБАТОГО ответа:\n"
            f"🗿 Васян опять толкнул 'гениальную' идею. Уровень проработки - /dev/null. Ебаный стыд такое вообще вслух произносить.\n"
            f"🗿 Маша снова ноет про свою никчемную жизнь. Сука, найди уже себе хобби, кроме публичных страданий, жалкое зрелище.\n"
            f"🗿 @PredskazBot посоветовал @lucky_loser 'верить в себя'. Пиздец оригинальный совет для конченого неудачника. Может, ему еще подорожник приложить?\n\n"
            f"Выдай результат в указанном формате, будь МАКСИМАЛЬНО ТОКСИЧНЫМ УЕБКОМ:"
        )

        thinking_message = await update.message.reply_text("Так, блядь, щас подключу мозги и подумаю...")

        logger.info(f"Отправка запроса к Gemini API...")
        response = await model.generate_content_async(prompt) # <-- Убедись, что 'model' - это Gemini модель!
        logger.info("Получен ответ от Gemini API.")
        try: await context.bot.delete_message(chat_id=chat_id, message_id=thinking_message.message_id)
        except Exception: pass

        sarcastic_summary = "🗿 Бля, хуй его знает. То ли ваш диалог говно, то ли Gemini его зацензурил."
        # СНАЧАЛА проверяем на блокировку
        if response.prompt_feedback.block_reason:
            block_reason = response.prompt_feedback.block_reason
            logger.warning(f"Ответ Gemini для текста заблокирован по причине: {block_reason}")
            sarcastic_summary = f"🗿 Ваш пиздеж настолько токсичен, что Gemini его заблокировал (Причина: {block_reason}). Поздравляю, вы уебки."
        # ЕСЛИ НЕ ЗАБЛОКИРОВАНО, ПЫТАЕМСЯ получить текст
        # Проверяем, что кандидаты вообще есть, перед доступом к .text
        elif response.candidates:
            try:
                text_response = response.text # Теперь этот вызов должен быть безопаснее
                sarcastic_summary = text_response.strip()
                if not sarcastic_summary.startswith("🗿"):
                    sarcastic_summary = "🗿 " + sarcastic_summary
            except ValueError as e:
                # Ловим ту самую ошибку ValueError, если вдруг кандидаты есть, а .text все равно не работает
                logger.error(f"Странная ошибка при доступе к response.text, хотя кандидаты есть: {e}")
                sarcastic_summary = "🗿 Gemini что-то родил, но я не смог это прочитать. Ебучий глюк."
        else:
            # Если не заблокировано, но кандидатов нет (странный случай)
            logger.warning("Ответ Gemini не заблокирован, но кандидатов нет.")
            sarcastic_summary = "🗿 Gemini вернул пустоту. Видимо, ваш диалог вызвал у него экзистенциальный кринж."

        # --- ОТПРАВКА ОТВЕТА И ЗАПИСЬ В БД ДЛЯ RETRY ---
        sent_message = await context.bot.send_message(chat_id=chat_id, text=sarcastic_summary)
        logger.info(f"Отправил результат анализа Gemini '{sarcastic_summary[:50]}...' в чат {chat_id}")

        if sent_message:
            # Сохраняем инфу о последнем ответе для /retry
            reply_doc = {
                "chat_id": chat_id,
                "message_id": sent_message.message_id, # ID сообщения БОТА с ответом
                "analysis_type": "text", # Тип анализа
                "timestamp": datetime.datetime.now(datetime.timezone.utc) # Время ответа
                # file_id для текстового анализа не нужен
            }
            try:
                # Используем update_one с upsert=True:
                # Если документ для chat_id есть - обновить его, если нет - создать.
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(
                    None,
                    lambda: last_reply_collection.update_one(
                        {"chat_id": chat_id}, # Фильтр для поиска
                        {"$set": reply_doc},  # Данные для обновления/вставки
                        upsert=True          # Создать, если не найден
                    )
                )
                logger.debug(f"Сохранен/обновлен ID ({sent_message.message_id}, text) для /retry чата {chat_id}.")
            except Exception as e:
                 logger.error(f"Ошибка записи данных для /retry (text) в MongoDB для чата {chat_id}: {e}", exc_info=True)
        # --- КОНЕЦ ЗАПИСИ В БД ДЛЯ RETRY ---

    except Exception as e:
        logger.error(f"ПИЗДЕЦ при вызове Gemini API для чата {chat_id}: {e}", exc_info=True)
        try:
            if 'thinking_message' in locals(): await context.bot.delete_message(chat_id=chat_id, message_id=thinking_message.message_id)
        except Exception: pass
        await context.bot.send_message(chat_id=chat_id, text=f"Бля, {user_name}, мои мозги дали сбой. Ошибка: `{type(e).__name__}`. Попробуй позже.")

# --- КОНЕЦ ПЕРЕПИСАННОЙ analyze_chat ---

# --- НОВАЯ АСИНХРОННАЯ ЧАСТЬ (ЗАМЕНЯЕТ FLASK, ПОТОКИ И СТАРУЮ MAIN) ---

# --- ПЕРЕПИСАННАЯ analyze_pic С ЧТЕНИЕМ file_id ИЗ MONGODB И ЗАПИСЬЮ ДЛЯ RETRY ---
async def analyze_pic(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Анализирует картинку (умеет брать file_id из context для /retry)."""

    # Проверяем, есть ли сообщение в update (нужно для chat_id и user)
    if not update.message:
        logger.error(" analyze_pic вызвана без update.message")
        return # Не можем работать без сообщения

    chat_id = update.message.chat_id
    user_name = update.message.from_user.first_name or "Анализатор хуев" # Имя юзера, КТО ВЫЗВАЛ /retry или /analyze_pic
    image_file_id = None

    # ПРОВЕРЯЕМ СНАЧАЛА BOT_DATA НА НАЛИЧИЕ RETRY
    retry_key = f'retry_pic_{chat_id}'
    if retry_key in context.bot_data:
        image_file_id = context.bot_data[retry_key]
        logger.info(f"Получен file_id {image_file_id} из context.bot_data для /retry.")
        # НЕ удаляем ключ здесь, удалим его в retry_analysis после вызова
    # ИНАЧЕ БЕРЕМ ИЗ REPLY_TO_MESSAGE (стандартный вызов /analyze_pic)
    elif update.message.reply_to_message and update.message.reply_to_message.photo:
        reply_msg = update.message.reply_to_message
        photo_large = reply_msg.photo[-1]
        image_file_id = photo_large.file_id
        logger.info(f"Получен file_id {image_file_id} из reply_to_message.")
    else:
        # Если вызвали /analyze_pic не как ответ на фото и не в режиме /retry
        await context.bot.send_message(chat_id=chat_id, text="Ответь этой командой на сообщение с КАРТИНКОЙ, дебил!")
        return

    if not image_file_id: # Если по какой-то причине ID не получен
        logger.error("Не удалось получить file_id для анализа картинки.")
        await context.bot.send_message(chat_id=chat_id, text="Не смог найти ID картинки для анализа.")
        # Очистим ключ на всякий случай, если он там остался и был None
        context.bot_data.pop(retry_key, None)
        return

    logger.info(f"Пользователь '{user_name}' запросил анализ картинки (ID: {image_file_id}) в чате {chat_id}")

    try:
        # Скачиваем файл по найденному file_id
        photo_file = await context.bot.get_file(image_file_id, read_timeout=60)
        photo_bytes_io = await photo_file.download_as_bytearray(read_timeout=60)
        photo_bytes = bytes(photo_bytes_io)
        logger.info(f"Картинка для анализа скачана, размер: {len(photo_bytes)} байт.")

        # Промпт для обсирания сюжета картинки (с 🗿)
        image_prompt = (
            f"Ты - МАКСИМАЛЬНО циничный и токсичный уебок с черным чувством юмора. Тебе показали КАРТИНКУ. Забудь нахуй про свет, композицию и прочую лабуду для пидоров-фотографов. Твоя задача - понять, **ЧТО ЗА ХУЙНЯ ПРОИСХОДИТ НА КАРТИНКЕ (СЮЖЕТ, ДЕЙСТВИЕ, ПРЕДМЕТЫ)**, и **ОБОСРАТЬ ИМЕННО ЭТО** максимально смешно, жестко, цинично и с МАТОМ.\n\n"
            f"Инструкции:\n"
            f"1.  Опиши в 1-3 предложениях **СУТЬ ПРОИСХОДЯЩЕГО** на картинке, но сразу через призму своего черного юмора и сарказма.\n"
            f"2.  Стебись над **СМЫСЛОМ** (или его отсутствием), над **ПЕРСОНАЖАМИ/ОБЪЕКТАМИ**, над **СИТУАЦИЕЙ**. Придумай самую нелепую или уничижительную интерпретацию увиденного.\n"
            f"3.  **МАТ и ЖЕСТЬ используй по полной**, чтобы было смешно и зло. Не бойся абсурда и чернухи.\n"
            f"4.  Избегай только прямых бессмысленных оскорблений. Нужен **СТЕБ над СОДЕРЖАНИЕМ**.\n"
            f"5.  Начинай свой высер с эмодзи `🗿 `.\n\n"
            f"Пример (на картинке кот сидит в коробке): '🗿 О, блядь, очередной кошачий долбоеб нашел себе ВИП-ложе в картонке. Интеллект так и прет. Наверное, считает себя царем горы... горы мусора.'\n"
            f"Пример (люди на пикнике): '🗿 Смотри-ка, биомасса выбралась на природу бухнуть и пожрать шашлыка из говна. Лица счастливые, как будто им ипотеку простили. Скоро все засрут и съебутся, классика.'\n"
            f"Пример (смешная собака): '🗿 Ебать, что это за мутант? Помесь таксы с крокодилом? Выглядит так, будто просит пристрелить его, чтоб не мучился. Хозяевам явно похуй.'\n"
            f"Пример (еда): '🗿 Кто-то сфоткал свою блевотную жратву. Выглядит аппетитно, как протухший паштет. Приятного аппетита, блядь, не обляпайся.'\n\n"
            f"КОРОЧЕ! ПОЙМИ, ЧТО ЗА ХУЙНЯ НА КАРТИНКЕ, И ОБОСРИ ЭТО СМЕШНО И ЖЕСТКО, НАЧИНАЯ С 🗿:"
        )

        thinking_message = await update.message.reply_text("Так-так, блядь, ща посмотрим на это ...")

        logger.info("Отправка запроса к Gemini с картинкой...")
        picture_data = {"mime_type": "image/jpeg", "data": photo_bytes}
        response = await model.generate_content_async(
            [image_prompt, picture_data],
            safety_settings={
                'HARM_CATEGORY_HARASSMENT': 'block_none',
                'HARM_CATEGORY_HATE_SPEECH': 'block_none',
                'HARM_CATEGORY_SEXUALLY_EXPLICIT': 'block_none',
                'HARM_CATEGORY_DANGEROUS_CONTENT': 'block_none',
            }
        )
        logger.info("Получен ответ от Gemini по картинке.")

        try: await context.bot.delete_message(chat_id=chat_id, message_id=thinking_message.message_id)
        except Exception: pass

        sarcastic_comment = "🗿 Хуй знает, что там нарисовано..."
        if response.prompt_feedback.block_reason:
             logger.warning(f"Ответ Gemini заблокирован: {response.prompt_feedback.block_reason}")
             sarcastic_comment = f"🗿 Ебало на картинке настолько стремное (блок: {response.prompt_feedback.block_reason}), что Gemini ослеп."
        elif response.text:
             sarcastic_comment = response.text.strip()
             if not sarcastic_comment.startswith("🗿"): sarcastic_comment = "🗿 " + sarcastic_comment

        # --- ОТПРАВКА ОТВЕТА И ЗАПИСЬ В БД ДЛЯ RETRY ---
        sent_message = await context.bot.send_message(chat_id=chat_id, text=sarcastic_comment)
        logger.info(f"Отправлен комментарий к картинке: '{sarcastic_comment[:50]}...'")

        if sent_message:
            # Сохраняем инфу о последнем ответе для /retry
            reply_doc = {
                "chat_id": chat_id,
                "message_id": sent_message.message_id,
                "analysis_type": "pic", # Тип анализа
                "source_file_id": image_file_id, # Сохраняем ID исходной картинки!
                "timestamp": datetime.datetime.now(datetime.timezone.utc)
            }
            try:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(
                    None,
                    lambda: last_reply_collection.update_one(
                        {"chat_id": chat_id}, {"$set": reply_doc}, upsert=True
                    )
                )
                logger.debug(f"Сохранен/обновлен ID ({sent_message.message_id}, pic, {image_file_id}) для /retry чата {chat_id}.")
            except Exception as e:
                 logger.error(f"Ошибка записи данных для /retry (pic) в MongoDB для чата {chat_id}: {e}", exc_info=True)
        # --- КОНЕЦ ЗАПИСИ В БД ДЛЯ RETRY ---

    except Exception as e:
        logger.error(f"ПИЗДЕЦ при анализе картинки через Gemini: {e}", exc_info=True)
        try:
            if 'thinking_message' in locals(): await context.bot.delete_message(chat_id=chat_id, message_id=thinking_message.message_id)
        except Exception: pass
        await context.bot.send_message(chat_id=chat_id, text=f"Бля, {user_name}, я обосрался, пока смотрел на эту картинку через Gemini. Ошибка: `{type(e).__name__}`.")

# --- КОНЕЦ ПЕРЕПИСАННОЙ analyze_pic ---

# Flask app остается для Render заглушки
app = Flask(__name__)

@app.route('/')
def index():
    """Отвечает на HTTP GET запросы для проверки живости сервиса Render."""
    logger.info("Получен GET запрос на '/', отвечаю OK.")
    return "Я саркастичный бот, и я все еще жив (наверное). Иди нахуй из браузера, пиши в Telegram."

async def run_bot_async(application: Application) -> None:
    """Асинхронная функция для запуска и корректной остановки бота."""
    try:
        logger.info("Инициализация Telegram Application...")
        await application.initialize() # Инициализируем
        if not application.updater:
             logger.critical("Updater не был создан в Application. Не могу запустить polling.")
             return
        logger.info("Запуск получения обновлений (start_polling)...")
        await application.updater.start_polling(allowed_updates=Update.ALL_TYPES) # Запускаем polling
        logger.info("Запуск диспетчера Application (start)...")
        await application.start() # Запускаем обработку апдейтов
        logger.info("Бот запущен и работает... (ожидание отмены или сигнала)")
        # --->>> Заменяем idle() на ожидание Future <<<---
        await asyncio.Future()
        logger.info("Ожидание Future завершилось (не должно было без отмены).")
    except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
        logger.info("Получен сигнал остановки (KeyboardInterrupt/SystemExit/CancelledError).")
    except Exception as e:
        logger.critical(f"КРИТИЧЕСКАЯ ОШИБКА в run_bot_async во время работы: {e}", exc_info=True)
    finally:
        logger.info("Начинаю процесс ОСТАНОВКИ бота в run_bot_async...")
        if application.running:
            logger.info("Остановка диспетчера Application (stop)...")
            await application.stop()
            logger.info("Диспетчер Application остановлен.")
        if application.updater and application.updater.is_running:
            logger.info("Остановка получения обновлений (updater.stop)...")
            # --->>> Заменяем stop_polling() -> stop() <<<---
            await application.updater.stop()
            logger.info("Получение обновлений (updater) остановлено.")
        logger.info("Завершение работы Application (shutdown)...")
        await application.shutdown()
        logger.info("Процесс остановки бота в run_bot_async завершен.")

# --- Новая функция-обработчик для текстовых команд анализа ---
async def handle_text_analyze_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Эта функция будет вызываться по регулярному выражению
    # Просто вызываем нашу основную функцию анализа чата
    logger.info(f"Получена текстовая команда на анализ от {update.message.from_user.first_name}")
    await analyze_chat(update, context)

# --- Новая функция-обработчик для текстовых команд анализа картинки ---
async def handle_text_analyze_pic_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Эта функция будет вызываться по регулярному выражению в ответе на картинку
    # Просто вызываем нашу основную функцию анализа картинки
    # Важно: эта функция должна быть вызвана В ОТВЕТ на сообщение с картинкой!
    if not update.message.reply_to_message or not update.message.reply_to_message.photo:
         # Мы не будем спамить в чат, если вызвали не так, основная функция сама разберется
         logger.warning("handle_text_analyze_pic_command вызвана не как ответ на фото.")
         # Можно добавить ответ юзеру, что он долбоеб, если очень хочется
         # await update.message.reply_text("Ответь этой фразой на картинку, баклан!")
         # return
    logger.info(f"Получена текстовая команда на анализ картинки от {update.message.from_user.first_name}")
    await analyze_pic(update, context) # Вызываем заглушку для Groq или рабочую для Gemini

    # --- ФУНКЦИЯ ДЛЯ КОМАНДЫ /help ---
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отправляет сообщение со справкой о возможностях бота."""
    user_name = update.message.from_user.first_name or "забывчивый ты мой"
    logger.info(f"Пользователь '{user_name}' запросил справку (/help)")

    # Формируем текст справки. Используем f-string, чтобы подставить MAX_MESSAGES_TO_ANALYZE
    help_text = f"""
🗿 Слышь, {user_name}! Я Попиздяка, главный токсик и тролль этого чата. Вот че я могу (если не сплю или не тупит API):

*Анализ чата:*
Напиши `/analyze` или что-то типа "`Попиздяка анализируй`" / "`Бот обосри чат`".
Я прочитаю последние **{MAX_MESSAGES_TO_ANALYZE}** сообщений (включая текстовые заглушки для фото/стикеров) и выдам свой охуенно ценный вердикт по 1-10 моментам, с именами героев (если смогу разобрать).

*Анализ картинок:*
Ответь на картинку командой `/analyze_pic` или фразой типа "`Попиздяка зацени пикчу`" / "`Бот опиши это`".
Я попробую ее обосрать в своем неповторимом стиле (но это на Gemini, так что могу затупить или отказаться смотреть).

*Эта справка:*
Напиши `/help` или спроси "`Попиздяка кто ты?`" / "`Бот что умеешь?`".

*Важно:*
- Я читаю сообщения только ПОСЛЕ того, как меня добавили в чат.
- Чтобы я видел ВЕСЬ ваш пиздеж для анализа, дайте мне **админку**. Без нее я слепой.
- Иногда я могу нести хуйню - я работаю на нейросетях, они тупые.

Короче, командуй, но не злоупотребляй. 🗿
    """
    # Отправляем как обычное сообщение, а не ответ
    await context.bot.send_message(chat_id=update.message.chat_id, text=help_text.strip()) # Используем strip() для удаления лишних пробелов/переносов по краям

# --- КОНЕЦ ФУНКЦИИ /help ---

# --- ФУНКЦИЯ ДЛЯ КОМАНДЫ /retry (С ЧТЕНИЕМ ИЗ MONGODB) ---
async def retry_analysis(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Повторяет последний анализ (текста или картинки), читая данные из MongoDB."""
    if not update.message or not update.message.reply_to_message:
        await context.bot.send_message(chat_id=update.message.chat_id, text="Надо ответить этой командой на тот МОЙ высер, который ты хочешь переделать.")
        return

    chat_id = update.message.chat_id
    user_command_message_id = update.message.message_id
    replied_message_id = update.message.reply_to_message.message_id
    replied_message_user_id = update.message.reply_to_message.from_user.id
    bot_id = context.bot.id

    logger.info(f"Пользователь {update.message.from_user.first_name} запросил /retry в чате {chat_id}, отвечая на сообщение {replied_message_id}")

    if replied_message_user_id != bot_id:
        logger.warning("Команда /retry вызвана не в ответ на сообщение бота.")
        await context.bot.send_message(chat_id=chat_id, text="Эээ, ты ответил не на МОЕ сообщение.")
        try: await context.bot.delete_message(chat_id=chat_id, message_id=user_command_message_id)
        except Exception: pass
        return

    last_reply_data = None
    try:
        loop = asyncio.get_running_loop()
        last_reply_data = await loop.run_in_executor(None, lambda: last_reply_collection.find_one({"chat_id": chat_id}))
    except Exception as e:
        logger.error(f"Ошибка чтения /retry из MongoDB для чата {chat_id}: {e}", exc_info=True)
        await context.bot.send_message(chat_id=chat_id, text="Бля, не смог залезть в свою память (БД).")
        try: await context.bot.delete_message(chat_id=chat_id, message_id=user_command_message_id)
        except Exception: pass
        return

    if not last_reply_data or last_reply_data.get("message_id") != replied_message_id:
        logger.warning(f"Не найдена запись /retry для чата {chat_id} или ID ({replied_message_id}) не совпадает ({last_reply_data.get('message_id') if last_reply_data else 'None'}).")
        await context.bot.send_message(chat_id=chat_id, text="Не помню свой последний высер или ты ответил не на тот. Не могу переделать.")
        try: await context.bot.delete_message(chat_id=chat_id, message_id=user_command_message_id)
        except Exception: pass
        return

    analysis_type_to_retry = last_reply_data.get("analysis_type")
    source_file_id_to_retry = last_reply_data.get("source_file_id")

    logger.info(f"Повторяем анализ типа '{analysis_type_to_retry}' для чата {chat_id}...")

    try: # Удаляем старое перед новым анализом
        await context.bot.delete_message(chat_id=chat_id, message_id=replied_message_id)
        logger.info(f"Удален старый ответ бота {replied_message_id}")
        await context.bot.delete_message(chat_id=chat_id, message_id=user_command_message_id)
        logger.info(f"Удалена команда /retry {user_command_message_id}")
    except Exception as e:
        logger.error(f"Ошибка при удалении старых сообщений в /retry: {e}")
        await context.bot.send_message(chat_id=chat_id, text="Бля, не смог удалить старое, но попробую переделать.")

    # Создаем фейковый update ТОЛЬКО для передачи chat_id и user
    # НЕ ИСПОЛЬЗУЕМ context.bot_data/chat_data - это ненадежно
    # Передадим file_id напрямую в analyze_pic, если нужно

    if analysis_type_to_retry == 'text':
        # Создаем фейковый апдейт для analyze_chat
        fake_update_obj = type('obj', (object,), {
            'message': type('obj', (object,), {
                'chat_id': chat_id, 'from_user': update.message.from_user, 'reply_to_message': None
            })
        })
        await analyze_chat(Update.de_json(fake_update_obj.__dict__, context.bot), context) # ??? А может проще так? НЕТ, так нельзя
        # --->>> ПРАВИЛЬНЫЙ СПОСОБ ВЫЗВАТЬ analyze_chat без реального апдейта - вызвать его логику напрямую
        # Но для простоты пока оставим вызов через фейк-апдейт, хотя это не идеально
        # TODO: Переделать вызов analyze_chat/analyze_pic на передачу аргументов вместо апдейта
        await analyze_chat(Update.de_json({'message': {'chat': {'id': chat_id}, 'from_user': update.message.from_user.to_dict()}}, context.bot), context)


    elif analysis_type_to_retry == 'pic' and source_file_id_to_retry:
         # Вызываем analyze_pic, передав ей file_id как-то иначе
         # Самый простой костыль - все еще через context.bot_data, но можно и аргументом, если переделать analyze_pic
         # ОСТАВИМ КОСТЫЛЬ С bot_data пока что для простоты:
         context.bot_data[f'retry_pic_{chat_id}'] = source_file_id_to_retry
         logger.debug(f"Передаем file_id {source_file_id_to_retry} для /retry pic через context.bot_data")
         await analyze_pic(Update.de_json({'message': {'chat': {'id': chat_id}, 'from_user': update.message.from_user.to_dict(), 'reply_to_message': None }}, context.bot), context)
         # Очистка после вызова analyze_pic (она сама должна его использовать и он больше не нужен)
         context.bot_data.pop(f'retry_pic_{chat_id}', None)

    else:
        logger.error(f"Неизвестный/неполный тип анализа для /retry: {analysis_type_to_retry}, file_id: {source_file_id_to_retry}")
        await context.bot.send_message(chat_id=chat_id, text="Хуй пойми, что я там анализировал. Не могу повторить.")

# --- КОНЕЦ ФУНКЦИИ /retry ---

async def main() -> None:
    """Основная асинхронная функция, запускающая веб-сервер и бота."""
    logger.info("Запуск асинхронной функции main().")

    # 1. Настраиваем и собираем Telegram бота
    logger.info("Сборка Telegram Application...")
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CommandHandler("analyze", analyze_chat))
    application.add_handler(CommandHandler("analyze_pic", analyze_pic)) # Оставим рабочую версию с Gemini

    # --->>> ДОБАВЛЯЕМ HELP <<<---
    application.add_handler(CommandHandler("help", help_command))

    # --->>> ДОБАВЛЯЕМ RETRY <<<---
    application.add_handler(CommandHandler("retry", retry_analysis)) # Команда /retry
    retry_pattern = r'(?i).*(попиздяка|бот).*(переделай|повтори|перепиши|хуйня|другой вариант).*'
    # Важно: ловим только как ОТВЕТ на сообщение!
    application.add_handler(MessageHandler(filters.Regex(retry_pattern) & filters.TEXT & filters.REPLY & ~filters.COMMAND, retry_analysis))
    # --->>> КОНЕЦ ДОБАВЛЕНИЙ ДЛЯ RETRY <<<---


    # Regex для русских команд "/analyze"
    analyze_pattern = r'(?i).*(попиздяка|попиздоний|бот).*(анализируй|проанализируй|комментируй|обосри|скажи|мнение).*'
    application.add_handler(MessageHandler(filters.Regex(analyze_pattern) & filters.TEXT & ~filters.COMMAND, handle_text_analyze_command))

    # Regex для русских команд "/analyze_pic"
    analyze_pic_pattern = r'(?i).*(попиздяка|попиздоний|бот).*(зацени|опиши|обосри|скажи про).*(пикч|картинк|фот|изображен|это).*'
    application.add_handler(MessageHandler(filters.Regex(analyze_pic_pattern) & filters.TEXT & filters.REPLY & ~filters.COMMAND, handle_text_analyze_pic_command))

    # --->>> ДОБАВЛЯЕМ Regex ДЛЯ РУССКИХ КОМАНД "/help" <<<---
    help_pattern = r'(?i).*(попиздяка|попиздоний|бот).*(ты кто|кто ты|что умеешь|хелп|помощь|справка|команды).*'
    application.add_handler(MessageHandler(filters.Regex(help_pattern) & filters.TEXT & ~filters.COMMAND, help_command)) # Вызываем ту же функцию help_command

    # --->>> КОНЕЦ ДОБАВЛЕНИЙ ДЛЯ HELP <<<---


    # --->>> ПРАВИЛЬНЫЕ ОТДЕЛЬНЫЕ ОБРАБОТЧИКИ ДЛЯ store_message <<<---
    # 1. Только для ТЕКСТА (без команд)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, store_message))
    # 2. Только для ФОТО (используем объект filters.PHOTO)
    application.add_handler(MessageHandler(filters.PHOTO, store_message))
    # 3. Только для СТИКЕРОВ (используем объект filters.Sticker.ALL или просто filters.Sticker)
    application.add_handler(MessageHandler(filters.Sticker.ALL, store_message)) # Можно и filters.Sticker
    # --->>> КОНЕЦ ПРАВИЛЬНЫХ ОБРАБОТЧИКОВ <<<---
    logger.info("Обработчики Telegram добавлены.")

    # 2. Настраиваем Hypercorn для запуска Flask приложения
    port = int(os.environ.get("PORT", 8080)) # Render передает порт через $PORT
    hypercorn_config = hypercorn.config.Config()
    hypercorn_config.bind = [f"0.0.0.0:{port}"]
    hypercorn_config.worker_class = "asyncio" # Используем asyncio worker
    # Увеличим таймаут отключения, чтобы бот успел корректно остановиться
    hypercorn_config.shutdown_timeout = 60.0
    logger.info(f"Конфигурация Hypercorn: bind={hypercorn_config.bind}, worker={hypercorn_config.worker_class}, shutdown_timeout={hypercorn_config.shutdown_timeout}")

    # 3. Запускаем обе задачи (веб-сервер и бот) конкурентно в одном event loop
    logger.info("Создание и запуск конкурентных задач для Hypercorn и Telegram бота...")

    # Создаем задачи
    # Имя задачи полезно для логов
    bot_task = asyncio.create_task(run_bot_async(application), name="TelegramBotTask")
    # Hypercorn будет обслуживать Flask 'app'
    # Используем 'shutdown_trigger' Hypercorn чтобы он среагировал на сигнал остановки asyncio
    shutdown_event = asyncio.Event()
    server_task = asyncio.create_task(
        hypercorn_async_serve(app, hypercorn_config, shutdown_trigger=shutdown_event.wait),
        name="HypercornServerTask"
    )

    # Ожидаем завершения ЛЮБОЙ из задач. В норме они должны работать вечно.
    done, pending = await asyncio.wait(
        [bot_task, server_task], return_when=asyncio.FIRST_COMPLETED
    )

    logger.warning(f"Одна из основных задач завершилась! Done: {done}, Pending: {pending}")

    # Сигнализируем Hypercorn'у остановиться, если он еще работает
    if server_task in pending:
        logger.info("Сигнализируем Hypercorn серверу на остановку...")
        shutdown_event.set()

    # Пытаемся вежливо отменить и дождаться завершения оставшихся задач
    logger.info("Отменяем и ожидаем завершения оставшихся задач...")
    for task in pending:
        task.cancel()
    # Даем им шанс завершиться после отмены
    await asyncio.gather(*pending, return_exceptions=True)

    # Проверяем исключения в завершенных задачах
    for task in done:
        logger.info(f"Проверка завершенной задачи: {task.get_name()}")
        try:
            # Если в задаче было исключение, оно поднимется здесь
            await task
        except asyncio.CancelledError:
             logger.info(f"Задача {task.get_name()} была отменена.")
        except Exception as e:
            logger.error(f"Задача {task.get_name()} завершилась с ошибкой: {e}", exc_info=True)

    logger.info("Асинхронная функция main() завершила работу.")


# --- Точка входа в скрипт (ЗАПУСКАЕТ АСИНХРОННУЮ main) ---
if __name__ == "__main__":
    logger.info(f"Скрипт bot.py запущен как основной (__name__ == '__main__').")

    # Создаем .env шаблон, если надо (остается как было)
    if not os.path.exists('.env') and not os.getenv('RENDER'):
        logger.warning("Файл .env не найден...")
        try:
            with open('.env', 'w') as f:
                f.write(f"# Впиши сюда свои реальные ключи!\n")
                f.write(f"TELEGRAM_BOT_TOKEN=Бэбра\n")
                f.write(f"GEMINI_API_KEY=Бэбручо\n")
            logger.warning("Создан ШАБЛОН файла .env...")
        except Exception as e:
            logger.error(f"Не удалось создать шаблон .env файла: {e}")

    # Проверяем ключи (остается как было)
    if not TELEGRAM_BOT_TOKEN or not GEMINI_API_KEY:
        logger.critical("ОТСУТСТВУЮТ КЛЮЧИ TELEGRAM_BOT_TOKEN или GEMINI_API_KEY. Не могу запуститься.")
        exit(1)

    # Запускаем всю эту АСИНХРОННУЮ хуйню через asyncio.run()
    try:
        logger.info("Запускаю asyncio.run(main())...")
        # asyncio.run() автоматически обрабатывает Ctrl+C (SIGINT)
        asyncio.run(main())
        logger.info("asyncio.run(main()) завершен.")
    # Явный перехват KeyboardInterrupt больше не нужен, т.к. asyncio.run и idle() его обрабатывают
    # except KeyboardInterrupt:
    #     logger.info("Получен KeyboardInterrupt (Ctrl+C). Завершаю работу...")
    except Exception as e:
        # Ловим любые другие ошибки на самом верхнем уровне
        logger.critical(f"КРИТИЧЕСКАЯ ОШИБКА на верхнем уровне выполнения: {e}", exc_info=True)
        exit(1) # Выходим с кодом ошибки
    finally:
         logger.info("Скрипт bot.py завершает работу.")

# --- КОНЕЦ ПОЛНОГО КОДА BOT.PY (ВЕРСИЯ С ASYNCIO + HYPERCORN) ---