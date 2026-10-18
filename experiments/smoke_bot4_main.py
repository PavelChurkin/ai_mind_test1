"""
Смоук-тест init()/main() бота bot4 с настоящими библиотеками vk_api/openai,
но без сети: VkApi, VkBotLongPoll и OpenAI подменены. Проверяет, что
импорты, разбор событий Long Poll и сохранение ответа работают вместе.

Запуск (нужны vk_api, openai, python-dotenv):
    python3 experiments/smoke_bot4_main.py
"""
import os
import sys
import tempfile
import importlib.util

import vk_api
import vk_api.bot_longpoll as bl
import openai

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("bot4", os.path.join(HERE, "..", "Прочее", "bot4.py"))
bot4 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot4)

SENT = []


class FakeApi:
    class messages:
        @staticmethod
        def send(**p):
            SENT.append(p)
            return [{"peer_id": int(p["peer_ids"]), "message_id": 0, "conversation_message_id": 99}]

        @staticmethod
        def getConversations(count):
            return {"items": []}

    class users:
        @staticmethod
        def get(user_ids, **kw):
            return [{"id": int(i), "first_name": "Пётр", "last_name": ""} for i in user_ids.split(",")]


class FakeSession:
    def __init__(self, token):
        pass

    def get_api(self):
        return FakeApi()


EVENTS = [
    {"type": "message_new", "group_id": 5, "object": {"message": {
        "id": 0, "conversation_message_id": 1, "peer_id": 2000000001, "from_id": 7,
        "text": "Грань, привет!", "date": int(__import__("time").time()),
        "attachments": [], "fwd_messages": []}}},
    {"type": "message_reply", "group_id": 5, "object": {
        "id": 0, "conversation_message_id": 99, "peer_id": 2000000001, "from_id": -5,
        "text": "Привет, Пётр!", "date": int(__import__("time").time())}},
    {"type": "message_new", "group_id": 5, "object": {"message": {
        "id": 0, "conversation_message_id": 3, "peer_id": 2000000001, "from_id": 7,
        "text": "Грань, стоп машина", "date": int(__import__("time").time())}}},
]


class FakeLongPoll:
    def __init__(self, session, group_id):
        pass

    def listen(self):
        for raw in EVENTS:
            yield bl.VkBotMessageEvent(raw)


class FakeOpenAI:
    def __init__(self, **kw):
        class R:
            class choices0:
                class message:
                    content = "Привет, Пётр!"
            choices = [choices0]
        self.chat = type("C", (), {"completions": type("X", (), {"create": staticmethod(lambda **k: R)})})


vk_api.VkApi = FakeSession
bl.VkBotLongPoll = FakeLongPoll
openai.OpenAI = FakeOpenAI
os.environ.update(VK_GROUP_ID="5", VK_TOKEN="x", PROXYAPI_KEY="x")

with tempfile.TemporaryDirectory() as d:
    bot4.DB_PATH = os.path.join(d, "gran4.db")
    bot4.OLD_DB_PATH = os.path.join(d, "gran.db")
    try:
        bot4.main()
    except SystemExit:
        pass
    import sqlite3
    rows = sqlite3.connect(bot4.DB_PATH).execute(
        "SELECT cmid, from_id, text FROM messages ORDER BY id").fetchall()

print("Отправлено:", [p["message"] for p in SENT])
print("В БД:", rows)
assert rows[0] == (1, 7, "Грань, привет!"), rows
assert len([r for r in rows if r[2] == "Привет, Пётр!"]) == 1, "ответ бота задублирован"
print("OK")
