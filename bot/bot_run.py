import io
import logging
import asyncio
import traceback
import html
import json
from datetime import datetime
import openai

import telegram
from telegram import (
    Update,
    User,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackContext,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    AIORateLimiter,
    filters
)
from telegram.constants import ParseMode

import config
import openai_utils

import base64

# Инициализация базы данных и логгера
def init_database():
    from bot.database import Database
    db = Database()  # Создание экземпляра базы данных
    return db


logger = logging.getLogger(__name__)  # Создание логгера для текущего модуля

# Словари для хранения семафоров и задач пользователей
user_semaphores = {}
user_tasks = {}

# Сообщение с перечнем доступных команд для пользователя
HELP_MESSAGE = """Commands:
⚪ /retry – Восстановить последний ответ бота
⚪ /new – Начать новый диалог
⚪ /mode – Выберите режим чата
⚪ /settings – Показать настройки
⚪ /balance – Показать баланс
⚪ /help – Показать справку

🎨Генерируйте изображения из текстовых подсказок в <b>👩‍🎨 Artist</b> /mode
👥 Добавить бота в <b>групповой чат</b>: /help_group_chat
🎤 Вы можете отправить <b>Голосовые сообщения</b> вместо текста
"""

# Сообщение с инструкциями по добавлению бота в групповой чат
HELP_GROUP_CHAT_MESSAGE = """Вы можете добавить бота в любой <b>групповой чат</b>, чтобы помогать и развлекать его участников.!

Инструкции (см. <b>видео</b> ниже):
1. Добавьте бота в групповой чат
2. Сделайте его <b>администратором</b>, чтобы он мог видеть сообщения (все остальные права можно ограничить).
3. Ты потрясающий!

Чтобы получить ответ от бота в чате — @ <b>отметьте</b> его или <b>ответьте</b> на его сообщение.
Например: «{bot_username} напиши стихотворение о Telegram»
"""


def split_text_into_chunks(text, chunk_size):
    """
    Разбивает текст на части (chunk) заданного размера.

    Аргументы:
    text -- исходный текст, который нужно разбить
    chunk_size -- максимальный размер одного chunk

    Возвращает:
    Генератор, возвращающий части текста длиной не более chunk_size символов.
    """
    for i in range(0, len(text), chunk_size):
        yield text[i:i + chunk_size]


async def register_user_if_not_exists(update: Update, context: CallbackContext, user: User):
    """
    Регистрирует пользователя в базе данных, если он ещё не зарегистрирован.

    Аргументы:
    update -- объект Update, содержащий данные о текущем обновлении
    context -- контекст, передаваемый в колбэк функции
    user -- объект User, представляющий текущего пользователя
    """
    # Проверка наличия пользователя в базе данных
    if not db.check_if_user_exists(user.id):
        # Если пользователь не существует, добавляем его в базу данных
        db.add_new_user(
            user.id,
            update.message.chat_id,
            username=user.username,
            first_name=user.first_name,
            last_name=user.last_name
        )
        db.start_new_dialog(user.id)  # Начинаем новый диалог для пользователя

    # Проверка наличия активного диалога для пользователя
    if db.get_user_attribute(user.id, "current_dialog_id") is None:
        db.start_new_dialog(user.id)

    # Инициализация семафора для пользователя, если он отсутствует
    if user.id not in user_semaphores:
        user_semaphores[user.id] = asyncio.Semaphore(1)

    # Установка модели по умолчанию для пользователя, если она не указана
    if db.get_user_attribute(user.id, "current_model") is None:
        db.set_user_attribute(user.id, "current_model", config.models["available_text_models"][0])

    # Проверка обратной совместимости для поля n_used_tokens
    n_used_tokens = db.get_user_attribute(user.id, "n_used_tokens")
    if isinstance(n_used_tokens, int) or isinstance(n_used_tokens, float):  # старый формат
        new_n_used_tokens = {
            "gpt-3.5-turbo": {
                "n_input_tokens": 0,
                "n_output_tokens": n_used_tokens
            }
        }
        db.set_user_attribute(user.id, "n_used_tokens", new_n_used_tokens)

    # Инициализация поля для транскрибированных секунд голосовых сообщений
    if db.get_user_attribute(user.id, "n_transcribed_seconds") is None:
        db.set_user_attribute(user.id, "n_transcribed_seconds", 0.0)

    # Инициализация поля для сгенерированных изображений
    if db.get_user_attribute(user.id, "n_generated_images") is None:
        db.set_user_attribute(user.id, "n_generated_images", 0)


async def is_bot_mentioned(update: Update, context: CallbackContext):
    """
    Проверяет, был ли упомянут бот в сообщении.

    Аргументы:
    update -- объект Update, содержащий данные о текущем обновлении
    context -- контекст, передаваемый в колбэк функции

    Возвращает:
    True, если бот был упомянут, иначе False.
    """
    try:
        message = update.message

        # Если сообщение в приватном чате, бот всегда считается упомянутым
        if message.chat.type == "private":
            return True

        # Проверка на наличие упоминания бота по username в тексте сообщения
        if message.text is not None and ("@" + context.bot.username) in message.text:
            return True

        # Проверка, является ли сообщение ответом на сообщение бота
        if message.reply_to_message is not None:
            if message.reply_to_message.from_user.id == context.bot.id:
                return True
    except:
        # В случае исключения считаем, что бот упомянут
        return True
    else:
        return False


async def start_handle(update: Update, context: CallbackContext):
    """
    Обрабатывает команду /start, регистрирует пользователя и отправляет приветственное сообщение.

    Аргументы:
    update -- объект Update, содержащий данные о текущем обновлении
    context -- контекст, передаваемый в колбэк функции
    """
    # Регистрация пользователя, если он не существует
    await register_user_if_not_exists(update, context, update.message.from_user)
    user_id = update.message.from_user.id

    # Обновление времени последнего взаимодействия пользователя
    db.set_user_attribute(user_id, "last_interaction", datetime.now())
    db.start_new_dialog(user_id)  # Начало нового диалога

    # Формирование приветственного сообщения
    reply_text = "Hi! I'm <b>ChatGPT</b> bot implemented with OpenAI API 🤖\n\n"
    reply_text += HELP_MESSAGE

    # Отправка приветственного сообщения пользователю
    await update.message.reply_text(reply_text, parse_mode=ParseMode.HTML)
    await show_chat_modes_handle(update, context)


async def help_handle(update: Update, context: CallbackContext):
    """
    Обрабатывает команду /help, регистрирует пользователя и отправляет сообщение с доступными командами.

    Аргументы:
    update -- объект Update, содержащий данные о текущем обновлении
    context -- контекст, передаваемый в колбэк функции
    """
    await register_user_if_not_exists(update, context, update.message.from_user)  # Регистрация пользователя
    user_id = update.message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())  # Обновление времени последнего взаимодействия
    await update.message.reply_text(HELP_MESSAGE, parse_mode=ParseMode.HTML)  # Отправка сообщения с командами


async def help_group_chat_handle(update: Update, context: CallbackContext):
    """
    Обрабатывает команду /help_group_chat, регистрирует пользователя и отправляет инструкции по добавлению бота в групповой чат.

    Аргументы:
    update -- объект Update, содержащий данные о текущем обновлении
    context -- контекст, передаваемый в колбэк функции
    """
    await register_user_if_not_exists(update, context, update.message.from_user)  # Регистрация пользователя
    user_id = update.message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())  # Обновление времени последнего взаимодействия

    # Формирование сообщения с инструкциями
    text = HELP_GROUP_CHAT_MESSAGE.format(bot_username="@" + context.bot.username)

    # Отправка сообщения с инструкциями и видео
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)
    await update.message.reply_video(config.help_group_chat_video_path)


async def retry_handle(update: Update, context: CallbackContext):
    """
    Обрабатывает команду /retry, повторяет последний запрос пользователя.

    Аргументы:
    update -- объект Update, содержащий данные о текущем обновлении
    context -- контекст, передаваемый в колбэк функции
    """
    await register_user_if_not_exists(update, context, update.message.from_user)  # Регистрация пользователя
    if await is_previous_message_not_answered_yet(update,
                                                  context): return  # Проверка, был ли предыдущий запрос обработан

    user_id = update.message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())  # Обновление времени последнего взаимодействия

    # Получение сообщений диалога пользователя
    dialog_messages = db.get_dialog_messages(user_id, dialog_id=None)
    if len(dialog_messages) == 0:
        # Если сообщений нет, отправляется соответствующее сообщение
        await update.message.reply_text("Нет сообщения для повторной попытки 🤷‍♂️")
        return

    # Извлечение последнего сообщения из диалога
    last_dialog_message = dialog_messages.pop()
    db.set_dialog_messages(user_id, dialog_messages,
                           dialog_id=None)  # Удаление последнего сообщения из контекста диалога

    # Повторная обработка последнего сообщения
    await message_handle(update, context, message=last_dialog_message["user"], use_new_dialog_timeout=False)


async def _vision_message_handle_fn(
        update: Update, context: CallbackContext, use_new_dialog_timeout: bool = True
):
    """
    Обрабатывает сообщение с изображением для моделей, поддерживающих обработку изображений (gpt-4-vision-preview и gpt-4o).

    Аргументы:
    update -- объект Update, содержащий данные о текущем обновлении
    context -- контекст, передаваемый в колбэк функции
    use_new_dialog_timeout -- флаг, указывающий, нужно ли проверять тайм-аут для нового диалога
    """
    logger.info('_vision_message_handle_fn')  # Логирование начала обработки
    user_id = update.message.from_user.id  # Идентификатор пользователя
    current_model = db.get_user_attribute(user_id, "current_model")  # Текущая модель, используемая пользователем

    # Проверка, поддерживает ли текущая модель обработку изображений
    if current_model != "gpt-4-vision-preview" and current_model != "gpt-4o":
        await update.message.reply_text(
            "🥲 Обработка изображений доступна только для моделей <b>gpt-4-vision-preview</b> и <b>gpt-4o</b>. Пожалуйста, измените настройки в /settings",
            parse_mode=ParseMode.HTML,
        )
        return

    chat_mode = db.get_user_attribute(user_id, "current_chat_mode")  # Текущий режим чата пользователя

    # Проверка тайм-аута для нового диалога
    if use_new_dialog_timeout:
        last_interaction = db.get_user_attribute(user_id, "last_interaction")
        if (datetime.now() - last_interaction).seconds > config.new_dialog_timeout and len(
                db.get_dialog_messages(user_id)) > 0:
            db.start_new_dialog(user_id)  # Начинаем новый диалог при истечении тайм-аута
            await update.message.reply_text(
                f"Начинаем новый диалог из-за тайм-аута (<b>{config.chat_modes[chat_mode]['name']}</b> режим) ✅",
                parse_mode=ParseMode.HTML)

    db.set_user_attribute(user_id, "last_interaction", datetime.now())  # Обновляем время последнего взаимодействия

    buf = None
    if update.message.effective_attachment:
        photo = update.message.effective_attachment[-1]  # Получаем последнюю фотографию из вложений
        photo_file = await context.bot.get_file(photo.file_id)  # Загружаем файл фотографии

        # Сохраняем файл в памяти, а не на диске
        buf = io.BytesIO()
        await photo_file.download_to_memory(buf)
        buf.name = "image.jpg"  # Требуется указание расширения файла
        buf.seek(0)  # Перемещаем курсор в начало буфера

    # В случае ошибки CancelledError
    n_input_tokens, n_output_tokens = 0, 0

    try:
        # Отправляем пользователю сообщение-заполнитель
        placeholder_message = await update.message.reply_text("...")
        message = update.message.caption or update.message.text or ''

        # Отправляем действие "печатает"
        await update.message.chat.send_action(action="typing")

        dialog_messages = db.get_dialog_messages(user_id, dialog_id=None)  # Получаем сообщения текущего диалога
        parse_mode = {"html": ParseMode.HTML, "markdown": ParseMode.MARKDOWN}[
            config.chat_modes[chat_mode]["parse_mode"]]  # Определяем режим парсинга

        chatgpt_instance = openai_utils.ChatGPT(model=current_model)
        if config.enable_message_streaming:
            # Если включен потоковый режим, отправляем сообщение с изображением с потоковой обработкой
            gen = chatgpt_instance.send_vision_message_stream(
                message,
                dialog_messages=dialog_messages,
                image_buffer=buf,
                chat_mode=chat_mode,
            )
        else:
            # Иначе отправляем сообщение с изображением обычным способом
            (
                answer,
                (n_input_tokens, n_output_tokens),
                n_first_dialog_messages_removed,
            ) = await chatgpt_instance.send_vision_message(
                message,
                dialog_messages=dialog_messages,
                image_buffer=buf,
                chat_mode=chat_mode,
            )

            async def fake_gen():
                yield "finished", answer, (
                    n_input_tokens,
                    n_output_tokens,
                ), n_first_dialog_messages_removed

            gen = fake_gen()  # Генерация фейкового генератора для работы с синхронным кодом

        prev_answer = ""
        async for gen_item in gen:
            (
                status,
                answer,
                (n_input_tokens, n_output_tokens),
                n_first_dialog_messages_removed,
            ) = gen_item

            answer = answer[:4096]  # Ограничение на длину сообщения в Telegram

            # Обновляем сообщение, только если добавилось более 100 символов или генерация завершена
            if abs(len(answer) - len(prev_answer)) < 100 and status != "finished":
                continue

            try:
                await context.bot.edit_message_text(
                    answer,
                    chat_id=placeholder_message.chat_id,
                    message_id=placeholder_message.message_id,
                    parse_mode=parse_mode,
                )
            except telegram.error.BadRequest as e:
                if str(e).startswith("Message is not modified"):
                    continue
                else:
                    await context.bot.edit_message_text(
                        answer,
                        chat_id=placeholder_message.chat_id,
                        message_id=placeholder_message.message_id,
                    )

            await asyncio.sleep(0.01)  # Небольшая пауза для избежания ограничения на количество запросов

            prev_answer = answer

        answer = ""
        # Обновляем данные пользователя
        if buf is not None:
            base_image = base64.b64encode(buf.getvalue()).decode("utf-8")
            new_dialog_message = {"user": [
                {
                    "type": "text",
                    "text": message,
                },
                {
                    "type": "image",
                    "image": base_image,
                }
            ]
                , "bot": answer, "date": datetime.now()}
        else:
            new_dialog_message = {"user": [{"type": "text", "text": message}], "bot": answer, "date": datetime.now()}

        db.set_dialog_messages(
            user_id,
            db.get_dialog_messages(user_id, dialog_id=None) + [new_dialog_message],
            dialog_id=None
        )

        db.update_n_used_tokens(user_id, current_model, n_input_tokens,
                                n_output_tokens)  # Обновляем количество использованных токенов

    except asyncio.CancelledError:
        # В случае ошибки отмены, обновляем количество использованных токенов
        db.update_n_used_tokens(user_id, current_model, n_input_tokens, n_output_tokens)
        raise

    except Exception as e:
        error_text = f"Что-то пошло не так во время обработки. Причина: {e}"
        logger.error(error_text)
        await update.message.reply_text(error_text)
        return


async def unsupport_message_handle(update: Update, context: CallbackContext, message=None):
    """
    Обрабатывает неподдерживаемые типы сообщений (файлы и видео).

    Аргументы:
    update -- объект Update, содержащий данные о текущем обновлении
    context -- контекст, передаваемый в колбэк функции
    message -- текстовое сообщение пользователя (по умолчанию None)
    """
    error_text = "Я не могу читать файлы или видео. Отправьте картинку в обычном режиме (Быстрый режим)."
    logger.error(error_text)
    await update.message.reply_text(error_text)
    return


async def message_handle(update: Update, context: CallbackContext, message=None, use_new_dialog_timeout=True):
    """
    Обрабатывает текстовые сообщения от пользователя, включая обработку изображений в некоторых режимах.

    Аргументы:
    update -- объект Update, содержащий данные о текущем обновлении
    context -- контекст, передаваемый в колбэк функции
    message -- текстовое сообщение пользователя (по умолчанию None)
    use_new_dialog_timeout -- флаг, указывающий, нужно ли проверять тайм-аут для нового диалога (по умолчанию True)
    """
    # Проверяем, упомянут ли бот (актуально для групповых чатов)
    if not await is_bot_mentioned(update, context):
        return

    # Проверяем, было ли сообщение отредактировано
    if update.edited_message is not None:
        await edited_message_handle(update, context)
        return

    _message = message or update.message.text  # Используем переданное сообщение или текст из update

    # Удаляем упоминание бота (для групповых чатов)
    if update.message.chat.type != "private":
        _message = _message.replace("@" + context.bot.username, "").strip()

    await register_user_if_not_exists(update, context,
                                      update.message.from_user)  # Регистрация пользователя, если он ещё не существует
    if await is_previous_message_not_answered_yet(update,
                                                  context): return  # Проверка, был ли предыдущий запрос обработан

    user_id = update.message.from_user.id
    chat_mode = db.get_user_attribute(user_id, "current_chat_mode")  # Получаем текущий режим чата пользователя

    # Если режим чата "artist", обрабатываем сообщение как запрос на генерацию изображения
    if chat_mode == "artist":
        await generate_image_handle(update, context, message=message)
        return

    current_model = db.get_user_attribute(user_id, "current_model")  # Получаем текущую модель пользователя

    async def message_handle_fn():
        """
        Вспомогательная функция для обработки текстовых сообщений.
        """
        answer = ''
        n_first_dialog_messages_removed = 0
        # Проверка тайм-аута для нового диалога
        if use_new_dialog_timeout:
            last_interaction = db.get_user_attribute(user_id, "last_interaction")
            if (datetime.now() - last_interaction).seconds > config.new_dialog_timeout and len(
                    db.get_dialog_messages(user_id)) > 0:
                db.start_new_dialog(user_id)
                await update.message.reply_text(
                    f"Начинаем новый диалог из-за тайм-аута (<b>{config.chat_modes[chat_mode]['name']}</b> режим) ✅",
                    parse_mode=ParseMode.HTML)
        db.set_user_attribute(user_id, "last_interaction", datetime.now())  # Обновляем время последнего взаимодействия

        # В случае ошибки CancelledError
        n_input_tokens, n_output_tokens = 0, 0

        try:
            # Отправляем пользователю сообщение-заполнитель
            placeholder_message = await update.message.reply_text("...")

            # Отправляем действие "печатает"
            await update.message.chat.send_action(action="typing")

            if _message is None or len(_message) == 0:
                await update.message.reply_text("🥲 Вы отправили <b>пустое сообщение</b>. Попробуйте снова!",
                                                parse_mode=ParseMode.HTML)
                return

            dialog_messages = db.get_dialog_messages(user_id, dialog_id=None)  # Получаем сообщения текущего диалога
            parse_mode = {
                "html": ParseMode.HTML,
                "markdown": ParseMode.MARKDOWN
            }[config.chat_modes[chat_mode]["parse_mode"]]  # Определяем режим парсинга

            chatgpt_instance = openai_utils.ChatGPT(model=current_model)
            if config.enable_message_streaming:
                # Если включен потоковый режим, отправляем сообщение с потоковой обработкой
                gen = chatgpt_instance.send_message_stream(_message, dialog_messages=dialog_messages,
                                                           chat_mode=chat_mode)
            else:
                # Иначе отправляем сообщение обычным способом
                answer, (
                    n_input_tokens,
                    n_output_tokens), n_first_dialog_messages_removed = await chatgpt_instance.send_message(
                    _message,
                    dialog_messages=dialog_messages,
                    chat_mode=chat_mode
                )

                async def fake_gen():
                    yield "finished", answer, (n_input_tokens, n_output_tokens), n_first_dialog_messages_removed

                gen = fake_gen()  # Генерация фейкового генератора для работы с синхронным кодом

            prev_answer = ""

            async for gen_item in gen:
                status, answer, (n_input_tokens, n_output_tokens), n_first_dialog_messages_removed = gen_item

                answer = answer[:4096]  # Ограничение на длину сообщения в Telegram

                # Обновляем сообщение, только если добавилось более 100 символов или генерация завершена
                if abs(len(answer) - len(prev_answer)) < 100 and status != "finished":
                    continue

                try:
                    await context.bot.edit_message_text(answer, chat_id=placeholder_message.chat_id,
                                                        message_id=placeholder_message.message_id,
                                                        parse_mode=parse_mode)
                except telegram.error.BadRequest as e:
                    if str(e).startswith("Message is not modified"):
                        continue
                    else:
                        await context.bot.edit_message_text(answer, chat_id=placeholder_message.chat_id,
                                                            message_id=placeholder_message.message_id)

                await asyncio.sleep(0.01)  # Небольшая пауза для избежания ограничения на количество запросов

                prev_answer = answer

            # Обновляем данные пользователя
            new_dialog_message = {"user": [{"type": "text", "text": _message}], "bot": answer, "date": datetime.now()}

            db.set_dialog_messages(
                user_id,
                db.get_dialog_messages(user_id, dialog_id=None) + [new_dialog_message],
                dialog_id=None
            )

            db.update_n_used_tokens(user_id, current_model, n_input_tokens,
                                    n_output_tokens)  # Обновляем количество использованных токенов

        except asyncio.CancelledError:
            # В случае ошибки отмены, обновляем количество использованных токенов
            db.update_n_used_tokens(user_id, current_model, n_input_tokens, n_output_tokens)
            raise

        except Exception as e:
            error_text = f"Что-то пошло не так во время обработки. Причина: {e}"
            logger.error(error_text)
            await update.message.reply_text(error_text)
            return

        # Отправляем сообщение, если некоторые сообщения были удалены из контекста
        if n_first_dialog_messages_removed > 0:
            if n_first_dialog_messages_removed == 1:
                text = "✍️ <i>Примечание:</i> Ваш текущий диалог слишком длинный, поэтому ваше <b>первое сообщение</b> было удалено из контекста.\n Отправьте команду /new, чтобы начать новый диалог"
            else:
                text = f"✍️ <i>Примечание:</i> Ваш текущий диалог слишком длинный, поэтому <b>{n_first_dialog_messages_removed} первых сообщений</b> были удалены из контекста.\n Отправьте команду /new, чтобы начать новый диалог"
            await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    async with user_semaphores[user_id]:  # Блокируем выполнение для пользователя до завершения текущей задачи
        if current_model == "gpt-4-vision-preview" or current_model == "gpt-4o" or update.message.photo is not None and len(
                update.message.photo) > 0:

            logger.error(current_model)
            # Проверка текущей модели

            if current_model != "gpt-4o" and current_model != "gpt-4-vision-preview":
                current_model = "gpt-4o"
                db.set_user_attribute(user_id, "current_model", "gpt-4o")
            task = asyncio.create_task(
                _vision_message_handle_fn(update, context, use_new_dialog_timeout=use_new_dialog_timeout)
            )
        else:
            task = asyncio.create_task(
                message_handle_fn()
            )

        user_tasks[user_id] = task  # Сохраняем задачу для пользователя

        try:
            await task  # Ожидаем завершения задачи
        except asyncio.CancelledError:
            await update.message.reply_text("✅ Отменено", parse_mode=ParseMode.HTML)
        else:
            pass
        finally:
            if user_id in user_tasks:
                del user_tasks[user_id]  # Удаляем задачу из списка активных задач


async def is_previous_message_not_answered_yet(update: Update, context: CallbackContext):
    """
    Проверяет, не остался ли предыдущий запрос пользователя без ответа.

    Если предыдущее сообщение пользователя еще не обработано (находится в процессе обработки),
    отправляется уведомление с просьбой подождать и функция возвращает True.
    В противном случае возвращается False.

    Аргументы:
    - update: объект Update, представляющий текущее обновление (сообщение).
    - context: объект CallbackContext, предоставляющий контекст выполнения.

    Возвращает:
    - bool: True, если предыдущее сообщение еще не обработано; False в противном случае.
    """
    await register_user_if_not_exists(update, context, update.message.from_user)

    user_id = update.message.from_user.id
    if user_semaphores[user_id].locked():
        text = "⏳ Пожалуйста, <b>дождитесь</b> ответа на предыдущее сообщение.\n"
        text += "Или ты можешь отменить /cancel"
        await update.message.reply_text(text, reply_to_message_id=update.message.id, parse_mode=ParseMode.HTML)
        return True
    else:
        return False


async def voice_message_handle(update: Update, context: CallbackContext):
    """
    Обрабатывает голосовое сообщение, транскрибирует его и отправляет текстовую версию.

    Если бот был упомянут (в случае группового чата), голосовое сообщение загружается,
    транскрибируется с помощью OpenAI, и текстовый результат отправляется в чат.

    Аргументы:
    - update: объект Update, представляющий текущее обновление (голосовое сообщение).
    - context: объект CallbackContext, предоставляющий контекст выполнения.
    """
    # Проверяет, упомянут ли бот (для групповых чатов)
    if not await is_bot_mentioned(update, context):
        return

    await register_user_if_not_exists(update, context, update.message.from_user)
    if await is_previous_message_not_answered_yet(update, context):
        return

    user_id = update.message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    # Получаем голосовое сообщение и файл
    voice = update.message.voice
    voice_file = await context.bot.get_file(voice.file_id)

    # Загружаем файл в память (без сохранения на диск)
    buf = io.BytesIO()
    await voice_file.download_to_memory(buf)
    buf.name = "voice.oga"  # Требуется указание расширения файла
    buf.seek(0)  # Перемещаем курсор в начало буфера

    # Транскрибируем аудио
    transcribed_text = await openai_utils.transcribe_audio(buf)
    text = f"🎤: <i>{transcribed_text}</i>"
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    # Обновляем количество транскрибированных секунд
    db.set_user_attribute(user_id, "n_transcribed_seconds",
                          voice.duration + db.get_user_attribute(user_id, "n_transcribed_seconds"))

    # Передаем транскрибированный текст для дальнейшей обработки
    await message_handle(update, context, message=transcribed_text)


async def generate_image_handle(update: Update, context: CallbackContext, message=None):
    """
    Обрабатывает запрос на генерацию изображения на основе текстового сообщения пользователя.

    Генерирует изображение с помощью OpenAI и отправляет его пользователю.
    Если запрос нарушает политику использования OpenAI, отправляется сообщение с отказом.

    Аргументы:
    - update: объект Update, представляющий текущее обновление.
    - context: объект CallbackContext, предоставляющий контекст выполнения.
    - message: необязательный аргумент, содержащий текст запроса для генерации изображения.
    """
    await register_user_if_not_exists(update, context, update.message.from_user)
    if await is_previous_message_not_answered_yet(update, context):
        return

    user_id = update.message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    # Устанавливаем статус "загрузка фото"
    await update.message.chat.send_action(action="upload_photo")

    message = message or update.message.text

    try:
        # Генерируем изображения с помощью OpenAI
        image_urls = await openai_utils.generate_images(message, n_images=config.return_n_generated_images,
                                                        size=config.image_size)
    except openai.error.InvalidRequestError as e:
        if str(e).startswith("Your request was rejected as a result of our safety system"):
            text = "🥲 Your request <b>doesn't comply</b> with OpenAI's usage policies.\nWhat did you write there, huh?"
            await update.message.reply_text(text, parse_mode=ParseMode.HTML)
            return
        else:
            raise

    # Обновляем статистику использования токенов
    db.set_user_attribute(user_id, "n_generated_images",
                          config.return_n_generated_images + db.get_user_attribute(user_id, "n_generated_images"))

    # Отправляем сгенерированные изображения
    for i, image_url in enumerate(image_urls):
        await update.message.chat.send_action(action="upload_photo")
        await update.message.reply_photo(image_url, parse_mode=ParseMode.HTML)


async def new_dialog_handle(update: Update, context: CallbackContext):
    """
    Начинает новый диалог с пользователем, сбрасывая текущее состояние диалога.

    Устанавливает модель GPT-3.5-turbo в качестве текущей и отправляет приветственное сообщение.

    Аргументы:
    - update: объект Update, представляющий текущее обновление.
    - context: объект CallbackContext, предоставляющий контекст выполнения.
    """
    await register_user_if_not_exists(update, context, update.message.from_user)
    if await is_previous_message_not_answered_yet(update, context):
        return

    user_id = update.message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())
    db.set_user_attribute(user_id, "current_model", "gpt-3.5-turbo")

    # Начинаем новый диалог
    db.start_new_dialog(user_id)
    await update.message.reply_text("Начало нового диалога ✅")

    # Отправляем приветственное сообщение для выбранного режима общения
    chat_mode = db.get_user_attribute(user_id, "current_chat_mode")
    await update.message.reply_text(f"{config.chat_modes[chat_mode]['welcome_message']}", parse_mode=ParseMode.HTML)


async def cancel_handle(update: Update, context: CallbackContext):
    """
    Отменяет текущую задачу пользователя, если таковая существует.

    Если задачи нет, отправляет сообщение "Nothing to cancel".

    Аргументы:
    - update: объект Update, представляющий текущее обновление.
    - context: объект CallbackContext, предоставляющий контекст выполнения.
    """
    await register_user_if_not_exists(update, context, update.message.from_user)

    user_id = update.message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    if user_id in user_tasks:
        task = user_tasks[user_id]
        task.cancel()  # Отменяем текущую задачу
    else:
        await update.message.reply_text("<i>Nothing to cancel...</i>", parse_mode=ParseMode.HTML)


def get_chat_mode_menu(page_index: int):
    """
    Создает меню выбора режима общения с ботом, с поддержкой постраничной навигации.

    Аргументы:
    - page_index: индекс текущей страницы с режимами общения.

    Возвращает:
    - text: текст сообщения с инструкциями по выбору режима общения.
    - reply_markup: объект InlineKeyboardMarkup, содержащий кнопки для выбора режимов и навигации.
    """
    n_chat_modes_per_page = config.n_chat_modes_per_page
    text = f"Select <b>chat mode</b> ({len(config.chat_modes)} modes available):"

    # Кнопки выбора режима
    chat_mode_keys = list(config.chat_modes.keys())
    page_chat_mode_keys = chat_mode_keys[page_index * n_chat_modes_per_page:(page_index + 1) * n_chat_modes_per_page]

    keyboard = []
    for chat_mode_key in page_chat_mode_keys:
        name = config.chat_modes[chat_mode_key]["name"]
        keyboard.append([InlineKeyboardButton(name, callback_data=f"set_chat_mode|{chat_mode_key}")])

    # Пагинация (переход между страницами)
    if len(chat_mode_keys) > n_chat_modes_per_page:
        is_first_page = (page_index == 0)
        is_last_page = ((page_index + 1) * n_chat_modes_per_page >= len(chat_mode_keys))

        if is_first_page:
            keyboard.append([
                InlineKeyboardButton("»", callback_data=f"show_chat_modes|{page_index + 1}")
            ])
        elif is_last_page:
            keyboard.append([
                InlineKeyboardButton("«", callback_data=f"show_chat_modes|{page_index - 1}"),
            ])
        else:
            keyboard.append([
                InlineKeyboardButton("«", callback_data=f"show_chat_modes|{page_index - 1}"),
                InlineKeyboardButton("»", callback_data=f"show_chat_modes|{page_index + 1}")
            ])

    reply_markup = InlineKeyboardMarkup(keyboard)

    return text, reply_markup


async def show_chat_modes_handle(update: Update, context: CallbackContext):
    """
    Обрабатывает команду для отображения меню выбора режима общения с поддержкой пагинации.

    Аргументы:
    - update: объект Update, представляющий текущее обновление.
    - context: объект CallbackContext, предоставляющий контекст выполнения.

    Описание:
    - Проверяет, зарегистрирован ли пользователь. Если нет, регистрирует.
    - Проверяет, был ли предыдущий запрос пользователя обработан.
    - Отправляет пользователю меню выбора режима общения с начальной страницей (индекс 0).
    """
    await register_user_if_not_exists(update, context, update.message.from_user)
    if await is_previous_message_not_answered_yet(update, context):
        return

    user_id = update.message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    text, reply_markup = get_chat_mode_menu(0)
    await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)


async def show_chat_modes_callback_handle(update: Update, context: CallbackContext):
    """
    Обрабатывает нажатие кнопок навигации в меню выбора режимов общения.

    Аргументы:
    - update: объект Update, представляющий текущее обновление (нажатие кнопки).
    - context: объект CallbackContext, предоставляющий контекст выполнения.

    Описание:
    - Проверяет, зарегистрирован ли пользователь. Если нет, регистрирует.
    - Проверяет, был ли предыдущий запрос пользователя обработан.
    - Определяет индекс страницы для отображения и обновляет меню выбора режима общения.
    """
    await register_user_if_not_exists(update.callback_query, context, update.callback_query.from_user)
    if await is_previous_message_not_answered_yet(update.callback_query, context):
        return

    user_id = update.callback_query.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    query = update.callback_query
    await query.answer()

    # Определяем индекс страницы для отображения
    page_index = int(query.data.split("|")[1])
    if page_index < 0:
        return

    text, reply_markup = get_chat_mode_menu(page_index)
    try:
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    except telegram.error.BadRequest as e:
        # Если сообщение не было изменено (Message is not modified), ничего не делаем
        if str(e).startswith("Message is not modified"):
            pass


async def set_chat_mode_handle(update: Update, context: CallbackContext):
    """
    Обрабатывает выбор режима общения пользователя из меню.

    Аргументы:
    - update: объект Update, представляющий текущее обновление (нажатие кнопки).
    - context: объект CallbackContext, предоставляющий контекст выполнения.

    Описание:
    - Проверяет, зарегистрирован ли пользователь. Если нет, регистрирует.
    - Устанавливает выбранный режим общения и начинает новый диалог.
    - Отправляет пользователю приветственное сообщение для выбранного режима.
    """
    await register_user_if_not_exists(update.callback_query, context, update.callback_query.from_user)
    user_id = update.callback_query.from_user.id

    query = update.callback_query
    await query.answer()

    # Устанавливаем выбранный режим общения
    chat_mode = query.data.split("|")[1]
    db.set_user_attribute(user_id, "current_chat_mode", chat_mode)
    db.start_new_dialog(user_id)

    await context.bot.send_message(
        update.callback_query.message.chat.id,
        f"{config.chat_modes[chat_mode]['welcome_message']}",
        parse_mode=ParseMode.HTML
    )


def get_settings_menu(user_id: int):
    """
    Создает меню настроек для выбора модели.

    Аргументы:
    - user_id: идентификатор пользователя.

    Возвращает:
    - text: текст сообщения с текущими настройками и доступными моделями.
    - reply_markup: объект InlineKeyboardMarkup, содержащий кнопки для выбора модели.
    """
    current_model = db.get_user_attribute(user_id, "current_model")
    text = config.models["info"][current_model]["description"]

    text += "\n\n"
    score_dict = config.models["info"][current_model]["scores"]
    for score_key, score_value in score_dict.items():
        text += "🟢" * score_value + "⚪️" * (5 - score_value) + f" – {score_key}\n\n"

    text += "\nВыберите <b>модель</b>:"

    # Кнопки для выбора модели
    buttons = []
    for model_key in config.models["available_text_models"]:
        title = config.models["info"][model_key]["name"]
        if model_key == current_model:
            title = "✅ " + title

        buttons.append(
            InlineKeyboardButton(title, callback_data=f"set_settings|{model_key}")
        )
    # Разбиваем кнопки на строки по одной кнопке в каждой
    rows = [[button] for button in buttons]
    # Создаем клавиатуру
    reply_markup = InlineKeyboardMarkup(rows)

    return text, reply_markup


async def settings_handle(update: Update, context: CallbackContext):
    """
    Отправляет пользователю меню настроек для выбора модели.

    Аргументы:
    - update: объект Update, представляющий текущее обновление.
    - context: объект CallbackContext, предоставляющий контекст выполнения.

    Описание:
    - Проверяет, зарегистрирован ли пользователь. Если нет, регистрирует.
    - Отправляет пользователю текст с текущими настройками и кнопки для выбора модели.
    """
    await register_user_if_not_exists(update, context, update.message.from_user)
    if await is_previous_message_not_answered_yet(update, context):
        return

    user_id = update.message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    text, reply_markup = get_settings_menu(user_id)
    await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)


async def set_settings_handle(update: Update, context: CallbackContext):
    """
    Обрабатывает выбор модели из меню настроек.

    Аргументы:
    - update: объект Update, представляющий текущее обновление (нажатие кнопки).
    - context: объект CallbackContext, предоставляющий контекст выполнения.

    Описание:
    - Проверяет, зарегистрирован ли пользователь. Если нет, регистрирует.
    - Устанавливает выбранную модель и начинает новый диалог.
    - Обновляет меню настроек с новой выбранной моделью.
    """
    await register_user_if_not_exists(update.callback_query, context, update.callback_query.from_user)
    user_id = update.callback_query.from_user.id

    query = update.callback_query
    await query.answer()

    _, model_key = query.data.split("|")
    db.set_user_attribute(user_id, "current_model", model_key)
    db.start_new_dialog(user_id)

    text, reply_markup = get_settings_menu(user_id)
    try:
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    except telegram.error.BadRequest as e:
        # Если сообщение не было изменено (Message is not modified), ничего не делаем
        if str(e).startswith("Message is not modified"):
            pass


async def show_balance_handle(update: Update, context: CallbackContext):
    """
    Показывает пользователю статистику баланса, включая расходы на токены и изображения.

    Аргументы:
    - update: объект Update, представляющий текущее обновление.
    - context: объект CallbackContext, предоставляющий контекст выполнения.

    Описание:
    - Проверяет, зарегистрирован ли пользователь. Если нет, регистрирует.
    - Считает и отображает общую статистику расходов, включая использование токенов и генерацию изображений.
    """
    await register_user_if_not_exists(update, context, update.message.from_user)

    user_id = update.message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    # Подсчет общей статистики использования
    total_n_spent_dollars = 0
    total_n_used_tokens = 0

    n_used_tokens_dict = db.get_user_attribute(user_id, "n_used_tokens")
    n_generated_images = db.get_user_attribute(user_id, "n_generated_images")
    n_transcribed_seconds = db.get_user_attribute(user_id, "n_transcribed_seconds")

    details_text = "🏷️ Подробности:\n"
    for model_key in sorted(n_used_tokens_dict.keys()):
        n_input_tokens, n_output_tokens = n_used_tokens_dict[model_key]["n_input_tokens"], \
        n_used_tokens_dict[model_key]["n_output_tokens"]
        total_n_used_tokens += n_input_tokens + n_output_tokens

        n_input_spent_dollars = config.models["info"][model_key]["price_per_1000_input_tokens"] * (
                    n_input_tokens / 1000)
        n_output_spent_dollars = config.models["info"][model_key]["price_per_1000_output_tokens"] * (
                    n_output_tokens / 1000)
        total_n_spent_dollars += n_input_spent_dollars + n_output_spent_dollars

        details_text += f"- {model_key}: <b>{n_input_spent_dollars + n_output_spent_dollars:.03f}$</b> / <b>{n_input_tokens + n_output_tokens} tokens</b>\n"

    # Генерация изображений
    image_generation_n_spent_dollars = config.models["info"]["dalle-2"]["price_per_1_image"] * n_generated_images
    if n_generated_images != 0:
        details_text += f"- DALL·E 2 (image generation): <b>{image_generation_n_spent_dollars:.03f}$</b> / <b>{n_generated_images} generated images</b>\n"

    total_n_spent_dollars += image_generation_n_spent_dollars

    # Распознавание голоса
    voice_recognition_n_spent_dollars = config.models["info"]["whisper"]["price_per_1_min"] * (
                n_transcribed_seconds / 60)
    if n_transcribed_seconds != 0:
        details_text += f"- Whisper (voice recognition): <b>{voice_recognition_n_spent_dollars:.03f}$</b> / <b>{n_transcribed_seconds:.01f} seconds</b>\n"

    total_n_spent_dollars += voice_recognition_n_spent_dollars

    text = f"Вы потратили <b>{total_n_spent_dollars:.03f}$</b>\n"
    text += f"Вы использовали <b>{total_n_used_tokens}</b> tokens\n\n"
    text += details_text

    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def edited_message_handle(update: Update, context: CallbackContext):
    """
    Обрабатывает случаи, когда сообщение редактируется в личных чатах.

    Аргументы:
    - update: объект Update, представляющий текущее обновление (редактирование сообщения).
    - context: объект CallbackContext, предоставляющий контекст выполнения.

    Описание:
    - Отправляет пользователю сообщение о том, что редактирование сообщений не поддерживается.
    """
    if update.edited_message.chat.type == "private":
        text = "🥲 К сожалению, редактирование сообщений в личных чатах не поддерживается."
        await update.edited_message.reply_text(text, parse_mode=ParseMode.HTML)


async def error_handle(update: Update, context: CallbackContext) -> None:
    """
    Обрабатывает ошибки, возникшие при обработке обновлений.

    Аргументы:
    - update: объект Update, представляющий текущее обновление.
    - context: объект CallbackContext, предоставляющий контекст выполнения.

    Описание:
    - Логирует ошибку.
    - Отправляет пользователю сообщение с деталями ошибки.
    """
    logger.error(msg="Исключение при обработке обновления:", exc_info=context.error)

    try:
        # Собираем сообщение об ошибке
        tb_list = traceback.format_exception(None, context.error, context.error.__traceback__)
        tb_string = "".join(tb_list)
        update_str = update.to_dict() if isinstance(update, Update) else str(update)
        message = (
            f"Произошло исключение при обработке обновления\n"
            f"<pre>update = {html.escape(json.dumps(update_str, indent=2, ensure_ascii=False))}"
            "</pre>\n\n"
            f"<pre>{html.escape(tb_string)}</pre>"
        )

        # Разделяем текст на несколько сообщений из-за ограничения на 4096 символов
        for message_chunk in split_text_into_chunks(message, 4096):
            try:
                await context.bot.send_message(update.effective_chat.id, message_chunk, parse_mode=ParseMode.HTML)
            except telegram.error.BadRequest:
                # Сообщение содержит недопустимые символы, отправляем без parse_mode
                await context.bot.send_message(update.effective_chat.id, message_chunk)
    except:
        await context.bot.send_message(update.effective_chat.id, "Ошибка в обработчике ошибок")


async def post_init(application: Application):
    """
    Устанавливает команды бота после его инициализации.

    Аргументы:
    - application: объект Application, представляющий приложение бота.

    Описание:
    - Устанавливает список команд бота и их описание.
    """
    await application.bot.set_my_commands([
        BotCommand("/new", "Начать новый диалог"),
        BotCommand("/mode", "Выбрать режим общения"),
        BotCommand("/retry", "Перегенерировать ответ на предыдущий запрос"),
        BotCommand("/balance", "Показать баланс"),
        BotCommand("/settings", "Показать настройки"),
        BotCommand("/help", "Показать справку"),
    ])


def run_bot() -> None:
    """
        Запускает бота.

        Описание:
        - Создает объект приложения бота и добавляет обработчики команд и сообщений.
        - Настраивает фильтры и запускает бота в режиме polling.
        """
    application = (
        ApplicationBuilder()
        .token(config.telegram_token)
        .concurrent_updates(True)
        .rate_limiter(AIORateLimiter(max_retries=5))
        .http_version("1.1")
        .get_updates_http_version("1.1")
        .post_init(post_init)
        .build()
    )

    # Добавляем обработчики команд и сообщений
    user_filter = filters.ALL
    if len(config.allowed_telegram_usernames) > 0:
        usernames = [x for x in config.allowed_telegram_usernames if isinstance(x, str)]
        any_ids = [x for x in config.allowed_telegram_usernames if isinstance(x, int)]
        user_ids = [x for x in any_ids if x > 0]
        group_ids = [x for x in any_ids if x < 0]
        user_filter = filters.User(username=usernames) | filters.User(user_id=user_ids) | filters.Chat(
            chat_id=group_ids)

    application.add_handler(CommandHandler("start", start_handle, filters=user_filter))
    application.add_handler(CommandHandler("help", help_handle, filters=user_filter))
    application.add_handler(CommandHandler("help_group_chat", help_group_chat_handle, filters=user_filter))

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & user_filter, message_handle))
    application.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND & user_filter, message_handle))
    application.add_handler(MessageHandler(filters.VIDEO & ~filters.COMMAND & user_filter, unsupport_message_handle))
    application.add_handler(
        MessageHandler(filters.Document.ALL & ~filters.COMMAND & user_filter, unsupport_message_handle))
    application.add_handler(CommandHandler("retry", retry_handle, filters=user_filter))
    application.add_handler(CommandHandler("new", new_dialog_handle, filters=user_filter))
    application.add_handler(CommandHandler("cancel", cancel_handle, filters=user_filter))

    application.add_handler(MessageHandler(filters.VOICE & user_filter, voice_message_handle))

    application.add_handler(CommandHandler("mode", show_chat_modes_handle, filters=user_filter))
    application.add_handler(CallbackQueryHandler(show_chat_modes_callback_handle, pattern="^show_chat_modes"))
    application.add_handler(CallbackQueryHandler(set_chat_mode_handle, pattern="^set_chat_mode"))

    application.add_handler(CommandHandler("settings", settings_handle, filters=user_filter))
    application.add_handler(CallbackQueryHandler(set_settings_handle, pattern="^set_settings"))

    application.add_handler(CommandHandler("balance", show_balance_handle, filters=user_filter))

    application.add_error_handler(error_handle)

    # start the bot
    application.run_polling()


if __name__ == "__main__":
    db = init_database()
    run_bot()
