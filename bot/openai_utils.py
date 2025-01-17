import base64
from io import BytesIO
from bot import config
import logging

import tiktoken
import openai

# Настройка OpenAI API
openai.api_key = config.openai_api_key
if config.openai_api_base is not None:
    openai.api_base = config.openai_api_base
logger = logging.getLogger(__name__)

# Опции для завершения запроса к OpenAI
OPENAI_COMPLETION_OPTIONS = {
    "temperature": 0.7,  # Температура для генерации текста
    "max_tokens": 1000,  # Максимальное количество токенов в ответе
    "top_p": 1,  # Параметр для контроля кумулятивной вероятности
    "frequency_penalty": 0,  # Штраф за частоту повторений
    "presence_penalty": 0,  # Штраф за присутствие новых тем
    "request_timeout": 60.0,  # Тайм-аут запроса
}


class ChatGPT:
    def __init__(self, model="gpt-3.5-turbo"):
        """
        Инициализация экземпляра ChatGPT.

        :param model: Имя модели OpenAI, которая будет использоваться.
                      Должно быть одним из следующих: "text-davinci-003",
                      "gpt-3.5-turbo-16k", "gpt-3.5-turbo", "gpt-4", "gpt-4o",
                      "gpt-4-1106-preview", "gpt-4-vision-preview".
        """
        assert model in {"text-davinci-003", "gpt-3.5-turbo-16k", "gpt-3.5-turbo", "gpt-4", "gpt-4o",
                         "gpt-4-1106-preview", "gpt-4-vision-preview"}, f"Unknown model: {model}"
        self.model = model

    async def send_message(self, message, dialog_messages=[], chat_mode="assistant"):
        """
        Отправляет сообщение в модель и получает ответ.

        :param message: Сообщение от пользователя.
        :param dialog_messages: Список сообщений диалога для контекста.
        :param chat_mode: Режим чата (например, "assistant").
        :return: Кортеж, содержащий ответ, токены ввода и вывода, и количество удаленных сообщений диалога.
        """
        if chat_mode not in config.chat_modes.keys():
            raise ValueError(f"Chat mode {chat_mode} is not supported")

        n_dialog_messages_before = len(dialog_messages)
        answer = None
        while answer is None:
            try:
                if self.model in {"gpt-3.5-turbo-16k", "gpt-3.5-turbo", "gpt-4", "gpt-4o", "gpt-4-1106-preview",
                                  "gpt-4-vision-preview"}:
                    messages = self._generate_prompt_messages(message, dialog_messages, chat_mode)

                    r = await openai.ChatCompletion.acreate(
                        model=self.model,
                        messages=messages,
                        **OPENAI_COMPLETION_OPTIONS
                    )
                    answer = r.choices[0].message["content"]
                elif self.model == "text-davinci-003":
                    prompt = self._generate_prompt(message, dialog_messages, chat_mode)
                    r = await openai.Completion.acreate(
                        engine=self.model,
                        prompt=prompt,
                        **OPENAI_COMPLETION_OPTIONS
                    )
                    answer = r.choices[0].text
                else:
                    raise ValueError(f"Unknown model: {self.model}")

                answer = self._postprocess_answer(answer)
                n_input_tokens, n_output_tokens = r.usage.prompt_tokens, r.usage.completion_tokens
            except openai.error.InvalidRequestError as e:  # Слишком много токенов
                if len(dialog_messages) == 0:
                    raise ValueError(
                        "Dialog messages is reduced to zero, but still has too many tokens to make completion") from e

                # Удаление первого сообщения из dialog_messages
                dialog_messages = dialog_messages[1:]

        n_first_dialog_messages_removed = n_dialog_messages_before - len(dialog_messages)

        return answer, (n_input_tokens, n_output_tokens), n_first_dialog_messages_removed

    async def send_message_stream(self, message, dialog_messages=[], chat_mode="assistant"):
        """
        Отправляет сообщение в модель и получает ответ в виде потока.

        :param message: Сообщение от пользователя.
        :param dialog_messages: Список сообщений диалога для контекста.
        :param chat_mode: Режим чата (например, "assistant").
        :return: Генератор, который выдает статус выполнения, частичный ответ, токены ввода и вывода, и количество удаленных сообщений диалога.
        """
        if chat_mode not in config.chat_modes.keys():
            raise ValueError(f"Chat mode {chat_mode} is not supported")

        n_dialog_messages_before = len(dialog_messages)
        answer = None
        while answer is None:
            try:
                if self.model in {"gpt-3.5-turbo-16k", "gpt-3.5-turbo", "gpt-4", "gpt-4o", "gpt-4-1106-preview"}:
                    messages = self._generate_prompt_messages(message, dialog_messages, chat_mode)

                    r_gen = await openai.ChatCompletion.acreate(
                        model=self.model,
                        messages=messages,
                        stream=True,
                        **OPENAI_COMPLETION_OPTIONS
                    )

                    answer = ""
                    async for r_item in r_gen:
                        delta = r_item.choices[0].delta

                        if "content" in delta:
                            answer += delta.content
                            n_input_tokens, n_output_tokens = self._count_tokens_from_messages(messages, answer,
                                                                                               model=self.model)
                            n_first_dialog_messages_removed = 0

                            yield "not_finished", answer, (
                            n_input_tokens, n_output_tokens), n_first_dialog_messages_removed


                elif self.model == "text-davinci-003":
                    prompt = self._generate_prompt(message, dialog_messages, chat_mode)
                    r_gen = await openai.Completion.acreate(
                        engine=self.model,
                        prompt=prompt,
                        stream=True,
                        **OPENAI_COMPLETION_OPTIONS
                    )

                    answer = ""
                    async for r_item in r_gen:
                        answer += r_item.choices[0].text
                        n_input_tokens, n_output_tokens = self._count_tokens_from_prompt(prompt, answer,
                                                                                         model=self.model)
                        n_first_dialog_messages_removed = n_dialog_messages_before - len(dialog_messages)
                        yield "not_finished", answer, (n_input_tokens, n_output_tokens), n_first_dialog_messages_removed

                answer = self._postprocess_answer(answer)

            except openai.error.InvalidRequestError as e:  # Слишком много токенов
                if len(dialog_messages) == 0:
                    raise e

                # Удаление первого сообщения из dialog_messages
                dialog_messages = dialog_messages[1:]

        yield "finished", answer, (
        n_input_tokens, n_output_tokens), n_first_dialog_messages_removed  # Отправка финального ответа

    async def send_vision_message(
            self,
            message,
            dialog_messages=[],
            chat_mode="assistant",
            image_buffer: BytesIO = None,
    ):
        """
        Отправляет сообщение в модель с изображением и получает ответ.

        :param message: Сообщение от пользователя.
        :param dialog_messages: Список сообщений диалога для контекста.
        :param chat_mode: Режим чата (например, "assistant").
        :param image_buffer: Буфер с изображением в формате BytesIO.
        :return: Кортеж, содержащий ответ, токены ввода и вывода, и количество удаленных сообщений диалога.
        """
        n_dialog_messages_before = len(dialog_messages)
        answer = None
        while answer is None:
            try:
                if self.model == "gpt-4-vision-preview" or self.model == "gpt-4o":
                    messages = self._generate_prompt_messages(
                        message, dialog_messages, chat_mode, image_buffer
                    )
                    r = await openai.ChatCompletion.acreate(
                        model=self.model,
                        messages=messages,
                        **OPENAI_COMPLETION_OPTIONS
                    )
                    answer = r.choices[0].message.content
                else:
                    raise ValueError(f"Unsupported model: {self.model}")

                answer = self._postprocess_answer(answer)
                n_input_tokens, n_output_tokens = (
                    r.usage.prompt_tokens,
                    r.usage.completion_tokens,
                )
            except openai.error.InvalidRequestError as e:  # Слишком много токенов
                if len(dialog_messages) == 0:
                    raise ValueError(
                        "Dialog messages is reduced to zero, but still has too many tokens to make completion"
                    ) from e

                # Удаление первого сообщения из dialog_messages
                dialog_messages = dialog_messages[1:]

        n_first_dialog_messages_removed = n_dialog_messages_before - len(
            dialog_messages
        )

        return (
            answer,
            (n_input_tokens, n_output_tokens),
            n_first_dialog_messages_removed,
        )

    async def send_vision_message_stream(
            self,
            message,
            dialog_messages=[],
            chat_mode="assistant",
            image_buffer: BytesIO = None,
    ):
        """
        Отправляет сообщение в модель с изображением и получает ответ в виде потока.

        :param message: Сообщение от пользователя.
        :param dialog_messages: Список сообщений диалога для контекста.
        :param chat_mode: Режим чата (например, "assistant").
        :param image_buffer: Буфер с изображением в формате BytesIO.
        :return: Генератор, который выдает статус выполнения, частичный ответ, токены ввода и вывода, и количество удаленных сообщений диалога.
        """
        n_dialog_messages_before = len(dialog_messages)
        answer = None
        while answer is None:
            try:
                if self.model == "gpt-4-vision-preview" or self.model == "gpt-4o":
                    messages = self._generate_prompt_messages(
                        message, dialog_messages, chat_mode, image_buffer
                    )

                    r_gen = await openai.ChatCompletion.acreate(
                        model=self.model,
                        messages=messages,
                        stream=True,
                        **OPENAI_COMPLETION_OPTIONS,
                    )

                    answer = ""
                    async for r_item in r_gen:
                        delta = r_item.choices[0].delta
                        if "content" in delta:
                            answer += delta.content
                            (
                                n_input_tokens,
                                n_output_tokens,
                            ) = self._count_tokens_from_messages(
                                messages, answer, model=self.model
                            )
                            n_first_dialog_messages_removed = (
                                    n_dialog_messages_before - len(dialog_messages)
                            )
                            yield "not_finished", answer, (
                                n_input_tokens,
                                n_output_tokens,
                            ), n_first_dialog_messages_removed

                answer = self._postprocess_answer(answer)

            except openai.error.InvalidRequestError as e:  # Слишком много токенов
                if len(dialog_messages) == 0:
                    raise e
                # Удаление первого сообщения из dialog_messages
                dialog_messages = dialog_messages[1:]

        yield "finished", answer, (
            n_input_tokens,
            n_output_tokens,
        ), n_first_dialog_messages_removed

    def _generate_prompt(self, message, dialog_messages, chat_mode):
        """
        Генерирует текстовый запрос для модели на основе сообщений и режима чата.

        :param message: Сообщение от пользователя.
        :param dialog_messages: Список сообщений диалога для контекста.
        :param chat_mode: Режим чата (например, "assistant").
        :return: Текстовый запрос, сформированный для модели.
        """
        prompt = config.chat_modes[chat_mode]["prompt_start"]
        prompt += "\n\n"

        # Добавление контекста диалога
        if len(dialog_messages) > 0:
            prompt += "Chat:\n"
            for dialog_message in dialog_messages:
                prompt += f"User: {dialog_message['user']}\n"
                prompt += f"Assistant: {dialog_message['bot']}\n"

        # Текущее сообщение
        prompt += f"User: {message}\n"
        prompt += "Assistant: "

        return prompt

    def _encode_image(self, image_buffer: BytesIO) -> bytes:
        """
        Кодирует изображение из буфера в формат base64.

        :param image_buffer: Буфер с изображением в формате BytesIO.
        :return: Изображение в формате base64.
        """
        return base64.b64encode(image_buffer.read()).decode("utf-8")

    def _generate_prompt_messages(self, message, dialog_messages, chat_mode, image_buffer: BytesIO = None):
        """
        Генерирует список сообщений для запроса в модель, учитывая изображение, если оно есть.

        :param message: Сообщение от пользователя.
        :param dialog_messages: Список сообщений диалога для контекста.
        :param chat_mode: Режим чата (например, "assistant").
        :param image_buffer: Буфер с изображением в формате BytesIO (может быть None).
        :return: Список сообщений для запроса.
        """
        prompt = config.chat_modes[chat_mode]["prompt_start"]

        messages = [{"role": "system", "content": prompt}]

        for dialog_message in dialog_messages:
            messages.append({"role": "user", "content": dialog_message["user"]})
            messages.append({"role": "assistant", "content": dialog_message["bot"]})

        if image_buffer is not None:
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": message,
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{self._encode_image(image_buffer)}",
                                "detail": "high"
                            }
                        }
                    ]
                }

            )
        else:
            messages.append({"role": "user", "content": message})

        return messages

    def _postprocess_answer(self, answer):
        """
        Постобработка ответа модели (например, удаление лишних пробелов).

        :param answer: Ответ модели до постобработки.
        :return: Постобработанный ответ.
        """
        answer = answer.strip()
        return answer

    def _count_tokens_from_messages(self, messages, answer, model="gpt-3.5-turbo"):
        """
        Подсчитывает количество токенов в сообщениях и ответе.

        :param messages: Список сообщений для подсчета токенов.
        :param answer: Ответ модели для подсчета токенов.
        :param model: Модель, которая используется для подсчета токенов.
        :return: Кортеж из количества токенов ввода и вывода.
        """
        encoding = tiktoken.encoding_for_model(model)

        if model == "gpt-3.5-turbo-16k":
            tokens_per_message = 4  # Каждый сообщение имеет формат <im_start>{role/name}\n{content}<im_end>\n
            tokens_per_name = -1  # Если есть имя, роль опускается
        elif model == "gpt-3.5-turbo":
            tokens_per_message = 4
            tokens_per_name = -1
        elif model == "gpt-4":
            tokens_per_message = 3
            tokens_per_name = 1
        elif model == "gpt-4-1106-preview":
            tokens_per_message = 3
            tokens_per_name = 1
        elif model == "gpt-4-vision-preview":
            tokens_per_message = 3
            tokens_per_name = 1
        elif model == "gpt-4o":
            tokens_per_message = 3
            tokens_per_name = 1
        else:
            raise ValueError(f"Unknown model: {model}")

        # Ввод
        n_input_tokens = 0
        for message in messages:
            n_input_tokens += tokens_per_message
            if isinstance(message["content"], list):
                for sub_message in message["content"]:
                    if "type" in sub_message:
                        if sub_message["type"] == "text":
                            n_input_tokens += len(encoding.encode(sub_message["text"]))
                        elif sub_message["type"] == "image_url":
                            pass
            else:
                if "type" in message:
                    if message["type"] == "text":
                        n_input_tokens += len(encoding.encode(message["text"]))
                    elif message["type"] == "image_url":
                        pass

        n_input_tokens += 2

        # Вывод
        n_output_tokens = 1 + len(encoding.encode(answer))

        return n_input_tokens, n_output_tokens

    def _count_tokens_from_prompt(self, prompt, answer, model="text-davinci-003"):
        """
        Подсчитывает количество токенов в запросе и ответе.

        :param prompt: Запрос для подсчета токенов.
        :param answer: Ответ модели для подсчета токенов.
        :param model: Модель, которая используется для подсчета токенов.
        :return: Кортеж из количества токенов ввода и вывода.
        """
        encoding = tiktoken.encoding_for_model(model)

        n_input_tokens = len(encoding.encode(prompt)) + 1
        n_output_tokens = len(encoding.encode(answer))

        return n_input_tokens, n_output_tokens


async def transcribe_audio(audio_file) -> str:
    """
    Распознает текст из аудиофайла с помощью модели Whisper от OpenAI.

    :param audio_file: Аудиофайл для распознавания.
    :return: Распознанный текст.
    """
    r = await openai.Audio.atranscribe("whisper-1", audio_file)
    return r["text"] or ""


async def generate_images(prompt, n_images=4, size="512x512"):
    """
    Генерирует изображения по текстовому запросу с помощью модели OpenAI.

    :param prompt: Текстовый запрос для генерации изображений.
    :param n_images: Количество изображений для генерации.
    :param size: Размер изображений.
    :return: Список URL сгенерированных изображений.
    """
    r = await openai.Image.acreate(prompt=prompt, n=n_images, size=size)
    image_urls = [item.url for item in r.data]
    return image_urls


async def is_content_acceptable(prompt):
    """
    Проверяет приемлемость контента с помощью модели модерации от OpenAI.

    :param prompt: Текст для проверки.
    :return: True, если контент приемлем, иначе False.
    """
    r = await openai.Moderation.acreate(input=prompt)
    return not all(r.results[0].categories.values())
