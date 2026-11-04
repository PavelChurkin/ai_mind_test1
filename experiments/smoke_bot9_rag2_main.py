"""
Смоук-тест /раг2 через настоящие init()/main() bot9 с библиотеками vk_api/openai, но без сети.
Настоящий фоновый поток строит пересказы, индекс лежит в файле; проверяется задача #21:
  1. книга из папки RAG2 попадает в rag2_index.db, в БЯМ уходит запрос пересказа, вектор — от пересказа;
  2. /раг2 вкл: вопрос без слов книги находит пересказ, а в запрос ответа идёт исходный фрагмент;
  3. /раг вкл: тот же вопрос ищется только в индексе папки RAG;
  4. после перезапуска оплаченные пересказы не запрашиваются снова.

Запуск: python3 experiments/smoke_bot9_rag2_main.py
"""
import importlib.util
import os
import sqlite3
import tempfile
import time
from types import SimpleNamespace

import vk_api
import vk_api.bot_longpoll as bl
import openai

HERE = os.path.dirname(os.path.abspath(__file__))
BOT9 = os.path.join(HERE, "..", "Прочее", "bot9", "bot9.py")
PEER = 2000000001
FRAGMENT = "Иван собрал чемодан и утром покинул город навсегда."
SUMMARY = "Пересказ: герой решил уехать из-за ссоры."
QUESTION = "Грань, почему герой решил уехать?"

SENT, SUMMARY_CALLS, ANSWER_CALLS, EMBEDDED = [], [], [], []
SCRIPT = []


def load_bot9():
    spec = importlib.util.spec_from_file_location("bot9", BOT9)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeApi:
    class messages:
        @staticmethod
        def send(**p):
            SENT.append(p["message"])
            return [{"peer_id": int(p["peer_ids"]), "message_id": 0,
                     "conversation_message_id": 100 + len(SENT)}]

        @staticmethod
        def getConversations(count):
            return {"items": []}

        @staticmethod
        def getConversationMembers(peer_id):
            return {"items": []}

        @staticmethod
        def getHistory(**kw):
            return {"items": []}

    class users:
        @staticmethod
        def get(user_ids, **kw):
            return [{"id": int(i), "first_name": "Пётр", "last_name": ""} for i in str(user_ids).split(",")]

    class groups:
        @staticmethod
        def getById(**kw):
            return [{"id": 5, "name": "Сообщество"}]


class FakeSession:
    def __init__(self, token):
        pass

    def get_api(self):
        return FakeApi()


class FakeLongPoll:
    def __init__(self, session, group_id, wait=25):
        self.started = time.time()

    def check(self):
        time.sleep(0.05)
        assert time.time() - self.started < 30, f"сценарий завис: {SENT}"
        if SCRIPT and SCRIPT[0][0]():
            return SCRIPT.pop(0)[1]()
        return []


class FakeOpenAI:
    def __init__(self, **kw):
        def create(**k):
            user = k["messages"][1]["content"]
            if "Прочитай этот фрагмент книги" in user:
                SUMMARY_CALLS.append(user)
                content = SUMMARY
            else:
                ANSWER_CALLS.append("\n".join(m["content"] for m in k["messages"]))
                content = "Ответ по фрагменту [1]"
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content),
                                                            finish_reason="stop")], usage=None)

        def embed(model, input):
            EMBEDDED.append(list(input))
            return SimpleNamespace(data=[SimpleNamespace(index=i, embedding=[1.0 if "уехать" in t else 0.0, 0.01])
                                         for i, t in enumerate(input)])
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=create))
        self.embeddings = SimpleNamespace(create=embed)


vk_api.VkApi = FakeSession
bl.VkBotLongPoll = FakeLongPoll
openai.OpenAI = FakeOpenAI
os.environ.update(VK_GROUP_ID="5", VK_TOKEN="x", PROXYAPI_KEY="x", ADMIN_IDS="7, 8", GROUP_NAME="грань")
os.environ.pop("BOT_DEBUG", None)


def new(cmid, text):
    return bl.VkBotMessageEvent({"type": "message_new", "group_id": 5, "object": {"message": {
        "id": 0, "conversation_message_id": cmid, "peer_id": PEER, "from_id": 7,
        "text": text, "date": int(time.time()), "attachments": [], "fwd_messages": []}}})


def configure(bot9, d):
    bot9.BATCH_WAIT_SEC = 0.2
    bot9.LLM_MIN_INTERVAL_SEC = 0
    bot9.RAG2_SUMMARY_INTERVAL_SEC = 0
    bot9.RAG2_IDLE_POLL_SEC = 1
    bot9.DB_PATH = os.path.join(d, "gran9.db")
    bot9.OLD_DB_PATH = os.path.join(d, "нет_gran8.db")
    bot9.RAG_DIR = os.path.join(d, "RAG")
    bot9.RAG_INDEX_PATH = os.path.join(d, "rag_index.db")
    bot9.RAG2_DIR = os.path.join(d, "RAG2")
    bot9.RAG2_INDEX_PATH = os.path.join(d, "rag2_index.db")
    bot9.ROLE_FILE = os.path.join(d, "Роль.txt")


def run_main(bot9):
    try:
        bot9.main(["--no-terminal"])
    except SystemExit as e:
        return e.code
    raise AssertionError("main() должен завершаться через sys.exit")


with tempfile.TemporaryDirectory() as d:
    os.makedirs(os.path.join(d, "RAG"))
    os.makedirs(os.path.join(d, "RAG2"))
    with open(os.path.join(d, "RAG", "Сократ.txt"), "w", encoding="utf-8") as f:
        f.write("Сократ учил, что добродетель есть знание.")
    with open(os.path.join(d, "RAG2", "Роман.txt"), "w", encoding="utf-8") as f:
        f.write(FRAGMENT)

    bot9 = load_bot9()
    configure(bot9, d)
    index2 = os.path.join(d, "rag2_index.db")

    def summarized():
        with sqlite3.connect(index2) as db:
            return db.execute("SELECT COUNT(*) FROM vectors").fetchone()[0] == 1

    SCRIPT[:] = [
        (summarized, lambda: [new(1, "/раг2 вкл")]),
        (lambda: len(SENT) >= 1, lambda: [new(2, QUESTION)]),
        (lambda: len(SENT) >= 2, lambda: [new(3, "/раг вкл")]),
        (lambda: len(SENT) >= 3, lambda: [new(4, QUESTION)]),
        (lambda: len(SENT) >= 4, lambda: [new(5, "Грань, стоп машина")]),
    ]
    assert run_main(bot9) == 0
    print("Отправлено:", SENT)
    assert len(SUMMARY_CALLS) == 1 and FRAGMENT in SUMMARY_CALLS[0], SUMMARY_CALLS
    assert [SUMMARY] in EMBEDDED, EMBEDDED
    assert not [t for batch in EMBEDDED for t in batch if FRAGMENT in t], "исходный текст не векторизуется"
    assert "Режим РАГ2 включён" in SENT[0], SENT
    assert FRAGMENT in ANSWER_CALLS[0] and SUMMARY not in ANSWER_CALLS[0], ANSWER_CALLS[0]
    assert "Ответ по фрагменту" in SENT[1] and "Роман" in SENT[1], SENT
    assert "Режим базы знаний включён" in SENT[2], SENT
    assert FRAGMENT not in ANSWER_CALLS[1], "/раг вкл не смотрит в индекс 2"
    assert SENT[3].startswith(bot9.RAG_NO_RESULT_PREFIX), SENT[3]
    with sqlite3.connect(index2) as db:
        linked = db.execute("SELECT c.text FROM vectors v JOIN chunks c ON c.id=v.chunk_id").fetchall()
    assert linked == [(FRAGMENT,)], linked

    # ---------- перезапуск: пересказ уже оплачен, индекс 2 не пересобирается ----------
    bot9 = load_bot9()
    configure(bot9, d)
    SENT.clear()
    SCRIPT[:] = [(lambda: True, lambda: [new(6, "Грань, стоп машина")])]
    assert run_main(bot9) == 0
    assert len(SUMMARY_CALLS) == 1, SUMMARY_CALLS

print("OK")
