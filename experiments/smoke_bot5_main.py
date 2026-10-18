"""
Смоук-тест init()/main() бота bot5 с настоящими библиотеками vk_api/openai,
но без сети: VkApi, VkBotLongPoll и OpenAI подменены. Проверяет главный цикл
на longpoll.check(): два обращения подряд → одна пауза BATCH_WAIT_SEC → один
ответ, событие message_reply не дублирует ответ, «стоп машина» завершает бота.

Запуск (нужны vk_api, openai, python-dotenv):
    python3 experiments/smoke_bot5_main.py
"""
import os
import time
import sqlite3
import tempfile
import importlib.util

import vk_api
import vk_api.bot_longpoll as bl
import openai

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location(
    "bot5", os.path.join(HERE, "..", "Прочее", "bot5", "bot5.py"))
bot5 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot5)

SENT = []
LLM_CALLS = []


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


def new(cmid, text):
    return {"type": "message_new", "group_id": 5, "object": {"message": {
        "id": 0, "conversation_message_id": cmid, "peer_id": 2000000001, "from_id": 7,
        "text": text, "date": int(time.time()), "attachments": [], "fwd_messages": []}}}


class FakeLongPoll:
    """check() как у настоящего VkBotLongPoll: список событий за wait секунд."""

    def __init__(self, session, group_id, wait=25):
        self.wait = wait
        self.step = 0

    def check(self):
        self.step += 1
        time.sleep(0.05)
        if self.step == 1:
            return [bl.VkBotMessageEvent(new(1, "Грань, привет!")),
                    bl.VkBotMessageEvent(new(2, "Грань, как дела?"))]
        if SENT and self.step < 1000 and not getattr(self, "replied", False):
            self.replied = True
            return [bl.VkBotMessageEvent({"type": "message_reply", "group_id": 5, "object": {
                "id": 0, "conversation_message_id": 99, "peer_id": 2000000001, "from_id": -5,
                "text": "Привет, Пётр! Дела хорошо.", "date": int(time.time())}})]
        if getattr(self, "replied", False):
            return [bl.VkBotMessageEvent(new(3, "Грань, стоп машина"))]
        return []


class FakeOpenAI:
    def __init__(self, **kw):
        class R:
            class choices0:
                class message:
                    content = "Привет, Пётр! Дела хорошо."
            choices = [choices0]

        def create(**k):
            LLM_CALLS.append(k)
            return R
        self.chat = type("C", (), {"completions": type("X", (), {"create": staticmethod(create)})})


vk_api.VkApi = FakeSession
bl.VkBotLongPoll = FakeLongPoll
openai.OpenAI = FakeOpenAI
os.environ.update(VK_GROUP_ID="5", VK_TOKEN="x", PROXYAPI_KEY="x", ADMIN_IDS="7, 8")
bot5.BATCH_WAIT_SEC = 0.3
bot5.LLM_MIN_INTERVAL_SEC = 0

with tempfile.TemporaryDirectory() as d:
    bot5.DB_PATH = os.path.join(d, "gran5.db")
    bot5.OLD_DB_PATH = os.path.join(d, "gran4.db")
    try:
        bot5.main()
    except SystemExit:
        pass
    rows = sqlite3.connect(bot5.DB_PATH).execute(
        "SELECT cmid, from_id, text FROM messages ORDER BY id").fetchall()

print("Отправлено:", [p["message"] for p in SENT])
print("В БД:", rows)
assert bot5.ADMIN_IDS == {7, 8}, bot5.ADMIN_IDS
assert len(LLM_CALLS) == 1, "на два обращения подряд должен быть один запрос к LLM"
assert "НОВЫЕ СООБЩЕНИЯ (их 2" in LLM_CALLS[0]["messages"][1]["content"]
assert [r[2] for r in rows[:2]] == ["Грань, привет!", "Грань, как дела?"], rows
assert len([r for r in rows if r[2] == "Привет, Пётр! Дела хорошо."]) == 1, "ответ бота задублирован"
assert SENT[-1]["message"] == "Останавливаюсь."
print("OK")
