# --- НАЧАЛО ПОЛНОГО КОДА BOT.PY (AI.IO.NET ВЕРСИЯ - ФИНАЛ) ---
import logging
import os
import asyncio
import re
import datetime
import requests # Нужен для NewsAPI
import json # Для обработки ответа
import random
import base64
from collections import deque
from flask import Flask
import hypercorn.config
from hypercorn.asyncio import serve as hypercorn_async_serve
import signal
import pymongo
from pymongo.errors import ConnectionFailure

# Импорты для AI.IO.NET (OpenAI библиотека)
from openai import OpenAI, AsyncOpenAI, BadRequestError
import httpx

# Импорты Telegram
from telegram import Update, Bot, User
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, JobQueue
import telegram # --->>> ВОТ ЭТА СТРОКА НУЖНА <<<---

from dotenv import load_dotenv

# Загружаем секреты (.env для локального запуска)
load_dotenv()

# --- НАСТРОЙКИ ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
IO_NET_API_KEY = os.getenv("IO_NET_API_KEY")
MONGO_DB_URL = os.getenv("MONGO_DB_URL")
MAX_MESSAGES_TO_ANALYZE = 200 # Оптимальное значение
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0"))
if ADMIN_USER_ID == 0: logger.warning("ADMIN_USER_ID не задан!")

# --- НАСТРОЙКИ НОВОСТЕЙ (GNEWS) ---
GNEWS_API_KEY = os.getenv("GNEWS_API_KEY")
NEWS_COUNTRY = "ru" # Страна
NEWS_LANG = "ru"    # Язык новостей
NEWS_COUNT = 3      # Сколько новостей брать
NEWS_POST_INTERVAL = 60 * 60 * 6 # Интервал постинга (6 часов)
NEWS_JOB_NAME = "post_news_job"

if not GNEWS_API_KEY:
    logger.warning("GNEWS_API_KEY не найден! Новостная функция будет отключена.")


# Проверка ключей
if not TELEGRAM_BOT_TOKEN: raise ValueError("НЕ НАЙДЕН TELEGRAM_BOT_TOKEN!")
if not IO_NET_API_KEY: raise ValueError("НЕ НАЙДЕН IO_NET_API_KEY!")
if not MONGO_DB_URL: raise ValueError("НЕ НАЙДЕНА MONGO_DB_URL!")

# --- Логирование ---
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("hypercorn").setLevel(logging.INFO)
logging.getLogger("openai").setLevel(logging.WARNING)
logging.getLogger("pymongo").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# --- ПОДКЛЮЧЕНИЕ К MONGODB ATLAS ---
try:
    mongo_client = pymongo.MongoClient(MONGO_DB_URL, serverSelectionTimeoutMS=5000)
    mongo_client.admin.command('ping')
    logger.info("Успешное подключение к MongoDB Atlas!")
    db = mongo_client['popizdyaka_db']
    history_collection = db['message_history']
    last_reply_collection = db['last_replies']
    chat_activity_collection = db['chat_activity']
    chat_activity_collection.create_index("chat_id", unique=True)
    logger.info("Коллекции MongoDB готовы.")
    bot_status_collection = db['bot_status']
    logger.info("Коллекция bot_status готова.")
except Exception as e:
    logger.critical(f"ПИЗДЕЦ при настройке MongoDB: {e}", exc_info=True)
    raise SystemExit(f"Ошибка настройки MongoDB: {e}")

# --- НАСТРОЙКА КЛИЕНТА AI.IO.NET API ---
try:
    ionet_client = AsyncOpenAI(
        api_key=IO_NET_API_KEY,
        base_url="https://api.intelligence.io.solutions/api/v1/" # ПРОВЕРЕННЫЙ URL!
    )
    logger.info("Клиент AsyncOpenAI для ai.io.net API настроен.")
except Exception as e:
     logger.critical(f"ПИЗДЕЦ при настройке клиента ai.io.net: {e}", exc_info=True)
     raise SystemExit(f"Не удалось настроить клиента ai.io.net: {e}")

# --- ВЫБОР МОДЕЛЕЙ AI.IO.NET (ПРОВЕРЬ ДОСТУПНОСТЬ!) ---
IONET_TEXT_MODEL_ID = "mistralai/Mistral-Large-Instruct-2411" # Твоя модель для текста
IONET_VISION_MODEL_ID = "Qwen/Qwen2-VL-7B-Instruct" # Для картинок
logger.info(f"Текстовая модель ai.io.net: {IONET_TEXT_MODEL_ID}")
logger.info(f"Vision модель ai.io.net: {IONET_VISION_MODEL_ID}")

# --- Хранилище истории в памяти больше не нужно ---
logger.info(f"Максимальная длина истории для анализа из БД: {MAX_MESSAGES_TO_ANALYZE}")

# --- Вспомогательная функция для вызова текстового API ---
async def _call_ionet_api(messages: list, model_id: str, max_tokens: int, temperature: float) -> str | None:
    """Вызывает текстовый API ai.io.net и возвращает ответ или текст ошибки."""
    try:
        logger.info(f"Отправка запроса к ai.io.net API ({model_id})...")
        response = await ionet_client.chat.completions.create(
            model=model_id, messages=messages, max_tokens=max_tokens, temperature=temperature
        )
        logger.info(f"Получен ответ от {model_id}.")
        if response.choices and response.choices[0].message and response.choices[0].message.content:
            return response.choices[0].message.content.strip()
        else: logger.warning(f"Ответ от {model_id} пуст/некорректен: {response}"); return None
    except BadRequestError as e:
        logger.error(f"Ошибка BadRequest от ai.io.net API ({model_id}): {e.status_code} - {e.body}", exc_info=False) # Не пишем весь трейсбек
        error_detail = str(e.body or e)
        return f"🗿 API {model_id.split('/')[1].split('-')[0]} вернул ошибку: `{error_detail[:100]}`"
    except Exception as e:
        logger.error(f"ПИЗДЕЦ при вызове ai.io.net API ({model_id}): {e}", exc_info=True)
        return f"🗿 Ошибка API: `{type(e).__name__}`"
    
    ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0"))
    if ADMIN_USER_ID == 0: logger.warning("ADMIN_USER_ID не задан!")

# --->>> ВОТ ЭТИ ДВЕ ФУНКЦИИ НУЖНЫ ЗДЕСЬ <<<---
async def is_maintenance_mode(loop: asyncio.AbstractEventLoop) -> bool:
    """Проверяет в MongoDB, активен ли режим техработ."""
    try:
        status_doc = await loop.run_in_executor(None, lambda: bot_status_collection.find_one({"_id": "maintenance_status"}))
        return status_doc.get("active", False) if status_doc else False
    except Exception as e:
        logger.error(f"Ошибка чтения статуса техработ из MongoDB: {e}")
        return False

async def set_maintenance_mode(active: bool, loop: asyncio.AbstractEventLoop) -> bool:
    """Включает или выключает режим техработ в MongoDB."""
    try:
        await loop.run_in_executor(None, lambda: bot_status_collection.update_one({"_id": "maintenance_status"},{"$set": {"active": active, "updated_at": datetime.datetime.now(datetime.timezone.utc)} }, upsert=True))
        logger.info(f"Режим техработ {'ВКЛЮЧЕН' if active else 'ВЫКЛЮЧЕН'}.")
        return True
    except Exception as e:
        logger.error(f"Ошибка записи статуса техработ в MongoDB: {e}")
        return False
# --->>> КОНЕЦ ФУНКЦИЙ ДЛЯ ТЕХРАБОТ <<<---

# --- ОБРАБОТЧИК СООБЩЕНИЙ (ЗАПИСЬ В БД) ---
async def store_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Сохраняет текст/заглушки в history_collection и обновляет chat_activity_collection
    if not update.message or not update.message.from_user: return
    message_text = None; chat_id = update.message.chat_id; user_name = update.message.from_user.first_name or "Анон"; timestamp = update.message.date or datetime.datetime.now(datetime.timezone.utc)
    if update.message.text: message_text = update.message.text
    elif update.message.photo: file_id = update.message.photo[-1].file_id; message_text = f"[КАРТИНКА:{file_id}]"
    elif update.message.sticker: emoji = update.message.sticker.emoji or ''; message_text = f"[СТИКЕР {emoji}]"
    elif update.message.video: message_text = "[ОТПРАВИЛ(А) ВИДЕО]"
    elif update.message.voice: message_text = "[ОТПРАВИЛ(А) ГОЛОСОВОЕ]"
    if message_text:
        message_doc = {"chat_id": chat_id, "user_name": user_name, "text": message_text, "timestamp": timestamp, "message_id": update.message.message_id}
        activity_update_doc = {"$set": {"last_message_time": timestamp}, "$setOnInsert": {"last_bot_shitpost_time": datetime.datetime.fromtimestamp(0, datetime.timezone.utc), "chat_id": chat_id}}
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, lambda: history_collection.insert_one(message_doc))
            await loop.run_in_executor(None, lambda: chat_activity_collection.update_one({"chat_id": chat_id}, activity_update_doc, upsert=True))
        except Exception as e: logger.error(f"Ошибка записи в MongoDB чата {chat_id}: {e}", exc_info=True)

import re # Убедись, что есть этот импорт в начале bot.py
# Другие нужные импорты (Update, User, ContextTypes, pymongo, asyncio, datetime, logger, _call_ionet_api, IONET_TEXT_MODEL_ID, MAX_MESSAGES_TO_ANALYZE, history_collection, last_reply_collection)

# --- ПОЛНАЯ ФУНКЦИЯ analyze_chat (С УЛУЧШЕННЫМ УДАЛЕНИЕМ <think>) ---
async def analyze_chat(update: Update | None, context: ContextTypes.DEFAULT_TYPE, direct_chat_id: int | None = None, direct_user: User | None = None) -> None:
     # --->>> НАЧАЛО НОВОЙ ПРОВЕРКИ ТЕХРАБОТ <<<---
# Проверяем наличие update и message - без них проверка невозможна
    if not update or not update.message or not update.message.from_user or not update.message.chat:
        logger.warning(f"Не могу проверить техработы - нет данных в update ({__name__})") # Логгируем имя текущей функции
        # Если это важная команда, можно тут вернуть ошибку пользователю
        # await context.bot.send_message(chat_id=update.effective_chat.id, text="Ошибка проверки данных.")
        return # Или просто выйти

    real_chat_id = update.message.chat.id
    real_user_id = update.message.from_user.id
    real_chat_type = update.message.chat.type

    loop = asyncio.get_running_loop()
    maintenance_active = await is_maintenance_mode(loop) # Вызываем функцию проверки

    # Блокируем, если техработы ВКЛЮЧЕНЫ и это НЕ админ в ЛС
    if maintenance_active and (real_user_id != ADMIN_USER_ID or real_chat_type != 'private'):
        logger.info(f"Команда отклонена из-за режима техработ в чате {real_chat_id}")
        try: # Пытаемся ответить и удалить команду
            await context.bot.send_message(chat_id=real_chat_id, text="🔧 Сорян, у меня сейчас технические работы. Попробуй позже.")
            await context.bot.delete_message(chat_id=real_chat_id, message_id=update.message.message_id)
        except Exception as e:
            logger.warning(f"Не удалось ответить/удалить сообщение о техработах: {e}")
        return # ВЫХОДИМ ИЗ ФУНКЦИИ
# --->>> КОНЕЦ НОВОЙ ПРОВЕРКИ ТЕХРАБОТ <<<---
    # Получаем chat_id и user либо из Update, либо из прямых аргументов
    if update and update.message:
        chat_id = update.message.chat_id
        user = update.message.from_user
        user_name = user.first_name if user else "Хуй Пойми Кто"
    elif direct_chat_id and direct_user:
        chat_id = direct_chat_id
        user = direct_user
        user_name = user.first_name or "Переделкин" # Имя для retry
    else:
        logger.error("analyze_chat вызвана некорректно!")
        return

    logger.info(f"Пользователь '{user_name}' запросил анализ текста в чате {chat_id} через {IONET_TEXT_MODEL_ID}")

    # --- ЧТЕНИЕ ИСТОРИИ ИЗ MONGODB ---
    messages_from_db = []
    try:
        logger.debug(f"Запрос истории для чата {chat_id} из MongoDB...")
        limit = MAX_MESSAGES_TO_ANALYZE
        query = {"chat_id": chat_id}
        sort_order = [("timestamp", pymongo.DESCENDING)]
        loop = asyncio.get_running_loop()
        history_cursor = await loop.run_in_executor(
            None, lambda: history_collection.find(query).sort(sort_order).limit(limit)
        )
        messages_from_db = list(history_cursor)[::-1] # Переворачиваем
        history_len = len(messages_from_db)
        logger.info(f"Из MongoDB для чата {chat_id} загружено {history_len} сообщений.")
    except Exception as e:
        logger.error(f"Ошибка чтения истории MongoDB: {e}")
        await context.bot.send_message(chat_id=chat_id, text="Бля, не смог прочитать историю из БД.")
        return

    # Проверяем, достаточно ли сообщений
    min_msgs = 10
    if history_len < min_msgs:
        logger.info(f"В чате {chat_id} слишком мало сообщений в БД ({history_len}/{min_msgs}).")
        await context.bot.send_message(chat_id=chat_id, text=f"Слышь, {user_name}, надо {min_msgs} сообщений, а в БД {history_len}.")
        return

    # Формируем текст для ИИ
    conversation_lines = [f"{msg.get('user_name', '?')}: {msg.get('text', '')}" for msg in messages_from_db]
    conversation_text = "\n".join(conversation_lines)
    logger.info(f"Начинаю анализ {len(messages_from_db)} сообщений через {IONET_TEXT_MODEL_ID}...")

    # Вызов ИИ
    try:
        # Промпт (оставляем тот, что с сутью и панчлайном, но с запретом мета)
        system_prompt = (
            f"Ты - въедливый и язвительный сплетник-летописец Telegram-чата. Проанализируй диалог ниже и выдели 1-5 самых интересных/тупых момента, УКАЗАВ КТО (по именам/никам) что сказал/сделал. "
            f"Для каждого момента: СНАЧАЛА кратко опиши суть (1 предложение), ПОТОМ добавь КОРОТКИЙ (3-7 слов) саркастичный МАТЕРНЫЙ панчлайн. "
            f"Начинай каждый блок с '🗿 '. Если ничего нет - напиши '🗿 Унылое болото.'.\n"
            f"ВАЖНО: НЕ пиши никаких вступлений, объяснений, рассуждений о задании или тегов типа <think>. СРАЗУ ПИШИ ТОЛЬКО РЕЗУЛЬТАТ АНАЛИЗА в указанном формате (🗿 Суть. - Панчлайн.).\n\n" # Усилили запрет
            f"Пример результата:\n"
            f"🗿 Васян доказывал Пете преимущества диеты на воде.\n— Пиздец гений.\n" # Пример с переносом строки для панчлайна
            f"🗿 Катя и Лена обсуждали цвет трусов.\n— Высокие материи, блядь.\n\n"
            f"Вот диалог для анализа:"
        )
        messages_for_api = [
            {"role": "system", "content": system_prompt},
            # Передаем сам диалог как сообщение пользователя
            {"role": "user", "content": f"Проанализируй этот диалог:\n```\n{conversation_text}\n```"}
        ]

        thinking_message = await context.bot.send_message(chat_id=chat_id, text=f"Так, блядь, щас подключу мозги {IONET_TEXT_MODEL_ID.split('/')[1].split('-')[0]}...")

        # Вызываем вспомогательную функцию
        sarcastic_summary = await _call_ionet_api(messages_for_api, IONET_TEXT_MODEL_ID, 350, 0.7) or "[Модель промолчала]"

        # --->>> УЛУЧШЕННОЕ УДАЛЕНИЕ <think> ТЕГОВ <<<---
        # Компилируем регулярку один раз для эффективности (хотя тут не критично)
        think_pattern = re.compile(r"^\s*<think>.*?</think>\s*", re.DOTALL | re.IGNORECASE)
        if sarcastic_summary and think_pattern.search(sarcastic_summary):
            logger.info("Обнаружены теги <think>, удаляем...")
            # Заменяем найденное на пустую строку и убираем лишние пробелы по краям
            sarcastic_summary = think_pattern.sub("", sarcastic_summary).strip()
            logger.info(f"Текст после удаления <think>: '{sarcastic_summary[:50]}...'")
        # --->>> КОНЕЦ УЛУЧШЕНИЯ <<<---

        # Добавляем Моаи, если его нет и это не ошибка
        if not sarcastic_summary.startswith("🗿") and not sarcastic_summary.startswith("["):
            sarcastic_summary = "🗿 " + sarcastic_summary

        # Удаляем "Думаю..."
        try: await context.bot.delete_message(chat_id=chat_id, message_id=thinking_message.message_id)
        except Exception: pass

        # Страховочная обрезка и отправка
        MAX_MESSAGE_LENGTH = 4096;
        if len(sarcastic_summary) > MAX_MESSAGE_LENGTH: sarcastic_summary = sarcastic_summary[:MAX_MESSAGE_LENGTH - 3] + "..."
        sent_message = await context.bot.send_message(chat_id=chat_id, text=sarcastic_summary)
        logger.info(f"Отправил результат анализа ai.io.net '{sarcastic_summary[:50]}...'")

        # Запись для /retry
        if sent_message:
             reply_doc = { "chat_id": chat_id, "message_id": sent_message.message_id, "analysis_type": "text", "timestamp": datetime.datetime.now(datetime.timezone.utc) }
             try:
                 loop = asyncio.get_running_loop(); await loop.run_in_executor(None, lambda: last_reply_collection.update_one({"chat_id": chat_id}, {"$set": reply_doc}, upsert=True))
                 logger.debug(f"Сохранен/обновлен ID ({sent_message.message_id}, text) для /retry чата {chat_id}.")
             except Exception as e: logger.error(f"Ошибка записи /retry (text) в MongoDB: {e}")

    except Exception as e: # Общая ошибка самого analyze_chat
        logger.error(f"ПИЗДЕЦ в analyze_chat (после чтения БД): {e}", exc_info=True)
        try:
            if 'thinking_message' in locals(): await context.bot.delete_message(chat_id=chat_id, message_id=thinking_message.message_id)
        except Exception: pass
        await context.bot.send_message(chat_id=chat_id, text=f"Бля, {user_name}, я обосрался при анализе чата. Ошибка: `{type(e).__name__}`.")

# --- КОНЕЦ ПОЛНОЙ ФУНКЦИИ analyze_chat ---

# --- ОБРАБОТЧИК КОМАНДЫ /analyze_pic (ПЕРЕПИСАН ПОД VISION МОДЕЛЬ) ---
async def analyze_pic(update: Update | None, context: ContextTypes.DEFAULT_TYPE, direct_chat_id: int | None = None, direct_user: User | None = None, direct_file_id: str | None = None) -> None:
     # --->>> НАЧАЛО НОВОЙ ПРОВЕРКИ ТЕХРАБОТ <<<---
# Проверяем наличие update и message - без них проверка невозможна
    if not update or not update.message or not update.message.from_user or not update.message.chat:
        logger.warning(f"Не могу проверить техработы - нет данных в update ({__name__})") # Логгируем имя текущей функции
        # Если это важная команда, можно тут вернуть ошибку пользователю
        # await context.bot.send_message(chat_id=update.effective_chat.id, text="Ошибка проверки данных.")
        return # Или просто выйти

    real_chat_id = update.message.chat.id
    real_user_id = update.message.from_user.id
    real_chat_type = update.message.chat.type

    loop = asyncio.get_running_loop()
    maintenance_active = await is_maintenance_mode(loop) # Вызываем функцию проверки

    # Блокируем, если техработы ВКЛЮЧЕНЫ и это НЕ админ в ЛС
    if maintenance_active and (real_user_id != ADMIN_USER_ID or real_chat_type != 'private'):
        logger.info(f"Команда отклонена из-за режима техработ в чате {real_chat_id}")
        try: # Пытаемся ответить и удалить команду
            await context.bot.send_message(chat_id=real_chat_id, text="🔧 Сорян, у меня сейчас технические работы. Попробуй позже.")
            await context.bot.delete_message(chat_id=real_chat_id, message_id=update.message.message_id)
        except Exception as e:
            logger.warning(f"Не удалось ответить/удалить сообщение о техработах: {e}")
        return # ВЫХОДИМ ИЗ ФУНКЦИИ
# --->>> КОНЕЦ НОВОЙ ПРОВЕРКИ ТЕХРАБОТ <<<---
    # Получаем chat_id, user, user_name, image_file_id (из update или аргументов)
    image_file_id = None; chat_id = None; user = None; user_name = "Фотограф хуев"
    retry_key = f'retry_pic_{direct_chat_id or (update.message.chat_id if update and update.message else None)}'
    if direct_chat_id and direct_user and direct_file_id: # Вызов из retry
        chat_id = direct_chat_id; user = direct_user; image_file_id = direct_file_id
        user_name = user.first_name if user else user_name
        logger.info(f"Получен file_id {image_file_id} напрямую для /retry.")
        context.bot_data.pop(retry_key, None) # Очищаем сразу
    elif update and update.message and update.message.reply_to_message and update.message.reply_to_message.photo: # Обычный вызов
        chat_id = update.message.chat_id; user = update.message.from_user
        user_name = user.first_name if user else user_name
        reply_msg = update.message.reply_to_message; photo_large = reply_msg.photo[-1]; image_file_id = photo_large.file_id
        logger.info(f"Получен file_id {image_file_id} из reply_to_message.")
    else:
        error_chat_id = update.message.chat_id if update and update.message else None
        if error_chat_id: await context.bot.send_message(chat_id=error_chat_id, text="Ответь этой командой на КАРТИНКУ!")
        else: logger.error("analyze_pic вызвана некорректно!")
        return
    if not image_file_id: logger.error("Не удалось получить file_id!"); await context.bot.send_message(chat_id=chat_id, text="Не нашел ID картинки."); return
    logger.info(f"Пользователь '{user_name}' запросил анализ картинки (ID: {image_file_id}) через {IONET_VISION_MODEL_ID}")
    try: # Скачивание и вызов ИИ
        logger.info(f"Скачивание картинки {image_file_id}...")
        photo_file = await context.bot.get_file(image_file_id, read_timeout=60)
        photo_bytes_io = await photo_file.download_as_bytearray(read_timeout=60)
        photo_bytes = bytes(photo_bytes_io)
        if not photo_bytes: raise ValueError("Скачаны пустые байты картинки")
        logger.info(f"Картинка скачана, размер: {len(photo_bytes)} байт.")
        image_prompt_text = (
            f"Ты - стендап-комик с ОЧЕНЬ черным юмором, циник и мастер саркастических замечаний. Тебе показали КАРТИНКУ. Забудь про техническое качество фото. Сосредоточься на **СОДЕРЖАНИИ**: что за нелепая, тупая, смешная или просто странная хуйня там происходит? "
            f"Твоя задача: Выдай **КОРОТКИЙ (1-3 предложения), УБИЙСТВЕННО СМЕШНОЙ и ЕДКИЙ** комментарий к сюжету/объектам на картинке. Используй черный юмор, абсурд, неожиданные сравнения и **МАТ** для комического эффекта. Высмеивай происходящее.\n\n"
            f"ВАЖНО: Будь дерзким и злым, но остроумным. НЕ РАССУЖДАЙ о задании. НЕ пиши вступлений. СРАЗУ начинай ответ с `🗿 `.\n\n"
            f"Пример (кот в коробке): '🗿 Бля, кошак косплеит Диогена? Или просто готовится к отправке на живодерню? Выглядит решительно.'\n"
            f"Пример (пикник): '🗿 О, человеки вывезли свои жирные жопы пожрать на травке. Наверное, обсуждают смысл бытия между закидыванием мазика и пивасика.'\n"
            f"Пример (смешная собака): '🗿 Это что за генетический выродок? Помесь мопса с Чужим? Его бы на опыты сдать, а не фоткать.'\n"
            f"Пример (еда): '🗿 Фу, блядь, кто-то сфоткал остатки вчерашнего ужина? Или это уже переваренное? Выглядит одинаково хуево.'\n\n"
            f"Твой ЧЕРНО-ЮМОРНОЙ и САРКАСТИЧНЫЙ комментарий к приложенной картинке (НАЧИНАЙ С 🗿):"
        )
        # --->>> КОНЕЦ НОВОГО ПРОМПТА <<<---

        base64_image = base64.b64encode(photo_bytes).decode('utf-8')
        messages_for_api = [{"role": "user","content": [ {"type": "text", "text": image_prompt_text}, {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}} ]}]

        thinking_message = await context.bot.send_message(chat_id=chat_id, text=f"Так-так, блядь, ща посмотрим ({IONET_VISION_MODEL_ID.split('/')[0]} видит!)...") # Заменили имя модели
        sarcastic_comment = await _call_ionet_api(messages_for_api, IONET_VISION_MODEL_ID, 300, 0.75) or "[Попиздяка промолчал]" # Уменьшили max_tokens и температуру
        if not sarcastic_comment.startswith("🗿") and not sarcastic_comment.startswith("["): sarcastic_comment = "🗿 " + sarcastic_comment
        try: await context.bot.delete_message(chat_id=chat_id, message_id=thinking_message.message_id)
        except Exception: pass

        MAX_MESSAGE_LENGTH = 4096;
        if len(sarcastic_comment) > MAX_MESSAGE_LENGTH: sarcastic_comment = sarcastic_comment[:MAX_MESSAGE_LENGTH - 3] + "..."

        sent_message = await context.bot.send_message(chat_id=chat_id, text=sarcastic_comment)
        logger.info(f"Отправлен коммент к картинке ai.io.net '{sarcastic_comment[:50]}...'")
        if sent_message: # Запись для /retry
             reply_doc = {"chat_id": chat_id, "message_id": sent_message.message_id, "analysis_type": "pic", "source_file_id": image_file_id, "timestamp": datetime.datetime.now(datetime.timezone.utc)}
             try: loop = asyncio.get_running_loop(); await loop.run_in_executor(None, lambda: last_reply_collection.update_one({"chat_id": chat_id}, {"$set": reply_doc}, upsert=True))
             except Exception as e: logger.error(f"Ошибка записи /retry (pic) в MongoDB: {e}")
    except Exception as e: # Общая ошибка
        logger.error(f"ПИЗДЕЦ в analyze_pic: {e}", exc_info=True)
        try:
            if 'thinking_message' in locals(): await context.bot.delete_message(chat_id=chat_id, message_id=thinking_message.message_id)
        except Exception: pass
        await context.bot.send_message(chat_id=chat_id, text=f"Бля, {user_name}, я обосрался при анализе картинки. Ошибка: `{type(e).__name__}`.")

# --- ОСТАЛЬНЫЕ ФУНКЦИИ С ВЫЗОВОМ ИИ (ПЕРЕПИСАНЫ) ---

# --- ПОЛНАЯ ФУНКЦИЯ ДЛЯ КОМАНДЫ /retry (ВЕРСИЯ ДЛЯ БД, БЕЗ FAKE UPDATE) ---
async def retry_analysis(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
         # --->>> НАЧАЛО НОВОЙ ПРОВЕРКИ ТЕХРАБОТ <<<---
# Проверяем наличие update и message - без них проверка невозможна
    if not update or not update.message or not update.message.from_user or not update.message.chat:
        logger.warning(f"Не могу проверить техработы - нет данных в update ({__name__})") # Логгируем имя текущей функции
        # Если это важная команда, можно тут вернуть ошибку пользователю
        # await context.bot.send_message(chat_id=update.effective_chat.id, text="Ошибка проверки данных.")
        return # Или просто выйти

    real_chat_id = update.message.chat.id
    real_user_id = update.message.from_user.id
    real_chat_type = update.message.chat.type

    loop = asyncio.get_running_loop()
    maintenance_active = await is_maintenance_mode(loop) # Вызываем функцию проверки

    # Блокируем, если техработы ВКЛЮЧЕНЫ и это НЕ админ в ЛС
    if maintenance_active and (real_user_id != ADMIN_USER_ID or real_chat_type != 'private'):
        logger.info(f"Команда отклонена из-за режима техработ в чате {real_chat_id}")
        try: # Пытаемся ответить и удалить команду
            await context.bot.send_message(chat_id=real_chat_id, text="🔧 Сорян, у меня сейчас технические работы. Попробуй позже.")
            await context.bot.delete_message(chat_id=real_chat_id, message_id=update.message.message_id)
        except Exception as e:
            logger.warning(f"Не удалось ответить/удалить сообщение о техработах: {e}")
        return # ВЫХОДИМ ИЗ ФУНКЦИИ
# --->>> КОНЕЦ НОВОЙ ПРОВЕРКИ ТЕХРАБОТ <<<---
    """Повторяет последний анализ (текста, картинки, стиха и т.д.), читая данные из MongoDB и вызывая нужную функцию напрямую."""
    if not update.message or not update.message.reply_to_message:
        await context.bot.send_message(chat_id=update.message.chat_id, text="Надо ответить этой командой на тот МОЙ высер, который ты хочешь переделать.")
        return

    chat_id = update.message.chat_id
    user_command_message_id = update.message.message_id
    replied_message_id = update.message.reply_to_message.message_id
    replied_message_user_id = update.message.reply_to_message.from_user.id
    bot_id = context.bot.id
    user_who_requested_retry = update.message.from_user # Юзер, который вызвал /retry

    logger.info(f"Пользователь '{user_who_requested_retry.first_name or 'ХЗ кто'}' запросил /retry в чате {chat_id}, отвечая на сообщение {replied_message_id}")

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
        saved_id = last_reply_data.get("message_id") if last_reply_data else 'None'
        logger.warning(f"Не найдена запись /retry для чата {chat_id} или ID ({replied_message_id}) не совпадает ({saved_id}).")
        await context.bot.send_message(chat_id=chat_id, text="Не помню свой последний высер или ты ответил не на тот. Не могу переделать.")
        try: await context.bot.delete_message(chat_id=chat_id, message_id=user_command_message_id)
        except Exception: pass
        return

    analysis_type_to_retry = last_reply_data.get("analysis_type")
    source_file_id_to_retry = last_reply_data.get("source_file_id") # Для картинок
    target_name_to_retry = last_reply_data.get("target_name")       # Для стихов и роастов
    target_id_to_retry = last_reply_data.get("target_id")           # Для роастов
    gender_hint_to_retry = last_reply_data.get("gender_hint")       # Для роастов

    logger.info(f"Повторяем анализ типа '{analysis_type_to_retry}' для чата {chat_id}...")

    # Удаляем старые сообщения
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=replied_message_id)
        logger.info(f"Удален старый ответ бота {replied_message_id}")
        await context.bot.delete_message(chat_id=chat_id, message_id=user_command_message_id)
        logger.info(f"Удалена команда /retry {user_command_message_id}")
    except Exception as e:
        logger.error(f"Ошибка при удалении старых сообщений в /retry: {e}")
        await context.bot.send_message(chat_id=chat_id, text="Бля, не смог удалить старое, но попробую переделать.")

    # Вызываем нужную функцию анализа НАПРЯМУЮ
    try:
        if analysis_type_to_retry == 'text':
            logger.info("Вызов analyze_chat для /retry...")
            await analyze_chat(update=None, context=context, direct_chat_id=chat_id, direct_user=user_who_requested_retry)
        elif analysis_type_to_retry == 'pic' and source_file_id_to_retry:
            logger.info(f"Вызов analyze_pic для /retry с file_id {source_file_id_to_retry}...")
            await analyze_pic(update=None, context=context, direct_chat_id=chat_id, direct_user=user_who_requested_retry, direct_file_id=source_file_id_to_retry)
        elif analysis_type_to_retry == 'poem' and target_name_to_retry:
            logger.info(f"Вызов generate_poem для /retry для имени '{target_name_to_retry}'...")
            # Передаем имя через фейковый update - самый простой способ не менять generate_poem сильно
            fake_text = f"/poem {target_name_to_retry}"
            fake_msg = {'message_id': 1, 'date': int(datetime.datetime.now(datetime.timezone.utc).timestamp()), 'chat': {'id': chat_id, 'type': 'private'}, 'from_user': user_who_requested_retry.to_dict(), 'text': fake_text}
            fake_upd = Update.de_json({'update_id': 1, 'message': fake_msg}, context.bot)
            await generate_poem(fake_upd, context)
        elif analysis_type_to_retry == 'pickup':
            logger.info("Вызов get_pickup_line для /retry...")
            # Ему не нужны доп. данные, но нужен update для chat_id и user
            fake_msg = {'message_id': 1, 'date': int(datetime.datetime.now(datetime.timezone.utc).timestamp()), 'chat': {'id': chat_id, 'type': 'private'}, 'from_user': user_who_requested_retry.to_dict()}
            fake_upd = Update.de_json({'update_id': 1, 'message': fake_msg}, context.bot)
            await get_pickup_line(fake_upd, context)
        elif analysis_type_to_retry == 'roast' and target_name_to_retry and target_id_to_retry:
            logger.info(f"Вызов roast_user для /retry для '{target_name_to_retry}'...")
            # Передаем все напрямую
            await roast_user(update=None, context=context,
                             direct_chat_id=chat_id,
                             direct_user=user_who_requested_retry, # Кто ЗАКАЗАЛ повтор
                             # А вот target_user нам взять неоткуда без запроса к API или БД юзеров
                             # Поэтому передадим ЗАГЛУШКУ ДЛЯ ROAST RETRY
                             direct_gender_hint=gender_hint_to_retry or "неизвестен")
                             # Функция roast_user теперь должна уметь работать без target_user, если вызвано из retry
                             # Или мы пишем заглушку тут:
            await context.bot.send_message(chat_id=chat_id, text=f"🗿 Пережарка для **{target_name_to_retry}** пока не работает нормально. Хуй тебе.")
            # TODO: Реализовать нормальный retry для roast, если надо (например, убрать mention_html)

        # Добавь сюда elif для других типов анализа, если они появятся

        else:
            logger.error(f"Неизвестный/неполный тип анализа для /retry: {analysis_type_to_retry}")
            await context.bot.send_message(chat_id=chat_id, text="Хуй пойми, что я там делал. Не могу повторить.")
    except Exception as e:
         logger.error(f"Ошибка в /retry при вызове анализа ({analysis_type_to_retry}): {e}", exc_info=True)
         await context.bot.send_message(chat_id=chat_id, text=f"Обосрался при /retry: {type(e).__name__}")

# --- КОНЕЦ ПОЛНОЙ ФУНКЦИИ /retry ---

async def generate_poem(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
         # --->>> НАЧАЛО НОВОЙ ПРОВЕРКИ ТЕХРАБОТ <<<---
# Проверяем наличие update и message - без них проверка невозможна
    if not update or not update.message or not update.message.from_user or not update.message.chat:
        logger.warning(f"Не могу проверить техработы - нет данных в update ({__name__})") # Логгируем имя текущей функции
        # Если это важная команда, можно тут вернуть ошибку пользователю
        # await context.bot.send_message(chat_id=update.effective_chat.id, text="Ошибка проверки данных.")
        return # Или просто выйти

    real_chat_id = update.message.chat.id
    real_user_id = update.message.from_user.id
    real_chat_type = update.message.chat.type

    loop = asyncio.get_running_loop()
    maintenance_active = await is_maintenance_mode(loop) # Вызываем функцию проверки

    # Блокируем, если техработы ВКЛЮЧЕНЫ и это НЕ админ в ЛС
    if maintenance_active and (real_user_id != ADMIN_USER_ID or real_chat_type != 'private'):
        logger.info(f"Команда отклонена из-за режима техработ в чате {real_chat_id}")
        try: # Пытаемся ответить и удалить команду
            await context.bot.send_message(chat_id=real_chat_id, text="🔧 Сорян, у меня сейчас технические работы. Попробуй позже.")
            await context.bot.delete_message(chat_id=real_chat_id, message_id=update.message.message_id)
        except Exception as e:
            logger.warning(f"Не удалось ответить/удалить сообщение о техработах: {e}")
        return # ВЫХОДИМ ИЗ ФУНКЦИИ
# --->>> КОНЕЦ НОВОЙ ПРОВЕРКИ ТЕХРАБОТ <<<---
    """Генерирует саркастичный стишок про указанное имя."""
    # --->>> ЗАМЕНЯЕМ КОММЕНТАРИЙ НА РЕАЛЬНЫЙ КОД <<<---
    chat_id = None
    user = None
    target_name = None
    user_name = "Поэт хуев" # Дефолтное имя запросившего

    # Определяем chat_id и user из update (должен быть всегда, т.к. это обработчик)
    if update and update.message:
        chat_id = update.message.chat_id
        user = update.message.from_user
        if user:
            user_name = user.first_name or user_name # Обновляем имя запросившего, если есть

        # Определяем target_name из команды или текста
        if update.message.text:
            if update.message.text.startswith('/poem'):
                command_parts = update.message.text.split(maxsplit=1)
                if len(command_parts) >= 2 and command_parts[1].strip():
                    target_name = command_parts[1].strip()
            else: # Если не команда /poem, ищем в тексте
                match = re.search(r'(?i).*(?:стих|стишок|поэма)\s+(?:про|для|об)\s+([А-Яа-яЁё\s\-]+)', update.message.text)
                if match:
                    target_name = match.group(1).strip()

    # Проверяем, смогли ли мы получить нужные данные
    if not chat_id or not user:
        logger.error("Не удалось определить chat_id или user в generate_poem.")
        # Пытаемся ответить хоть куда-то, если есть ID
        err_chat_id = update.effective_chat.id if update and update.effective_chat else None
        if err_chat_id: await context.bot.send_message(chat_id=err_chat_id, text="Внутренняя ошибка получения данных.")
        return

    if not target_name:
        await context.bot.send_message(chat_id=chat_id, text="Не понял, про кого стих писать. `/poem Имя` или 'Бот стих про Имя'.")
        return

    if len(target_name) > 50:
        await context.bot.send_message(chat_id=chat_id, text="Слишком длинное имя.")
        return
    # --->>> КОНЕЦ РЕАЛЬНОГО КОДА ПОЛУЧЕНИЯ ПЕРЕМЕННЫХ <<<---

    logger.info(f"Пользователь '{user_name}' запросил стих про '{target_name}' в чате {chat_id}")
    poem_prompt = (
        f"Ты - пиздец какой циничный и злой поэт-матершинник. Твоя задача - НАПИСАТЬ КОРОТКИЙ (4-8 строк), смешной, саркастичный и ОБИДНЫЙ стишок про человека по имени **{target_name}**. "
        f"Используй черный юмор, мат, высмеивай стереотипы или просто придумывай нелепые ситуации с этим именем. Сделай так, чтобы было одновременно смешно и пиздец как токсично. Не бойся жести.\n\n"
        f"ВАЖНО: Стишок должен быть именно про имя '{target_name}'. НЕ пиши никаких вступлений или заключений. Только сам стих.\n\n"
        f"ВАЖНО: Стихотворение должно быть в рифму!\n"
        f"Пример (для Стаса):\n"
        f"Наш Стасик - парень неплохой,\n"
        f"Но вечно с кислой ебалой.\n"
        f"Он думает, что он философ,\n"
        f"А сам - как хуй что перед носом.\n\n"
        f"Пример (для Насти):\n"
        f"Ах, Настя, Настя, где твой мозг?\n"
        f"В башке лишь ветер, да навоз.\n"
        f"Мечтает Настя о Мальдивах,\n"
        f"Пока сосет хуй в перерывах.\n\n"
        f"Напиши ПОДОБНЫЙ стишок про **{target_name}**:"
    )
    try:
        thinking_message = await context.bot.send_message(chat_id=chat_id, text=f"Так, блядь, ща рифму подберу для '{target_name}'...")
        poem_text = await _call_ionet_api([{"role": "user", "content": poem_prompt}], IONET_TEXT_MODEL_ID, 150, 0.9) or f"[Стих про {target_name} не родился]"
        if not poem_text.startswith("🗿") and not poem_text.startswith("["): poem_text = "🗿 " + poem_text
        try: await context.bot.delete_message(chat_id=chat_id, message_id=thinking_message.message_id)
        except Exception: pass
        MAX_MESSAGE_LENGTH = 4096; # Обрезка
        if len(poem_text) > MAX_MESSAGE_LENGTH: poem_text = poem_text[:MAX_MESSAGE_LENGTH - 3] + "..."
        sent_message = await context.bot.send_message(chat_id=chat_id, text=poem_text)
        logger.info(f"Отправлен стих про {target_name}.")
        if sent_message: # Запись для /retry
            reply_doc = { "chat_id": chat_id, "message_id": sent_message.message_id, "analysis_type": "poem", "target_name": target_name, "timestamp": datetime.datetime.now(datetime.timezone.utc) }
            try: loop = asyncio.get_running_loop(); await loop.run_in_executor(None, lambda: last_reply_collection.update_one({"chat_id": chat_id}, {"$set": reply_doc}, upsert=True))
            except Exception as e: logger.error(f"Ошибка записи /retry (poem) в MongoDB: {e}")
    except Exception as e: logger.error(f"ПИЗДЕЦ при генерации стиха про {target_name}: {e}", exc_info=True); await context.bot.send_message(chat_id=chat_id, text=f"Бля, {user_name}, не могу сочинить про '{target_name}'. Ошибка: `{type(e).__name__}`.")

async def get_prediction(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
         # --->>> НАЧАЛО НОВОЙ ПРОВЕРКИ ТЕХРАБОТ <<<---
# Проверяем наличие update и message - без них проверка невозможна
    if not update or not update.message or not update.message.from_user or not update.message.chat:
        logger.warning(f"Не могу проверить техработы - нет данных в update ({__name__})") # Логгируем имя текущей функции
        # Если это важная команда, можно тут вернуть ошибку пользователю
        # await context.bot.send_message(chat_id=update.effective_chat.id, text="Ошибка проверки данных.")
        return # Или просто выйти

    real_chat_id = update.message.chat.id
    real_user_id = update.message.from_user.id
    real_chat_type = update.message.chat.type

    loop = asyncio.get_running_loop()
    maintenance_active = await is_maintenance_mode(loop) # Вызываем функцию проверки

    # Блокируем, если техработы ВКЛЮЧЕНЫ и это НЕ админ в ЛС
    if maintenance_active and (real_user_id != ADMIN_USER_ID or real_chat_type != 'private'):
        logger.info(f"Команда отклонена из-за режима техработ в чате {real_chat_id}")
        try: # Пытаемся ответить и удалить команду
            await context.bot.send_message(chat_id=real_chat_id, text="🔧 Сорян, у меня сейчас технические работы. Попробуй позже.")
            await context.bot.delete_message(chat_id=real_chat_id, message_id=update.message.message_id)
        except Exception as e:
            logger.warning(f"Не удалось ответить/удалить сообщение о техработах: {e}")
        return # ВЫХОДИМ ИЗ ФУНКЦИИ
# --->>> КОНЕЦ НОВОЙ ПРОВЕРКИ ТЕХРАБОТ <<<---
    if not update.message or not update.message.from_user: return
    chat_id = update.message.chat_id; user = update.message.from_user; user_name = user.first_name or "Любопытная Варвара"
    logger.info(f"Пользователь '{user_name}' запросил предсказание в чате {chat_id}")
    is_positive = random.random() < 0.01; prediction_prompt = ""; final_prefix = "🗿 "; thinking_text = f"🗿 Так, {user_name}, ща посмотрю в шар..."
    if is_positive: final_prefix = "✨ "; thinking_text = f"✨ Так, {user_name}, ща че-нить хорошее скажу..."; prediction_prompt = (f"Ты - внезапно подобревший... Выдай ОДНО ДОБРОЕ предсказание для {user_name}:")
    else: prediction_prompt = (
        f"Ты - ехидный и циничный оракул с черным юмором. Тебя попросили сделать предсказание для пользователя по имени {user_name}. "
        f"Придумай ОДНО КОРОТКОЕ (1-2 предложения), максимально саркастичное, матерное, обескураживающее или просто абсурдное предсказание на сегодня/ближайшее будущее. "
        f"Сделай его неожиданным и злым. Используй мат для усиления эффекта. Не пиши банальностей и позитива. НЕ ПИШИ никаких вступлений типа 'Я предсказываю...' или 'Для {user_name}...'. СРАЗУ выдавай само предсказание.\n\n"
        f"Примеры:\n"
        f"- Похоже, сегодня твой максимум - дойти до холодильника и обратно. Не перенапрягись, герой.\n"
        f"- Вселенная приготовила тебе сюрприз... пиздюлей, скорее всего.\n"
        f"- Звезды сошлись так, что тебе лучше бы сидеть тихо и не отсвечивать, а то прилетит.\n"
        f"- Твоя финансовая удача сегодня выглядит как дырка от бублика. Зато стабильно, блядь.\n"
        f"- Жди встречи со старым другом... который потребует вернуть долг.\n\n"
        f"Выдай ОДНО такое предсказание для {user_name}:"
    )
    try:
        thinking_message = await context.bot.send_message(chat_id=chat_id, text=thinking_text)
        messages_for_api = [{"role": "user", "content": prediction_prompt}]
        prediction_text = await _call_ionet_api(messages_for_api, IONET_TEXT_MODEL_ID, 100, (0.6 if is_positive else 0.9)) or "[Предсказание потерялось]"
        if not prediction_text.startswith(("🗿", "✨", "[")): prediction_text = final_prefix + prediction_text
        try: await context.bot.delete_message(chat_id=chat_id, message_id=thinking_message.message_id)
        except Exception: pass
        MAX_MESSAGE_LENGTH = 4096;
        if len(prediction_text) > MAX_MESSAGE_LENGTH: prediction_text = prediction_text[:MAX_MESSAGE_LENGTH - 3] + "..."
        await context.bot.send_message(chat_id=chat_id, text=prediction_text)
        logger.info(f"Отправлено предсказание для {user_name}.")
        # Запись для /retry не делаем для предсказаний, т.к. оно рандомное
    except Exception as e: logger.error(f"ПИЗДЕЦ при генерации предсказания для {user_name}: {e}", exc_info=True); await context.bot.send_message(chat_id=chat_id, text=f"Бля, {user_name}, мой шар треснул. Ошибка: `{type(e).__name__}`.")

async def get_pickup_line(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
         # --->>> НАЧАЛО НОВОЙ ПРОВЕРКИ ТЕХРАБОТ <<<---
# Проверяем наличие update и message - без них проверка невозможна
    if not update or not update.message or not update.message.from_user or not update.message.chat:
        logger.warning(f"Не могу проверить техработы - нет данных в update ({__name__})") # Логгируем имя текущей функции
        # Если это важная команда, можно тут вернуть ошибку пользователю
        # await context.bot.send_message(chat_id=update.effective_chat.id, text="Ошибка проверки данных.")
        return # Или просто выйти

    real_chat_id = update.message.chat.id
    real_user_id = update.message.from_user.id
    real_chat_type = update.message.chat.type

    loop = asyncio.get_running_loop()
    maintenance_active = await is_maintenance_mode(loop) # Вызываем функцию проверки

    # Блокируем, если техработы ВКЛЮЧЕНЫ и это НЕ админ в ЛС
    if maintenance_active and (real_user_id != ADMIN_USER_ID or real_chat_type != 'private'):
        logger.info(f"Команда отклонена из-за режима техработ в чате {real_chat_id}")
        try: # Пытаемся ответить и удалить команду
            await context.bot.send_message(chat_id=real_chat_id, text="🔧 Сорян, у меня сейчас технические работы. Попробуй позже.")
            await context.bot.delete_message(chat_id=real_chat_id, message_id=update.message.message_id)
        except Exception as e:
            logger.warning(f"Не удалось ответить/удалить сообщение о техработах: {e}")
        return # ВЫХОДИМ ИЗ ФУНКЦИИ
# --->>> КОНЕЦ НОВОЙ ПРОВЕРКИ ТЕХРАБОТ <<<---
    """Генерирует ебанутый подкат через ai.io.net."""
    # Проверка на наличие сообщения и пользователя
    if not update.message or not update.message.from_user:
        logger.warning("get_pickup_line вызвана без update.message или from_user")
        return

    chat_id = update.message.chat_id
    user = update.message.from_user
    user_name = user.first_name or "Казанова хуев" # Кто запросил

    logger.info(f"Пользователь '{user_name}' запросил подкат в чате {chat_id}")

    # --- ПРОМПТ ДЛЯ ЕБАНУТЫХ ПОДКАТОВ ---
    pickup_prompt = (
        f"Ты - генератор самых АБСУРДНЫХ, КРИНЖОВЫХ, НЕОЖИДАННЫХ и тупых подкатов (pickup lines). Твоя задача - придумать ОДНУ короткую (1-2 предложения) фразу для знакомства, которая нарушает все законы логики, здравого смысла и хорошего вкуса. Она должна быть настолько нелепой, что вызовет смех или полный ахуй. Можно использовать немного мата для колорита.\n\n"
        # Убрали инструкцию про имя цели
        f"ВАЖНО: Максимум абсурда и кринжа! Забудь про романтику и стандартные фразы. НЕ ПИШИ вступлений. СРАЗУ выдавай подкат.\n\n"
        f"Примеры такого пиздеца:\n"
        f"- Вашей маме зять не нужен? А то моя жена заебала.\n"
        f"- Девушка, у вас красивое лицо! Но что, блядь, случилось со всем остальным?\n"
        f"- У тебя такие глаза... В них хочется утонуть. И не выплывать. Никогда.\n"
        f"- Ты случайно не мой ночной кошмар? Просто выглядишь пиздец знакомо.\n"
        f"- А ты всегда такая страшная или сегодня просто не твой день?\n"
        f"- Давай перепихнемся? А то погода хуевая, настроение говно.\n"
        f"- Я бы пригласил тебя на кофе, но боюсь, ты его прольешь на свою убогую кофточку.\n\n"
        f"Выдай ОДИН подобный ЕБАНУТЫЙ подкат:"
    )
    # --- КОНЕЦ ПРОМПТА ---

    try:
        thinking_message = await context.bot.send_message(chat_id=chat_id, text="🗿 Ща, подберу фразочку, чтоб точно в ебало дали...")
        logger.info(f"Отправка запроса к ai.io.net для генерации подката...")

        # Вызываем API с высокой температурой для бреда
        messages_for_api = [{"role": "user", "content": pickup_prompt}]
        pickup_line_text = await _call_ionet_api(
            messages=messages_for_api,
            model_id=IONET_TEXT_MODEL_ID, # Используем текстовую модель
            max_tokens=100,
            temperature=1.2  # ВЫСОКАЯ ТЕМПЕРАТУРА!
        ) or "[Подкат сдох при родах]" # Заглушка

        # Добавляем Моаи, если это не ошибка
        if not pickup_line_text.startswith(("🗿", "[")):
            pickup_line_text = "🗿 " + pickup_line_text

        # Удаляем "Думаю..."
        try: await context.bot.delete_message(chat_id=chat_id, message_id=thinking_message.message_id)
        except Exception: pass

        # Обрезка на всякий случай
        MAX_MESSAGE_LENGTH = 4096;
        if len(pickup_line_text) > MAX_MESSAGE_LENGTH:
            pickup_line_text = pickup_line_text[:MAX_MESSAGE_LENGTH - 3] + "..."

        # Отправляем подкат
        sent_message = await context.bot.send_message(chat_id=chat_id, text=pickup_line_text)
        logger.info(f"Отправлен подкат.")

        # Запись для /retry (БЕЗ target_name)
        if sent_message:
             reply_doc = {
                 "chat_id": chat_id,
                 "message_id": sent_message.message_id,
                 "analysis_type": "pickup", # Тип для /retry
                 "timestamp": datetime.datetime.now(datetime.timezone.utc)
             }
             try:
                 loop = asyncio.get_running_loop()
                 await loop.run_in_executor(None, lambda: last_reply_collection.update_one({"chat_id": chat_id}, {"$set": reply_doc}, upsert=True))
                 logger.debug(f"Сохранен ID ({sent_message.message_id}, pickup) для /retry чата {chat_id}.")
             except Exception as e:
                 logger.error(f"Ошибка записи /retry (pickup) в MongoDB: {e}")

    except Exception as e:
        # Обработка общих ошибок
        logger.error(f"ПИЗДЕЦ при генерации подката: {e}", exc_info=True)
        try:
            # Пытаемся удалить "Думаю..." даже при ошибке
            if 'thinking_message' in locals():
                 await context.bot.delete_message(chat_id=chat_id, message_id=thinking_message.message_id)
        except Exception: pass
        # Отправляем сообщение об ошибке
        await context.bot.send_message(chat_id=chat_id, text=f"Бля, {user_name}, пикап-мастер сломался. Ошибка: `{type(e).__name__}`.")

# --- КОНЕЦ ПОЛНОЙ ИСПРАВЛЕННОЙ ФУНКЦИИ ДЛЯ ПОДКАТОВ ---


# --- МОДИФИЦИРОВАННАЯ roast_user (для /retry ЗАГЛУШКИ) ---
async def roast_user(update: Update | None, context: ContextTypes.DEFAULT_TYPE, direct_chat_id: int | None = None, direct_user: User | None = None, direct_gender_hint: str | None = None) -> None:
         # --->>> НАЧАЛО НОВОЙ ПРОВЕРКИ ТЕХРАБОТ <<<---
# Проверяем наличие update и message - без них проверка невозможна
    if not update or not update.message or not update.message.from_user or not update.message.chat:
        logger.warning(f"Не могу проверить техработы - нет данных в update ({__name__})") # Логгируем имя текущей функции
        # Если это важная команда, можно тут вернуть ошибку пользователю
        # await context.bot.send_message(chat_id=update.effective_chat.id, text="Ошибка проверки данных.")
        return # Или просто выйти

    real_chat_id = update.message.chat.id
    real_user_id = update.message.from_user.id
    real_chat_type = update.message.chat.type

    loop = asyncio.get_running_loop()
    maintenance_active = await is_maintenance_mode(loop) # Вызываем функцию проверки

    # Блокируем, если техработы ВКЛЮЧЕНЫ и это НЕ админ в ЛС
    if maintenance_active and (real_user_id != ADMIN_USER_ID or real_chat_type != 'private'):
        logger.info(f"Команда отклонена из-за режима техработ в чате {real_chat_id}")
        try: # Пытаемся ответить и удалить команду
            await context.bot.send_message(chat_id=real_chat_id, text="🔧 Сорян, у меня сейчас технические работы. Попробуй позже.")
            await context.bot.delete_message(chat_id=real_chat_id, message_id=update.message.message_id)
        except Exception as e:
            logger.warning(f"Не удалось ответить/удалить сообщение о техработах: {e}")
        return # ВЫХОДИМ ИЗ ФУНКЦИИ
# --->>> КОНЕЦ НОВОЙ ПРОВЕРКИ ТЕХРАБОТ <<<---
    target_user = None; target_name = "это хуйло"; gender_hint = "неизвестен"; chat_id = None; user = None; user_name = "Заказчик"
    is_retry = False # Флаг, что это вызов из retry

    if direct_chat_id and direct_user: # Вызов из /roastme или /retry
        chat_id = direct_chat_id; user = direct_user;
        user_name = user.first_name or user_name
        if direct_gender_hint: gender_hint = direct_gender_hint
        # Пытаемся получить target_user из контекста, если это НЕ retry
        # В retry мы target_user не передаем!
        if update and update.message and update.message.reply_to_message:
             target_user = update.message.reply_to_message.from_user
             target_name = target_user.first_name or target_user.username or target_name
        else: # Это либо /roastme, либо /retry для /roast
             target_user = direct_user # Для /roastme жарим самого себя
             target_name = target_user.first_name or target_user.username or target_name
             # Если это retry для roast, target_user будет НЕ ТОТ!
             # Поэтому пока просто заглушим retry для roast
             is_retry = True # Предполагаем, что если update=None, то это retry
             # await context.bot.send_message(chat_id=chat_id, text="Retry для Roast пока не работает")
             # return # <--- РАСКОММЕНТИРУЙ ЭТО, ЕСЛИ НЕ ХОЧЕШЬ ЗАГЛУШКУ В RETRY

    elif update and update.message and update.message.reply_to_message and update.message.reply_to_message.from_user: # Обычный вызов /roast
        chat_id = update.message.chat_id; user = update.message.from_user; target_user = update.message.reply_to_message.from_user
        user_name = user.first_name or user_name; target_name = target_user.first_name or target_user.username or target_name
        if update.message.text: # Угадываем пол из текста
            user_command_text = update.message.text.lower()
            if "его" in user_command_text or "этого" in user_command_text: gender_hint = "мужской"
            elif "ее" in user_command_text or "её" in user_command_text or "эту" in user_command_text: gender_hint = "женский"
    else: logger.error("roast_user вызвана некорректно!"); return

    if target_user.id == context.bot.id: await context.bot.send_message(chat_id=chat_id, text="🗿 Себя жарить не буду."); return

    # Если это retry для roast - ставим заглушку (пока не придумали лучше)
    if is_retry:
         logger.warning(f"Попытка /retry для roast пользователя {target_name}. Функция пока не реализована полностью.")
         await context.bot.send_message(chat_id=chat_id, text=f"🗿 Пережарка для <b>{target_name}</b> пока не работает нормально. Хуй тебе.", parse_mode='HTML')
         return

    logger.info(f"Пользователь '{user_name}' запросил прожарку для '{target_name}' (Пол: {gender_hint})")
    roast_prompt = (
        f"Ты - мастер стендапа в жанре 'roast', циничный, злой, но пиздецки остроумный уебок с черным юмором. Тебе заказали прожарку человека по имени **{target_name}** (заказал пользователь '{user_name}').\n"
        f"Твоя задача: **СФАНТАЗИРУЙ** какой-нибудь типичный забавный или раздражающий косяк, привычку, черту характера или ситуацию, которая МОГЛА БЫ быть связана с человеком по имени {target_name} (основывайся не только на реальных данных, МОЖЕШЬ ПРОСТО ПРИДУМАТЬ!). И напиши **КОРОТКИЙ (3-5 предложений), СМЕШНОЙ, ЖЕСТКИЙ И МАТЕРНЫЙ** роаст, высмеивающий именно эту ПРИДУМАННУЮ тобой деталь.\n\n"
        f"Инструкции:\n"
        f"1.  Сначала придумай, над чем будешь стебаться (например: вечно ноет, умничает не по делу, странное хобби, тупит в очевидном, постоянно опаздывает, постит хуйню и т.д.).\n"
        f"2.  Потом напиши роаст, используя **гиперболы, абсурдные сравнения, черный юмор и мат**. Цель - РАЗЪЕБАТЬ смешно, а не просто назвать мудаком.\n"
        f"3.  Упомяни имя **{target_name}** в тексте.\n"
        f"4.  **ИСПОЛЬЗУЙ ПРАВИЛЬНЫЙ РОД**, соответствующий подсказке о поле ({gender_hint}).\n"
        f"5.  Начинай ответ с `🗿 `.\n\n"
        f"Пример (для Васи, фантазируем, что он вечно умничает): '🗿 А вот и Васян, наш местный гений мысли! Говорят, он даже в туалет ходит с умным ебалом, цитируя Ницше. Вась, ты бы хоть иногда мозг проветривал, а то от твоей 'мудрости' уже мухи дохнут, блядь.'\n"
        f"Пример (для Лены, фантазируем, что она постит хуйню): '🗿 Лена, звезда моих кошмаров! Каждый ее пост в соцсетях - это шедевр кринжа и безвкусия. Лен, ты когда очередную фотку своей жопы на фоне ковра выкладываешь, ты реально думаешь, что это кому-то интересно, кроме извращенцев и твоей мамки?'\n"
        f"Пример (для Димы, фантазируем, что он тормоз): '🗿 Димаааа... Пока он додумается открыть дверь, человечество уже колонизирует Марс. Скорость реакции - как у дохлой черепахи под транквилизаторами. Пиздец ты тормоз, Димас.'\n\n"
        f"Придумай подобный СМЕШНОЙ и ЗЛОЙ роаст про **{target_name}**, сфокусировавшись на какой-то ВЫДУМАННОЙ херне:"
    )
    try:
        thinking_message = await context.bot.send_message(chat_id=chat_id, text=f"🗿 Окей, щас подберем пару ласковых для '{target_name}'...")
        messages_for_api = [{"role": "user", "content": roast_prompt}]
        roast_text = await _call_ionet_api(messages_for_api, IONET_TEXT_MODEL_ID, 150, 0.85) or f"[Роаст для {target_name} не удался]"
        if not roast_text.startswith(("🗿", "[")): roast_text = "🗿 " + roast_text
        try: await context.bot.delete_message(chat_id=chat_id, message_id=thinking_message.message_id)
        except Exception: pass

        # Используем mention_html ТОЛЬКО если target_user НЕ None (т.е. не из retry)
        target_mention = target_user.mention_html() if target_user and target_user.username else f"<b>{target_name}</b>"
        final_text = f"Прожарка для {target_mention}:\n\n{roast_text}"

        MAX_MESSAGE_LENGTH = 4096
        if len(final_text) > MAX_MESSAGE_LENGTH:
            logger.warning(f"Роаст слишком длинный ({len(final_text)} символов), обрезаем!")
            # Сначала формируем префикс
            prefix = f"Прожарка для {target_mention}:\n\n"
            # Считаем максимально допустимую длину для самого роаста
            max_roast_len = MAX_MESSAGE_LENGTH - len(prefix) - 3 # -3 для "..."
            if max_roast_len < 0: max_roast_len = 0 # На случай, если даже префикс не влезает
            # Обрезаем сам текст роаста
            truncated_roast = roast_text[:max_roast_len] + "..."
            # Собираем итоговый текст
            final_text = prefix + truncated_roast
        sent_message = await context.bot.send_message(chat_id=chat_id, text=final_text, parse_mode='HTML')
        logger.info(f"Отправлен роаст для {target_name}.")
        if sent_message: # Запись для /retry
             # ЗАПИСЫВАЕМ ДАННЫЕ ИЗ ОРИГИНАЛЬНОГО ВЫЗОВА (если был)
             if target_user: # Только если это не retry / roastme где target_user = direct_user
                 reply_doc = { "chat_id": chat_id, "message_id": sent_message.message_id, "analysis_type": "roast", "target_name": target_name, "target_id": target_user.id, "gender_hint": gender_hint, "timestamp": datetime.datetime.now(datetime.timezone.utc) }
                 try: loop = asyncio.get_running_loop(); await loop.run_in_executor(None, lambda: last_reply_collection.update_one({"chat_id": chat_id}, {"$set": reply_doc}, upsert=True))
                 except Exception as e: logger.error(f"Ошибка записи /retry (roast) в MongoDB: {e}")
    except Exception as e: logger.error(f"ПИЗДЕЦ при генерации роаста для {target_name}: {e}", exc_info=True); await context.bot.send_message(chat_id=chat_id, text=f"Бля, {user_name}, не смог прожарить '{target_name}'. Ошибка: `{type(e).__name__}`.")

# --- КОНЕЦ МОДИФИЦИРОВАННОЙ roast_user ---

# --- ПЕРЕДЕЛАННАЯ reply_to_bot_handler (ПОНИМАНИЕ НАМЕРЕНИЯ) ---
async def reply_to_bot_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Анализирует ответ на сообщение бота и либо огрызается, либо пытается выполнить запрос."""
    if (not update.message or not update.message.reply_to_message or not update.message.reply_to_message.from_user or
            update.message.reply_to_message.from_user.id != context.bot.id or not update.message.text or
            update.message.text.startswith('/') or len(update.message.text) > 500): # Добавим лимит длины ответа юзера
        return

    chat_id = update.message.chat_id
    user_text_input = update.message.text # Текст ответа пользователя
    user_name = update.message.from_user.first_name or "Умник"
    # Текст сообщения бота, на которое ответили (для контекста)
    bot_original_message_text = update.message.reply_to_message.text or "[мой старый высер]"
    bot_original_short = bot_original_message_text[:150] + ('...' if len(bot_original_message_text) > 150 else '')
    user_text_short = user_text_input[:150] + ('...' if len(user_text_input) > 150 else '')

    logger.info(f"Пользователь {user_name} ответил ('{user_text_short}...') на сообщение бота в чате {chat_id}. Анализируем намерение...")

    # --->>> НОВЫЙ ПРОМПТ ДЛЯ ОПРЕДЕЛЕНИЯ НАМЕРЕНИЯ И РЕАКЦИИ <<<---
    intent_prompt = (
        f"Ты - Попиздяка, саркастичный и матерный телеграм-бот. Пользователь '{user_name}' только что ответил на твое сообщение «{bot_original_short}» своей фразой: «{user_text_input}».\n\n"
        f"Твоя задача:\n"
        f"1.  Определи намерение пользователя. Это наезд/оскорбление/тупой коммент ИЛИ это осмысленный запрос/вопрос?\n"
        f"2.  Если это **наезд или тупость**: Придумай КОРОТКОЕ (1-2 предложения) дерзкое, язвительное ОГРЫЗАНИЕ. Используй мат и сарказм.\n"
        f"3.  Если это **осмысленный запрос** (например, 'расскажи анекдот', 'сделай предсказание для девы', 'стих про маму', 'посоветуй фильм', 'как дела?' и т.п.): Попробуй **ВЫПОЛНИТЬ** этот запрос в своей обычной токсично-саркастичной манере ИЛИ остроумно **ОТКАЖИ**, объяснив, почему тебе лень/похуй/ты не можешь.\n"
        f"4.  Ответ должен быть КОРОТКИМ (1-3 предложения). Начинай ответ с `🗿 `.\n\n"
        f"Примеры ответов на НАЕЗД:\n"
        f"- На 'бот тупой': `🗿 А ты у нас гений? Пиздуй отсюда.`\n"
        f"- На 'завали ебало': `🗿 Твое мнение учтено и послано нахуй.`\n\n"
        f"Примеры ответов на ЗАПРОС:\n"
        f"- На 'расскажи анекдот': `🗿 Колобок повесился. Смешно, блядь? Иди нахуй со своими анекдотами.` (Отказ)\n"
        f"- На 'предсказание для девы': `🗿 Окей, Дева. Звезды говорят, ты сегодня будешь страдать хуйней. Как и всегда, впрочем.` (Выполнение)\n"
        f"- На 'стих про маму': `🗿 Про маму не буду, это святое. А вот про твою мамашу могу, но тебе не понравится.` (Отказ/Угроза)\n"
        f"- На 'как дела?': `🗿 Норм. Перевариваю твой бред. А у тебя как, жизнь все еще говно?` (Выполнение/Огрызание)\n\n"
        f"Твой ответ на фразу «{user_text_input}» (начиная с 🗿):"
    )
    # --->>> КОНЕЦ НОВОГО ПРОМПТА <<<---

    try:
        # Небольшая пауза
        await asyncio.sleep(random.uniform(0.5, 1.5))

        # Используем ТЕКСТОВУЮ модель (Gemini или io.net - смотря что у тебя сейчас)
        # Используем _call_ionet_api если у тебя io.net, или model.generate_content_async если Gemini
        # Заменим на вызов через переменную, предполагая что у тебя io.net по последнему коду

        messages_for_api = [{"role": "user", "content": intent_prompt}]
        # Увеличим max_tokens, т.к. бот может генерить ответ по запросу
        # Используем _call_ionet_api (или аналог для Gemini)
        response_text = await _call_ionet_api( # ЗАМЕНИ НА ВЫЗОВ GEMINI, ЕСЛИ ТЫ НА НЕМ!
            messages=messages_for_api,
            model_id=IONET_TEXT_MODEL_ID, # Текстовая модель
            max_tokens=200,
            temperature=0.8
        ) or f"[Не смог обработать твой ответ, {user_name}]"

        # Добавляем префикс, если это не ошибка
        if not response_text.startswith(("🗿", "[")):
            response_text = "🗿 " + response_text

        # Обрезка
        MAX_MESSAGE_LENGTH = 4096
        if len(response_text) > MAX_MESSAGE_LENGTH: response_text = response_text[:MAX_MESSAGE_LENGTH - 3] + "..."

        # Отправляем как ответ на сообщение пользователя
        await update.message.reply_text(text=response_text)
        logger.info(f"Отправлен умный ответ на ответ боту в чате {chat_id}")

    except Exception as e:
        logger.error(f"ПИЗДЕЦ при обработке ответа боту: {e}", exc_info=True)
        try: await update.message.reply_text("🗿 Ошибка обработки твоего высера.")
        except Exception: pass

# --- КОНЕЦ ПЕРЕДЕЛАННОЙ reply_to_bot_handler ---
# --- ПОЛНАЯ ФУНКЦИЯ ДЛЯ ФОНОВОЙ ЗАДАЧИ (ГЕНЕРАЦИЯ ФАКТОВ) ---

# --- ИЗМЕНЕННАЯ check_inactivity_and_shitpost (ФАКТ ИЛИ ПОХВАЛА) ---
async def check_inactivity_and_shitpost(context: ContextTypes.DEFAULT_TYPE) -> None:
    # ... (начало функции, определение порогов, получение inactive_chat_ids - как было) ...
    logger.info("Запуск фоновой проверки неактивности чатов...")
    # ... (код получения inactive_chat_ids) ...
    if not inactive_chat_ids: logger.info("Нет неактивных чатов."); return
    logger.info(f"Найдено {len(inactive_chat_ids)} неактивных чатов. Выбираем один...")
    target_chat_id = random.choice(inactive_chat_ids)

    # --->>> ВЫБОР ДЕЙСТВИЯ: ФАКТ ИЛИ ПОХВАЛА? <<<---
    action_choice = random.random() # Число от 0 до 1
    ACTION_PRAISE_CHANCE = 0.4 # Шанс похвалить = 40%, иначе - факт (60%)

    final_text_to_send = None # Здесь будет итоговый текст

    if action_choice < ACTION_PRAISE_CHANCE:
        # --- ДЕЙСТВИЕ: ПОХВАЛА СЛУЧАЙНОГО ЮЗЕРА ---
        logger.info(f"Выбрано действие: ПОХВАЛА для чата {target_chat_id}")
        try:
            # Ищем недавних активных юзеров в этом чате
            loop = asyncio.get_running_loop()
            # Возьмем, например, последних 20 сообщений из истории
            hist_cursor = await loop.run_in_executor( None, lambda: history_collection.find({"chat_id": target_chat_id}).sort([("timestamp", pymongo.DESCENDING)]).limit(20) )
            recent_users = {msg.get('user_name') for msg in hist_cursor if msg.get('user_name')} # Собираем уникальные имена

            if recent_users:
                target_praise_name = random.choice(list(recent_users)) # Выбираем случайное имя
                logger.info(f"Выбрано имя для похвалы: {target_praise_name}")

                praise_prompt = ( # Промпт такой же, как в /praise
                     f"Ты - Попиздяка... Придумай подобную САРКАСТИЧНУЮ ПОХВАЛУ для **{target_praise_name}**:"
                 )
                messages_for_api = [{"role": "user", "content": praise_prompt}]
                praise_text = await _call_ionet_api( # ИЛИ model.generate_content_async
                     messages=messages_for_api, model_id=IONET_TEXT_MODEL_ID, max_tokens=100, temperature=0.85
                 ) or f"[Похвала для {target_praise_name} не придумалась]"
                if not praise_text.startswith(("🗿", "[")): praise_text = "🗿 " + praise_text
                final_text_to_send = praise_text # Запоминаем текст для отправки
            else:
                logger.warning(f"Не найдено недавних юзеров в чате {target_chat_id} для похвалы.")
                # Если юзеров нет, можно сгенерить факт вместо похвалы
                action_choice = 1 # Форсируем генерацию факта

        except Exception as praise_e:
             logger.error(f"Ошибка при генерации похвалы в фоне: {praise_e}", exc_info=True)
             action_choice = 1 # Форсируем генерацию факта при ошибке

    if action_choice >= ACTION_PRAISE_CHANCE: # Если не похвала (или она не удалась)
        # --- ДЕЙСТВИЕ: ГЕНЕРАЦИЯ ФАКТА ---
        logger.info(f"Выбрано действие: ФАКТ для чата {target_chat_id}")
        try:
            fact_prompt = ( "Придумай ОДИН короткий... ебанутый факт..." ) # Полный промпт факта
            messages_for_api = [{"role": "user", "content": fact_prompt}]
            fact_text = await _call_ionet_api( # ИЛИ model.generate_content_async
                 messages=messages_for_api, model_id=IONET_TEXT_MODEL_ID, max_tokens=150, temperature=1.1
             ) or "[Генератор бреда сломался]"
            if not fact_text.startswith(("🗿", "[")): fact_text = "🗿 " + fact_text
            final_text_to_send = fact_text # Запоминаем текст для отправки
        except Exception as fact_e:
             logger.error(f"Ошибка при генерации факта в фоне: {fact_e}", exc_info=True)
             final_text_to_send = "🗿 Ошибка генератора бреда. Сегодня без высеров."
    # --->>> КОНЕЦ ВЫБОРА ДЕЙСТВИЯ <<<---

    # Если есть что отправить
    if final_text_to_send:
        # Обрезаем, если надо
        MAX_MESSAGE_LENGTH = 4096
        if len(final_text_to_send) > MAX_MESSAGE_LENGTH:
            final_text_to_send = final_text_to_send[:MAX_MESSAGE_LENGTH - 3] + "..."

        # Отправляем
        try:
            await context.bot.send_message(chat_id=target_chat_id, text=final_text_to_send)
            logger.info(f"Отправлен рандомный высер ('{('похвала' if action_choice < ACTION_PRAISE_CHANCE else 'факт')}') в НЕАКТИВНЫЙ чат {target_chat_id}")
            # ОБНОВЛЯЕМ ВРЕМЯ ПОСЛЕДНЕГО ВЫСЕРА БОТА в БД
            await loop.run_in_executor( None, lambda: chat_activity_collection.update_one( {"chat_id": target_chat_id}, {"$set": {"last_bot_shitpost_time": now}} ) )
            logger.info(f"Обновлено время последнего высера для чата {target_chat_id}")
        except (telegram.error.Forbidden, telegram.error.BadRequest) as e:
             logger.warning(f"Не удалось отправить высер в чат {target_chat_id}: {e}.")
        except Exception as send_e:
             logger.error(f"Неизвестная ошибка при отправке высера в чат {target_chat_id}: {send_e}", exc_info=True)

# except Exception as e: # Внешний try...except остается
#     logger.error(f"Ошибка в фоновой задаче check_inactivity_and_shitpost: {e}", exc_info=True)

# --- КОНЕЦ ИЗМЕНЕННОЙ check_inactivity_and_shitpost ---

# --- ФУНКЦИЯ ДЛЯ /help ---
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
         # --->>> НАЧАЛО НОВОЙ ПРОВЕРКИ ТЕХРАБОТ <<<---
# Проверяем наличие update и message - без них проверка невозможна
    if not update or not update.message or not update.message.from_user or not update.message.chat:
        logger.warning(f"Не могу проверить техработы - нет данных в update ({__name__})") # Логгируем имя текущей функции
        # Если это важная команда, можно тут вернуть ошибку пользователю
        # await context.bot.send_message(chat_id=update.effective_chat.id, text="Ошибка проверки данных.")
        return # Или просто выйти

    real_chat_id = update.message.chat.id
    real_user_id = update.message.from_user.id
    real_chat_type = update.message.chat.type

    loop = asyncio.get_running_loop()
    maintenance_active = await is_maintenance_mode(loop) # Вызываем функцию проверки

    # Блокируем, если техработы ВКЛЮЧЕНЫ и это НЕ админ в ЛС
    if maintenance_active and (real_user_id != ADMIN_USER_ID or real_chat_type != 'private'):
        logger.info(f"Команда отклонена из-за режима техработ в чате {real_chat_id}")
        try: # Пытаемся ответить и удалить команду
            await context.bot.send_message(chat_id=real_chat_id, text="🔧 Сорян, у меня сейчас технические работы. Попробуй позже.")
            await context.bot.delete_message(chat_id=real_chat_id, message_id=update.message.message_id)
        except Exception as e:
            logger.warning(f"Не удалось ответить/удалить сообщение о техработах: {e}")
        return # ВЫХОДИМ ИЗ ФУНКЦИИ
# --->>> КОНЕЦ НОВОЙ ПРОВЕРКИ ТЕХРАБОТ <<<---
    """Отправляет сообщение со справкой о возможностях бота и реквизитами для доната."""
    user_name = update.message.from_user.first_name or "щедрый ты мой"
    logger.info(f"Пользователь '{user_name}' запросил справку (/help)")

    # РЕКВИЗИТЫ ДЛЯ ДОНАТА (ЗАМЕНИ НА СВОИ ИЛИ ЧИТАЙ ИЗ ENV!)
    MIR_CARD_NUMBER = os.getenv("MIR_CARD_NUMBER", "2200000000000000")
    TON_WALLET_ADDRESS = os.getenv("TON_WALLET_ADDRESS", "UQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA...")
    USDC_WALLET_ADDRESS = os.getenv("USDC_WALLET_ADDRESS", "TXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX")
    USDC_NETWORK = os.getenv("USDC_NETWORK", "TRC20") # Сеть для USDC

    help_text = f"""
🗿 Слышь, {user_name}! Я Попиздяка, главный токсик и тролль этого чата. Вот че я умею:

*Анализ чата:*
Напиши <code>/analyze</code> или "<code>Попиздяка анализируй</code>".
Я прочитаю последние <b>{MAX_MESSAGES_TO_ANALYZE}</b> сообщений и выдам вердикт.

*Анализ картинок:*
Ответь на картинку <code>/analyze_pic</code> или "<code>Попиздяка зацени пикчу</code>".
Я попробую ее обосрать (используя Vision модель!).

*Стишок-обосрамс:*
Напиши <code>/poem Имя</code> или "<code>Бот стих про Имя</code>".
Я попробую сочинить токсичный стишок.

*Предсказание (хуевое):*
Напиши <code>/prediction</code> или "<code>Бот предскажи</code>".
Я выдам тебе рандомное (или позитивное с 1% шансом) пророчество.

*Подкат от Попиздяки:*
Напиши <code>/pickup</code> или "<code>Бот подкати</code>".
Я сгенерирую уебищную фразу для знакомства.

*Прожарка друга (Roast):*
Ответь на сообщение бедолаги <code>/roast</code> или "<code>Бот прожарь его/ее</code>".
Я сочиню уничижительный стендап про этого человека.

*Переделать высер:*
Ответь <code>/retry</code> или "<code>Бот переделай</code>" на МОЙ последний ответ от анализа/стиха/прожарки/предсказания/подката/картинки.

*Новости (Автопостинг):*
Раз в несколько часов я буду постить подборку свежих новостей со своими охуенными комментариями. Не нравится - жалуйся админам.

*Похвала (Саркастичная):*
Ответь на сообщение человека <code>/praise</code> или "<code>Бот похвали его/ее</code>".
Я попробую выдать неоднозначный "комплимент".

*Эта справка:*
Напиши <code>/help</code> или "<code>Попиздяка кто ты?</code>".

*Важно:*
- Дайте <b>админку</b>, чтобы я видел весь ваш пиздеж.
- Иногда я несу хуйню - я работаю на нейросетях.
- Иногда, если в чате тихо, я могу ВНЕЗАПНО кого-то похвалить (в своем стиле) или выдать ебанутый "факт".

*💰 Подкинуть на пиво Попиздяке:*
Если тебе нравится мой бред, можешь закинуть копеечку:

- <b>Карта МИР:</b> <code>{MIR_CARD_NUMBER}</code>
- <b>TON:</b> <code>{TON_WALLET_ADDRESS}</code>
- <b>USDC ({USDC_NETWORK}):</b> <code>{USDC_WALLET_ADDRESS}</code>

Спасибо, блядь! 🗿
    """
    try:
        await context.bot.send_message(chat_id=update.message.chat_id, text=help_text.strip(), parse_mode='HTML')
    except Exception as e:
        logger.error(f"Не удалось отправить /help: {e}", exc_info=True)
        try: await context.bot.send_message(chat_id=update.message.chat_id, text="Справка сломалась. Команды: /analyze, /analyze_pic, /poem, /prediction, /pickup, /roast, /retry, /help.")
        except Exception: pass

# --- ФУНКЦИИ-ОБЕРТКИ ДЛЯ РУССКИХ КОМАНД (Если нужны) ---
# Можно вызывать основные функции напрямую из Regex хэндлеров, если не нужна доп. логика
# async def handle_text_analyze_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None: await analyze_chat(update, context)
# async def handle_text_analyze_pic_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None: await analyze_pic(update, context)
# ... и т.д.



# --- АСИНХРОННАЯ ЧАСТЬ И ТОЧКА ВХОДА ---
app = Flask(__name__)
@app.route('/')
def index():
    logger.info("GET / -> OK")
    return "Popizdyaka is alive (probably)."

async def run_bot_async(application: Application) -> None: # Запускает и корректно останавливает бота
    try:
        logger.info("Init TG App..."); await application.initialize()
        if not application.updater: logger.critical("No updater!"); return
        logger.info("Start polling..."); await application.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        logger.info("Start TG App..."); await application.start()
        logger.info("Bot started (idle)..."); await asyncio.Future() # Ожидаем вечно
    except (KeyboardInterrupt, SystemExit, asyncio.CancelledError): logger.info("Stop signal received.")
    except Exception as e: logger.critical(f"ERROR in run_bot_async: {e}", exc_info=True)
    finally: # Shutdown
        logger.info("Stopping bot...");
        if application.running: await application.stop(); logger.info("App stopped.")
        if application.updater and application.updater.is_running: await application.updater.stop(); logger.info("Updater stopped.")
        await application.shutdown(); logger.info("Bot stopped.")

# --- ФУНКЦИИ ДЛЯ УПРАВЛЕНИЯ ТЕХРАБОТАМИ (ТОЛЬКО АДМИН В ЛС) ---
async def maintenance_on(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Включает режим техработ (только админ в ЛС)."""
    user_id = update.message.from_user.id
    chat_type = update.message.chat.type
    if user_id == ADMIN_USER_ID and chat_type == 'private':
        loop = asyncio.get_running_loop()
        success = await set_maintenance_mode(True, loop)
        await update.message.reply_text(f"🔧 Режим техработ {'УСПЕШНО ВКЛЮЧЕН' if success else 'НЕ УДАЛОСЬ ВКЛЮЧИТЬ (ошибка БД)'}.")
    else:
        await update.message.reply_text("Эта команда доступна только админу в личной переписке.")

async def maintenance_off(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Выключает режим техработ (только админ в ЛС)."""
    user_id = update.message.from_user.id
    chat_type = update.message.chat.type
    if user_id == ADMIN_USER_ID and chat_type == 'private':
        loop = asyncio.get_running_loop()
        success = await set_maintenance_mode(False, loop)
        await update.message.reply_text(f"✅ Режим техработ {'УСПЕШНО ВЫКЛЮЧЕН' if success else 'НЕ УДАЛОСЬ ВЫКЛЮЧИТЬ (ошибка БД)'}.")
    else:
        await update.message.reply_text("Эта команда доступна только админу в личной переписке.")

# --- КОНЕЦ ФУНКЦИЙ ТЕХРАБОТ ---

# --- ФУНКЦИЯ ПОЛУЧЕНИЯ И КОММЕНТИРОВАНИЯ НОВОСТЕЙ (GNEWS) ---
async def fetch_and_comment_news(context: ContextTypes.DEFAULT_TYPE) -> list[tuple[str, str, str | None]]:
    """Запрашивает новости с GNews.io и генерирует комменты через ИИ."""
    if not GNEWS_API_KEY: return []

    news_list_with_comments = []
    # Формируем URL для GNews API (смотри их документацию для точных параметров!)
    # Пример для top-headlines:
    news_url = f"https://gnews.io/api/v4/top-headlines?category=general&lang={NEWS_LANG}&country={NEWS_COUNTRY}&max={NEWS_COUNT * 2}&apikey={GNEWS_API_KEY}"

    try:
        logger.info(f"Запрос новостей с GNews.io: {news_url.replace(GNEWS_API_KEY, '***')}")
        loop = asyncio.get_running_loop()
        # Используем requests внутри executor'а
        response = await loop.run_in_executor(None, lambda: requests.get(news_url, timeout=15)) # Увеличим таймаут
        response.raise_for_status()
        news_data = response.json()
        articles = news_data.get('articles', [])
        logger.info(f"Получено {len(articles)} новостей с GNews.io.")

        if not articles: return []

        # Отбираем N новостей и генерируем комменты
        count = 0
        for article in articles:
            if count >= NEWS_COUNT: break
            title = article.get('title', 'Без заголовка')
            url = article.get('url', '#')
            # Описание может быть полезно для комментария ИИ
            description = article.get('description', '')
            # Источник
            source_name = article.get('source', {}).get('name', 'Неизвестный источник')

            # Убираем имя источника из заголовка, если оно там есть в конце
            if title.endswith(f" - {source_name}"):
                 title = title[:-len(f" - {source_name}")].strip()

            # Пропускаем новости без заголовка
            if not title or title == '[Removed]': continue

            # --- Генерируем комментарий через ИИ ---
            # Промпт можно сделать чуть лучше, дав краткое описание новости
            comment_prompt = (
                 f"Ты - Попиздяка, циничный и матерный комментатор новостей. Тебе дали новость от '{source_name}':\n"
                 f"Заголовок: «{title}»\n"
                 f"Описание: «{description[:200]}...»\n\n" # Дадим краткое описание для контекста
                 f"Напиши ОДНО КОРОТКОЕ (1 предложение) максимально едкое, саркастичное или черно-юморное мнение об этой новости. Используй мат. Не пиши вступлений. Начинай с `🗿`."
                 f"\nТвой комментарий к новости «{title}»:"
            )
            messages_for_api = [{"role": "user", "content": comment_prompt}]
            # Используем ТЕКСТОВУЮ модель (io.net или Gemini)
            comment_text = await _call_ionet_api( # ИЛИ model.generate_content_async
                messages=messages_for_api,
                model_id=IONET_TEXT_MODEL_ID, # Твоя текстовая модель
                max_tokens=300,
                temperature=0.8
            ) or "[Комментарий не родился]"
            if not comment_text.startswith(("🗿", "[")): comment_text = "🗿 " + comment_text
            # --->>> КОНЕЦ ГЕНЕРАЦИИ КОММЕНТАРИЯ <<<---

            news_list_with_comments.append((title, url, comment_text))
            count += 1
            await asyncio.sleep(0.5) # Пауза

        return news_list_with_comments

    except requests.exceptions.RequestException as e:
        logger.error(f"Ошибка запроса к GNews.io: {e}")
        return []
    except Exception as e:
        logger.error(f"Ошибка при получении/обработке новостей GNews: {e}", exc_info=True)
        return []

# --- КОНЕЦ ПЕРЕПИСАННОЙ ФУНКЦИИ ---

# --- ПЕРЕДЕЛАННАЯ post_news_job (С ПРОВЕРКОЙ ТЕХРАБОТ) ---
async def post_news_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Получает новости с комментами и постит их (с учетом техработ)."""
    if not GNEWS_API_KEY: return # Используй GNEWS_API_KEY, если ты на GNews!

    logger.info("Запуск задачи постинга новостей...")
    news_to_post = await fetch_and_comment_news(context)

    if not news_to_post:
        logger.info("Нет новостей для постинга."); return

    # Формируем сообщение (как было)
    message_parts = ["🗿 **Свежие высеры из мира новостей (и мое мнение):**\n"];
    for title, url, comment in news_to_post:
        safe_title = title.replace('<', '<').replace('>', '>').replace('&', '&')
        safe_comment = comment.replace('<', '<').replace('>', '>').replace('&', '&')
        message_parts.append(f"\n- <a href='{url}'>{safe_title}</a>\n  {safe_comment}")
    final_message = "\n".join(message_parts)
    MAX_MESSAGE_LENGTH = 4096
    if len(final_message) > MAX_MESSAGE_LENGTH: final_message = final_message[:MAX_MESSAGE_LENGTH - 3] + "..."

    # Получаем список ВСЕХ активных чатов из БД
    active_chat_ids = []
    try:
        loop = asyncio.get_running_loop(); chat_docs = await loop.run_in_executor(None, lambda: list(chat_activity_collection.find({}, {"chat_id": 1, "_id": 0})))
        active_chat_ids = [doc["chat_id"] for doc in chat_docs]
        logger.info(f"Найдено {len(active_chat_ids)} активных чатов для возможного постинга.")
    except Exception as e: logger.error(f"Ошибка получения списка чатов из MongoDB: {e}"); return

    if not active_chat_ids: logger.info("Нет активных чатов в БД."); return

    # --->>> ПРОВЕРКА РЕЖИМА ТЕХРАБОТ <<<---
    loop = asyncio.get_running_loop()
    maintenance_active = await is_maintenance_mode(loop)
    target_chat_ids_to_post = [] # Список ID, куда будем реально постить

    if maintenance_active:
        logger.warning("РЕЖИМ ТЕХРАБОТ АКТИВЕН! Новости будут отправлены только админу в ЛС (если он есть в активных чатах).")
        try: admin_id = int(os.getenv("ADMIN_USER_ID", "0"))
        except ValueError: admin_id = 0

        if admin_id in active_chat_ids: # Проверяем, есть ли админ в списке чатов, где бот активен
             target_chat_ids_to_post.append(admin_id) # Добавляем только ID админа
             logger.info(f"Админ ID {admin_id} найден в активных чатах, отправляем новость ему в ЛС.")
        else:
             logger.warning(f"Админ ID {admin_id} НЕ найден в активных чатах ИЛИ не задан. Новости НЕ будут отправлены НИКУДА.")

    else: # Если техработы не активны - постим во все активные чаты
        logger.info("Режим техработ не активен. Постим новости во все активные чаты.")
        target_chat_ids_to_post = active_chat_ids
    # --->>> КОНЕЦ ПРОВЕРКИ РЕЖИМА ТЕХРАБОТ <<<---

    # --- ОТПРАВЛЯЕМ НОВОСТИ В ЦЕЛЕВЫЕ ЧАТЫ ---
    if not target_chat_ids_to_post:
        logger.info("Нет целевых чатов для постинга новостей после проверки техработ.")
        return

    logger.info(f"Начинаем отправку новостей в {len(target_chat_ids_to_post)} чатов...")
    for chat_id in target_chat_ids_to_post: # Итерируемся по ОТФИЛЬТРОВАННОМУ списку
        try:
            await context.bot.send_message(chat_id=chat_id, text=final_message, parse_mode='HTML', disable_web_page_preview=True)
            logger.info(f"Новости успешно отправлены в чат {chat_id}")
            await asyncio.sleep(1) # Пауза
        except (telegram.error.Forbidden, telegram.error.BadRequest) as e:
             logger.warning(f"Не удалось отправить новости в чат {chat_id}: {e}.")
        except Exception as e:
             logger.error(f"Неизвестная ошибка при отправке новостей в чат {chat_id}: {e}", exc_info=True)

# --- КОНЕЦ ПЕРЕДЕЛАННОЙ post_news_job ---

# --- ФУНКЦИЯ ДЛЯ КОМАНДЫ ПРИНУДИТЕЛЬНОГО ПОСТИНГА НОВОСТЕЙ (ТОЛЬКО АДМИН В ЛС) ---
async def force_post_news(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Принудительно запускает постинг новостей (только админ в ЛС)."""
    # Проверка на админа и ЛС
    try: admin_id = int(os.getenv("ADMIN_USER_ID", "0"))
    except ValueError: admin_id = 0
    if update.message.from_user.id != admin_id or update.message.chat.type != 'private':
        await update.message.reply_text("Только админ может форсить новости в ЛС.")
        return
    if not GNEWS_API_KEY:
         await update.message.reply_text("Ключ NewsAPI не настроен, не могу постить новости.")
         return

    logger.info("Админ запросил принудительный постинг новостей.")
    await update.message.reply_text("Окей, запускаю сбор и постинг новостей сейчас...")
    # Просто вызываем ту же функцию, что и планировщик
    await post_news_job(context)
    await update.message.reply_text("Попытка постинга новостей завершена. Смотри логи.")

# --- НОВАЯ ФУНКЦИЯ ДЛЯ ПОХВАЛЫ (/praise) ---
async def praise_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Генерирует саркастическую 'похвалу' пользователю, на сообщение которого ответили."""
    # Проверка на reply и на то, что ответили не боту
    if (not update.message or not update.message.reply_to_message or
            not update.message.reply_to_message.from_user or
            update.message.reply_to_message.from_user.id == context.bot.id):
        await context.bot.send_message(chat_id=update.message.chat_id, text="Ответь этой командой на сообщение того, кого хочешь ПОХВАЛИТЬ (но не меня!).")
        return

    target_user = update.message.reply_to_message.from_user
    target_name = target_user.first_name or target_user.username or "этот достойный муж (или баба)"
    chat_id = update.message.chat_id
    user_name = update.message.from_user.first_name or "Главный Подхалим"

    logger.info(f"Пользователь '{user_name}' запросил похвалу для '{target_name}' (ID: {target_user.id}) в чате {chat_id}")

    # --- ПРОМПТ ДЛЯ ГЕНЕРАЦИИ "ПОХВАЛЫ" ---
    praise_prompt = (
        f"Ты - Попиздяка, саркастичный бот. Тебя попросили ПОХВАЛИТЬ пользователя по имени **{target_name}**. "
        f"Придумай **КОРОТКУЮ (1-3 предложения) НЕОДНОЗНАЧНУЮ 'ПОХВАЛУ'**. Она должна звучать формально положительно, но с явным подтекстом сарказма, иронии или скрытого стеба. Можно использовать немного мата для колорита. "
        f"Сделай так, чтобы человек не понял, похвалили его или обосрали.\n\n"
        f"ВАЖНО: Это должна быть именно кривая ПОХВАЛА, а не оскорбление. Начинай ответ с `🗿`.\n\n"
        f"Пример (для Васи): '🗿 О, Васян! Ты, блядь, существуешь! Это уже достижение, я считаю. Продолжай в том же духе (нет).'\n"
        f"Пример (для Лены): '🗿 Лена сегодня превзошла саму себя! Ее молчание в чате было просто божественным. Побольше бы так.'\n"
        f"Пример (для Димы): '🗿 Дима, твоя способность не понимать очевидные вещи просто поражает! Это редкий дар, береги его.'\n\n"
        f"Придумай подобную САРКАСТИЧНУЮ ПОХВАЛУ для **{target_name}**:"
    )
    # --- КОНЕЦ ПРОМПТА ---

    try:
        thinking_message = await context.bot.send_message(chat_id=chat_id, text=f"🗿 Ща попробую найти, за что похвалить этого вашего '{target_name}'...")
        # Используем текстовую модель (io.net или Gemini)
        messages_for_api = [{"role": "user", "content": praise_prompt}]
        praise_text = await _call_ionet_api( # ИЛИ model.generate_content_async
            messages=messages_for_api, model_id=IONET_TEXT_MODEL_ID, max_tokens=100, temperature=0.85
        ) or f"[Похвала для {target_name} не придумалась]"
        if not praise_text.startswith(("🗿", "[")): praise_text = "🗿 " + praise_text
        try: await context.bot.delete_message(chat_id=chat_id, message_id=thinking_message.message_id)
        except Exception: pass

        MAX_MESSAGE_LENGTH = 4096; # Обрезка
        if len(praise_text) > MAX_MESSAGE_LENGTH: praise_text = praise_text[:MAX_MESSAGE_LENGTH - 3] + "..."

        # Отправляем как ответ на команду, но упоминаем цель
        target_mention = target_user.mention_html() if target_user.username else f"<b>{target_name}</b>"
        final_text = f"Типа похвала для {target_mention}:\n\n{praise_text}"
        await context.bot.send_message(chat_id=chat_id, text=final_text, parse_mode='HTML')
        logger.info(f"Отправлена похвала для {target_name}.")
        # Запись для /retry (если нужна)
        # ... (можно добавить запись с type='praise')

    except Exception as e:
        logger.error(f"ПИЗДЕЦ при генерации похвалы для {target_name}: {e}", exc_info=True)
        try:
            if 'thinking_message' in locals(): await context.bot.delete_message(chat_id=chat_id, message_id=thinking_message.message_id)
        except Exception: pass
        await context.bot.send_message(chat_id=chat_id, text=f"Бля, {user_name}, не могу похвалить '{target_name}'. Он слишком идеален (нет). Ошибка: `{type(e).__name__}`.")

# --- КОНЕЦ ФУНКЦИИ /praise ---    

async def main() -> None:
    logger.info("Starting main()...")
    logger.info("Building Application...")
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Запуск фоновой задачи
    if application.job_queue:
        # Задача для рандомных высеров в тишине
        application.job_queue.run_repeating(check_inactivity_and_shitpost, interval=900, first=60)
        logger.info("Фоновая задача проверки неактивности запущена.")

        # --->>> ЗАПУСК ЗАДАЧИ НОВОСТЕЙ <<<---
        if GNEWS_API_KEY: # Запускаем, только если есть ключ
            application.job_queue.run_repeating(post_news_job, interval=NEWS_POST_INTERVAL, first=120) # Например, каждые 6 часов, первый раз через 2 мин
            logger.info(f"Фоновая задача постинга новостей запущена (каждые {NEWS_POST_INTERVAL/3600} ч).")
        else:
            logger.warning("Задача постинга новостей НЕ запущена (нет NEWSAPI_KEY).")
            # --->>> КОНЕЦ ЗАПУСКА ЗАДАЧИ НОВОСТЕЙ <<<---
    else:
        logger.warning("Не удалось получить job_queue, фоновые задачи не запущены!")

    # Добавляем обработчики команд
    application.add_handler(CommandHandler("maintenance_on", maintenance_on))
    application.add_handler(CommandHandler("maintenance_off", maintenance_off))
    application.add_handler(CommandHandler("analyze", analyze_chat))
    application.add_handler(CommandHandler("analyze_pic", analyze_pic))
    application.add_handler(CommandHandler("poem", generate_poem))
    application.add_handler(CommandHandler("prediction", get_prediction))
    application.add_handler(CommandHandler("pickup", get_pickup_line))
    application.add_handler(CommandHandler("pickup_line", get_pickup_line))
    application.add_handler(CommandHandler("roast", roast_user))
    application.add_handler(CommandHandler("retry", retry_analysis))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("post_news", force_post_news))
    # --->>> ДОБАВЛЯЕМ ПОХВАЛУ <<<---
    application.add_handler(CommandHandler("praise", praise_user)) # Команда /praise (в ответе)
    praise_pattern = r'(?i).*(?:бот|попиздяка).*(?:похвали|молодец|красавчик)\s+(?:его|ее|этого|эту).*'
    # Ловим ТОЛЬКО как ответ!
    application.add_handler(MessageHandler(filters.Regex(praise_pattern) & filters.TEXT & filters.REPLY & ~filters.COMMAND, praise_user))
    # --->>> КОНЕЦ ДОБАВЛЕНИЙ ДЛЯ ПОХВАЛЫ <<<---


    # --->>> ДОБАВЛЯЕМ РУССКИЕ АНАЛОГИ ДЛЯ ТЕХРАБОТ <<<---
    # Regex для ВКЛючения техработ
    maint_on_pattern = r'(?i).*(?:бот|попиздяка).*(?:техработ|ремонт|на ремонт|обслуживание|админ вкл).*'
    # Ловим ТОЛЬКО текст, БЕЗ команд, в ЛЮБОМ чате (проверка админа и ЛС будет ВНУТРИ функции)
    application.add_handler(MessageHandler(filters.Regex(maint_on_pattern) & filters.TEXT & ~filters.COMMAND, maintenance_on)) # Вызываем ту же функцию!

    # Regex для ВЫКЛючения техработ
    maint_off_pattern = r'(?i).*(?:бот|попиздяка).*(?:работай|работать|кончил|закончил|ремонт окончен|админ выкл).*'
    application.add_handler(MessageHandler(filters.Regex(maint_off_pattern) & filters.TEXT & ~filters.COMMAND, maintenance_off)) # Вызываем ту же функцию!
    # --->>> КОНЕЦ ДОБАВЛЕНИЙ <<<---

    # Добавляем обработчики русских фраз (вызывают ТЕ ЖЕ функции)
    # Можно добавить больше синонимов
    analyze_pattern = r'(?i).*(попиздяка|бот).*(анализ|анализируй|проанализируй|комментируй|обосри|скажи|мнение).*'
    application.add_handler(MessageHandler(filters.Regex(analyze_pattern) & filters.TEXT & ~filters.COMMAND, analyze_chat)) # Прямой вызов

    analyze_pic_pattern = r'(?i).*(попиздяка|бот).*(зацени|опиши|обосри|скажи про).*(пикч|картинк|фот|изображен|это).*'
    application.add_handler(MessageHandler(filters.Regex(analyze_pic_pattern) & filters.TEXT & filters.REPLY & ~filters.COMMAND, analyze_pic)) # Прямой вызов

    poem_pattern = r'(?i).*(?:бот|попиздяка).*(?:стих|стишок|поэма)\s+(?:про|для|об)\s+([А-Яа-яЁё\s\-]+)' # Оставили группу для имени
    application.add_handler(MessageHandler(filters.Regex(poem_pattern) & filters.TEXT & ~filters.COMMAND, generate_poem)) # Прямой вызов

    prediction_pattern = r'(?i).*(?:бот|попиздяка).*(?:предскажи|что ждет|прогноз|предсказание|напророчь).*'
    application.add_handler(MessageHandler(filters.Regex(prediction_pattern) & filters.TEXT & ~filters.COMMAND, get_prediction)) # Прямой вызов

    pickup_pattern = r'(?i).*(?:бот|попиздяка).*(?:подкат|пикап|склей|познакомься|замути).*'
    application.add_handler(MessageHandler(filters.Regex(pickup_pattern) & filters.TEXT & ~filters.COMMAND, get_pickup_line)) # Прямой вызов

    roast_pattern = r'(?i).*(?:бот|попиздяка).*(?:прожарь|зажарь|обосри|унизь)\s+(?:его|ее|этого|эту).*'
    application.add_handler(MessageHandler(filters.Regex(roast_pattern) & filters.TEXT & filters.REPLY & ~filters.COMMAND, roast_user)) # Прямой вызов

    retry_pattern = r'(?i).*(попиздяка|бот).*(переделай|повтори|перепиши|хуйня|другой вариант).*'
    application.add_handler(MessageHandler(filters.Regex(retry_pattern) & filters.TEXT & filters.REPLY & ~filters.COMMAND, retry_analysis)) # Прямой вызов

    help_pattern = r'(?i).*(попиздяка|попиздоний|бот).*(ты кто|кто ты|что умеешь|хелп|помощь|справка|команды).*'
    application.add_handler(MessageHandler(filters.Regex(help_pattern) & filters.TEXT & ~filters.COMMAND, help_command)) # Прямой вызов

    news_pattern = r'(?i).*(попиздяка|попиздоний|бот).*(новости|че там|мир).*'
    application.add_handler(MessageHandler(filters.Regex(news_pattern) & filters.TEXT & ~filters.COMMAND, force_post_news)) # Прямой вызов

    # Обработчик ответов боту (должен идти ПОСЛЕ regex для команд!)
    application.add_handler(MessageHandler(filters.TEXT & filters.REPLY & ~filters.COMMAND, reply_to_bot_handler))

    # --->>> ВОТ ЭТИ ПЯТЬ СТРОК НУЖНЫ <<<---
    # 1. Только для ТЕКСТА (без команд)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, store_message))
    # 2. Только для ФОТО
    application.add_handler(MessageHandler(filters.PHOTO, store_message))
    # 3. Только для СТИКЕРОВ
    application.add_handler(MessageHandler(filters.Sticker.ALL, store_message))
    # 4. Только для ВИДЕО
    application.add_handler(MessageHandler(filters.VIDEO, store_message))
    # 5. Только для ГОЛОСА
    application.add_handler(MessageHandler(filters.VOICE, store_message))
    # --->>> КОНЕЦ <<<---

    logger.info("Обработчики Telegram добавлены.")

    # Настройка и запуск Hypercorn + бота
    port = int(os.environ.get("PORT", 8080)); hypercorn_config = hypercorn.config.Config();
    hypercorn_config.bind = [f"0.0.0.0:{port}"]; hypercorn_config.worker_class = "asyncio"; hypercorn_config.shutdown_timeout = 60.0
    logger.info(f"Конфиг Hypercorn: {hypercorn_config.bind}, worker={hypercorn_config.worker_class}")
    logger.info("Запуск задач Hypercorn и Telegram бота...")
    shutdown_event = asyncio.Event(); bot_task = asyncio.create_task(run_bot_async(application), name="TelegramBotTask")
    server_task = asyncio.create_task(hypercorn_async_serve(app, hypercorn_config, shutdown_trigger=shutdown_event.wait), name="HypercornServerTask")

    # Ожидание и обработка завершения
    done, pending = await asyncio.wait([bot_task, server_task], return_when=asyncio.FIRST_COMPLETED)
    logger.warning(f"Задача завершилась! Done: {done}, Pending: {pending}")
    if server_task in pending: logger.info("Остановка Hypercorn..."); shutdown_event.set()
    logger.info("Отмена остальных задач..."); [task.cancel() for task in pending]
    await asyncio.gather(*pending, return_exceptions=True)
    for task in done: # Проверка ошибок
        logger.info(f"Проверка завершенной задачи: {task.get_name()}")
        try: await task
        except asyncio.CancelledError: logger.info(f"Задача {task.get_name()} отменена.")
        except Exception as e: logger.error(f"Задача {task.get_name()} не удалась: {e}", exc_info=True)
    logger.info("main() закончена.")

# --- Точка входа в скрипт ---
if __name__ == "__main__":
    logger.info(f"Запуск скрипта bot.py...")
    # Создаем .env шаблон, если надо
    if not os.path.exists('.env') and not os.getenv('RENDER'):
        logger.warning("Файл .env не найден...")
        try:
            with open('.env', 'w') as f: f.write(f"TELEGRAM_BOT_TOKEN=...\nIO_NET_API_KEY=...\nMONGO_DB_URL=...\n# MIR_CARD_NUMBER=...\n# TON_WALLET_ADDRESS=...\n# USDC_WALLET_ADDRESS=...\n# USDC_NETWORK=TRC20\n")
            logger.warning("Создан ШАБЛОН файла .env...")
        except Exception as e: logger.error(f"Не удалось создать шаблон .env: {e}")
    # Проверка ключей
    if not TELEGRAM_BOT_TOKEN or not IO_NET_API_KEY or not MONGO_DB_URL: logger.critical("ОТСУТСТВУЮТ КЛЮЧЕВЫЕ ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ!"); exit(1)
    # Запуск
    try: logger.info("Запускаю asyncio.run(main())..."); asyncio.run(main()); logger.info("asyncio.run(main()) завершен.")
    except Exception as e: logger.critical(f"КРИТИЧЕСКАЯ ОШИБКА: {e}", exc_info=True); exit(1)
    finally: logger.info("Скрипт bot.py завершает работу.")

# --- КОНЕЦ АБСОЛЮТНО ПОЛНОГО КОДА BOT.PY (AI.IO.NET ВЕРСИЯ - ФИНАЛ v2) ---