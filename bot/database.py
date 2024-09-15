from typing import Optional, Any
import pymongo
import uuid
from datetime import datetime
from bot import config

class Database:
    """
    Класс для работы с базой данных MongoDB.

    Инициализирует подключение к базе данных и определяет коллекции для работы с пользователями и диалогами.
    """

    def __init__(self):
        """
        Инициализирует подключение к базе данных и коллекциям.
        """
        # Подключаемся к MongoDB по URI, указанному в конфигурации
        self.client = pymongo.MongoClient(config.mongodb_uri)
        # Выбираем базу данных "chatgpt_telegram_bot"
        self.db = self.client["chatgpt_telegram_bot"]

        # Определяем коллекции для пользователей и диалогов
        self.user_collection = self.db["user"]
        self.dialog_collection = self.db["dialog"]

    def check_if_user_exists(self, user_id: int, raise_exception: bool = False):
        """
        Проверяет, существует ли пользователь с данным идентификатором в базе данных.

        Аргументы:
        - user_id: идентификатор пользователя.
        - raise_exception: если True, выбрасывает исключение, если пользователь не найден.

        Возвращает:
        - True, если пользователь существует, иначе False.

        Исключение:
        - ValueError: если raise_exception=True и пользователь не существует.
        """
        if self.user_collection.count_documents({"_id": user_id}) > 0:
            return True
        else:
            if raise_exception:
                raise ValueError(f"Пользователь {user_id} не существует")
            else:
                return False

    def add_new_user(
        self,
        user_id: int,
        chat_id: int,
        username: str = "",
        first_name: str = "",
        last_name: str = "",
    ):
        """
        Добавляет нового пользователя в базу данных.

        Аргументы:
        - user_id: идентификатор пользователя.
        - chat_id: идентификатор чата.
        - username: имя пользователя (опционально).
        - first_name: имя пользователя (опционально).
        - last_name: фамилия пользователя (опционально).

        Описание:
        - Если пользователь с таким user_id не существует, он добавляется в коллекцию "user".
        """
        user_dict = {
            "_id": user_id,  # Идентификатор пользователя как первичный ключ
            "chat_id": chat_id,  # Идентификатор чата

            "username": username,  # Имя пользователя
            "first_name": first_name,  # Имя
            "last_name": last_name,  # Фамилия

            "last_interaction": datetime.now(),  # Время последнего взаимодействия
            "first_seen": datetime.now(),  # Время первого взаимодействия

            "current_dialog_id": None,  # Идентификатор текущего диалога
            "current_chat_mode": "assistant",  # Текущий режим общения
            "current_model": config.models["available_text_models"][0],  # Текущая модель для общения

            "n_used_tokens": {},  # Статистика использованных токенов

            "n_generated_images": 0,  # Количество сгенерированных изображений
            "n_transcribed_seconds": 0.0  # Время распознанных голосовых сообщений
        }

        # Добавляем пользователя в базу, если он не существует
        if not self.check_if_user_exists(user_id):
            self.user_collection.insert_one(user_dict)

    def start_new_dialog(self, user_id: int):
        """
        Начинает новый диалог для указанного пользователя.

        Аргументы:
        - user_id: идентификатор пользователя.

        Возвращает:
        - Идентификатор нового диалога.

        Описание:
        - Создает новый диалог и обновляет текущий диалог пользователя в базе данных.
        """
        # Проверяем, существует ли пользователь
        self.check_if_user_exists(user_id, raise_exception=True)

        # Генерируем уникальный идентификатор диалога
        dialog_id = str(uuid.uuid4())
        dialog_dict = {
            "_id": dialog_id,  # Идентификатор диалога
            "user_id": user_id,  # Идентификатор пользователя
            "chat_mode": self.get_user_attribute(user_id, "current_chat_mode"),  # Текущий режим общения
            "start_time": datetime.now(),  # Время начала диалога
            "model": self.get_user_attribute(user_id, "current_model"),  # Текущая модель для общения
            "messages": []  # Список сообщений в диалоге
        }

        # Добавляем новый диалог в коллекцию
        self.dialog_collection.insert_one(dialog_dict)

        # Обновляем текущий диалог пользователя
        self.user_collection.update_one(
            {"_id": user_id},
            {"$set": {"current_dialog_id": dialog_id}}
        )

        return dialog_id

    def get_user_attribute(self, user_id: int, key: str):
        """
        Получает значение атрибута пользователя по ключу.

        Аргументы:
        - user_id: идентификатор пользователя.
        - key: ключ атрибута.

        Возвращает:
        - Значение атрибута, если он существует, иначе None.

        Описание:
        - Проверяет наличие пользователя в базе данных и возвращает запрашиваемый атрибут.
        """
        # Проверяем, существует ли пользователь
        self.check_if_user_exists(user_id, raise_exception=True)

        # Ищем пользователя в базе данных
        user_dict = self.user_collection.find_one({"_id": user_id})

        # Возвращаем значение атрибута, если он существует
        if key not in user_dict:
            return None

        return user_dict[key]

    def set_user_attribute(self, user_id: int, key: str, value: Any):
        """
        Устанавливает значение атрибута пользователя.

        Аргументы:
        - user_id: идентификатор пользователя.
        - key: ключ атрибута.
        - value: новое значение атрибута.

        Описание:
        - Проверяет наличие пользователя в базе данных и обновляет значение атрибута.
        """
        # Проверяем, существует ли пользователь
        self.check_if_user_exists(user_id, raise_exception=True)

        # Обновляем значение атрибута в базе данных
        self.user_collection.update_one({"_id": user_id}, {"$set": {key: value}})

    def update_n_used_tokens(self, user_id: int, model: str, n_input_tokens: int, n_output_tokens: int):
        """
        Обновляет статистику использования токенов для пользователя.

        Аргументы:
        - user_id: идентификатор пользователя.
        - model: название модели.
        - n_input_tokens: количество входных токенов.
        - n_output_tokens: количество выходных токенов.

        Описание:
        - Добавляет или обновляет информацию о количестве использованных токенов для указанной модели.
        """
        # Получаем текущую статистику использования токенов
        n_used_tokens_dict = self.get_user_attribute(user_id, "n_used_tokens")

        # Если статистика для данной модели уже существует, обновляем её
        if model in n_used_tokens_dict:
            n_used_tokens_dict[model]["n_input_tokens"] += n_input_tokens  # Увеличиваем количество входных токенов
            n_used_tokens_dict[model]["n_output_tokens"] += n_output_tokens  # Увеличиваем количество выходных токенов
        else:
            # Если статистики для данной модели нет, создаем новую запись
            n_used_tokens_dict[model] = {
                "n_input_tokens": n_input_tokens,  # Устанавливаем количество входных токенов
                "n_output_tokens": n_output_tokens  # Устанавливаем количество выходных токенов
            }

        # Сохраняем обновленную статистику использования токенов в базе данных
        self.set_user_attribute(user_id, "n_used_tokens", n_used_tokens_dict)

    def get_dialog_messages(self, user_id: int, dialog_id: Optional[str] = None):
        """
        Возвращает список сообщений из указанного диалога пользователя.

        Аргументы:
        - user_id: идентификатор пользователя.
        - dialog_id: идентификатор диалога (опционально). Если не указан, используется текущий диалог.

        Возвращает:
        - Список сообщений из диалога.

        Описание:
        - Если идентификатор диалога не указан, используется текущий диалог пользователя.
        """
        # Проверяем, существует ли пользователь
        self.check_if_user_exists(user_id, raise_exception=True)

        # Если идентификатор диалога не указан, получаем текущий диалог
        if dialog_id is None:
            dialog_id = self.get_user_attribute(user_id, "current_dialog_id")

        # Ищем диалог в базе данных
        dialog_dict = self.dialog_collection.find_one({"_id": dialog_id, "user_id": user_id})

        # Возвращаем список сообщений из диалога
        return dialog_dict["messages"]

    def set_dialog_messages(self, user_id: int, dialog_messages: list, dialog_id: Optional[str] = None):
        """
        Обновляет список сообщений в указанном диалоге пользователя.

        Аргументы:
        - user_id: идентификатор пользователя.
        - dialog_messages: список сообщений, который нужно сохранить.
        - dialog_id: идентификатор диалога (опционально). Если не указан, используется текущий диалог.

        Описание:
        - Обновляет список сообщений в базе данных для указанного диалога.
        """
        # Проверяем, существует ли пользователь
        self.check_if_user_exists(user_id, raise_exception=True)

        # Если идентификатор диалога не указан, используем текущий диалог
        if dialog_id is None:
            dialog_id = self.get_user_attribute(user_id, "current_dialog_id")

        # Обновляем список сообщений в диалоге
        self.dialog_collection.update_one(
            {"_id": dialog_id, "user_id": user_id},
            {"$set": {"messages": dialog_messages}}
        )
