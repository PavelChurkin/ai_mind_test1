"""
Смоук-тест init()/main() бота bot8 с настоящими библиотеками vk_api/openai,
но без сети: VkApi, VkBotLongPoll и OpenAI подменены. Проверяет главный цикл:

  1. два обращения подряд → одна пауза BATCH_WAIT_SEC → один ответ; модель сначала
     «израсходовала лимит на рассуждение» (пустой ответ, finish_reason=length) — бот
     повторяет запрос с EMPTY_ANSWER_RETRY_TOKENS и отвечает (задача #17, пустые ответы);
  2. событие message_reply не дублирует ответ;
  3. «Грань, РАГ …» отвечает по книге из папки RAG с источником, «РАГ» без находок —
     «В базе знаний ничего не нашлось» без запроса к модели;
  4. «стоп машина» завершает бота с кодом 0, лог пишется в --log-file;
  5. --restart-after-hours: проработав срок и дождавшись тишины, бот выходит с кодом 75.

Запуск (нужны vk_api, openai, python-dotenv, для РАГ — pymorphy3):
    python3 experiments/smoke_bot8_main.py
"""
import io
import os
import time
import sqlite3
import tempfile
import importlib.util
from types import SimpleNamespace
from unittest import mock

import vk_api
import vk_api.bot_longpoll as bl
import openai

HERE = os.path.dirname(os.path.abspath(__file__))
BOT8 = os.path.join(HERE, "..", "Прочее", "bot8", "bot8.py")
PEER = 2000000001

SENT = []
LLM_CALLS = []


def load_bot8():
    """Свежий экземпляр модуля: у каждого запуска main() свои глобальные переменные."""
    spec = importlib.util.spec_from_file_location("bot8", BOT8)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeApi:
    class messages:
        @staticmethod
        def send(**p):
            SENT.append(p)
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
            return [{"id": 5, "name": "Грань"}]


class FakeSession:
    def __init__(self, token):
        pass

    def get_api(self):
        return FakeApi()


def new(cmid, text):
    return bl.VkBotMessageEvent({"type": "message_new", "group_id": 5, "object": {"message": {
        "id": 0, "conversation_message_id": cmid, "peer_id": PEER, "from_id": 7,
        "text": text, "date": int(time.time()), "attachments": [], "fwd_messages": []}}})


def reply_echo(sent):
    """Собственное сообщение бота приходит событием message_reply."""
    return bl.VkBotMessageEvent({"type": "message_reply", "group_id": 5, "object": {
        "id": 0, "conversation_message_id": 101, "peer_id": PEER, "from_id": -5,
        "text": sent["message"], "date": int(time.time())}})


SCRIPT = []          # шаги для FakeLongPoll текущего запуска: (условие, события)


class FakeLongPoll:
    """check() как у настоящего VkBotLongPoll: список событий за wait секунд.
    Шаг сценария выдаётся, когда выполнено его условие (например, бот уже ответил)."""

    def __init__(self, session, group_id, wait=25):
        self.wait = wait
        self.started = time.time()

    def check(self):
        time.sleep(0.05)
        assert time.time() - self.started < 30, "сценарий завис: " + repr([p["message"] for p in SENT])
        if SCRIPT and SCRIPT[0][0]():
            return SCRIPT.pop(0)[1]()
        return []


class FakeOpenAI:
    """Первый обычный запрос — пустой ответ с finish_reason=length (как у «рассуждалки»)."""

    def __init__(self, **kw):
        def create(**k):
            LLM_CALLS.append(k)
            system = k["messages"][0]["content"]
            if "НЕТ_ОТВЕТА" in system:                      # промпт РАГ: только по книгам
                content, finish = "Сократ учил, что добродетель есть знание [1].", "stop"
            elif len(LLM_CALLS) == 1:
                content, finish = "", "length"
            else:
                content, finish = "Привет, Пётр! Дела хорошо.", "stop"
            usage = SimpleNamespace(prompt_tokens=100, completion_tokens=k["max_tokens"] if not content else 20,
                                    completion_tokens_details=SimpleNamespace(reasoning_tokens=k["max_tokens"]
                                                                              if not content else 0))
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content),
                                                            finish_reason=finish)], usage=usage)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=create))


vk_api.VkApi = FakeSession
bl.VkBotLongPoll = FakeLongPoll
openai.OpenAI = FakeOpenAI
os.environ.update(VK_GROUP_ID="5", VK_TOKEN="x", PROXYAPI_KEY="x", ADMIN_IDS="7, 8")
os.environ.pop("BOT_DEBUG", None)


def configure(bot8, d):
    bot8.BATCH_WAIT_SEC = 0.3
    bot8.LLM_MIN_INTERVAL_SEC = 0
    bot8.DB_PATH = os.path.join(d, "gran8.db")
    bot8.OLD_DB_PATH = os.path.join(d, "нет_gran7.db")
    bot8.RAG_DIR = os.path.join(d, "RAG")
    bot8.RAG_INDEX_PATH = os.path.join(d, "rag_index.db")
    bot8.ROLE_FILE = os.path.join(d, "Роль.txt")


def run_main(bot8, argv):
    try:
        bot8.main(argv)
    except SystemExit as e:
        return e.code
    raise AssertionError("main() должен завершаться через sys.exit")


def texts():
    return [p["message"] for p in SENT]


with tempfile.TemporaryDirectory() as d:
    os.makedirs(os.path.join(d, "RAG"))
    with open(os.path.join(d, "RAG", "Сократ.txt"), "w", encoding="cp1251") as f:
        f.write("Глава 1. Сократ\n\nСократ учил, что добродетель есть знание.\n")
    with open(os.path.join(d, "Роль.txt"), "w", encoding="utf-8") as f:
        f.write("Ты — Грань, собеседник-философ.")
    log_file = os.path.join(d, "logs", "bot8.log")

    # ---------- запуск 1: обычная работа и «стоп машина» ----------
    bot8 = load_bot8()
    configure(bot8, d)
    SCRIPT[:] = [
        (lambda: True, lambda: [new(1, "Грань, привет!"), new(2, "Грань, как дела?")]),
        (lambda: SENT, lambda: [reply_echo(SENT[0])]),
        (lambda: True, lambda: [new(3, "Грань, РАГ чему учил Сократ?")]),
        (lambda: len(SENT) >= 2, lambda: [new(4, "Грань, РАГ квантовая хромодинамика")]),
        (lambda: len(SENT) >= 3, lambda: [new(5, "Грань, стоп машина")]),
    ]
    code = run_main(bot8, ["--log-file", log_file, "--no-terminal"])
    rows = sqlite3.connect(bot8.DB_PATH).execute(
        "SELECT cmid, from_id, text FROM messages WHERE peer_id=? ORDER BY id", (PEER,)).fetchall()

    print("Отправлено:", texts())
    print("Запросы к LLM:", [(c["max_tokens"], c["messages"][0]["content"][:40]) for c in LLM_CALLS])
    assert code == 0, code
    assert bot8.ADMIN_IDS == {7, 8}, bot8.ADMIN_IDS
    chat_calls = [c for c in LLM_CALLS if "НЕТ_ОТВЕТА" not in c["messages"][0]["content"]]
    assert len(chat_calls) == 2, "одна пачка: пустой ответ и повтор с большим лимитом"
    assert "НОВЫЕ СООБЩЕНИЯ (их 2" in chat_calls[0]["messages"][1]["content"]
    assert chat_calls[0]["max_tokens"] < chat_calls[1]["max_tokens"] == bot8.EMPTY_ANSWER_RETRY_TOKENS
    assert chat_calls[0]["messages"][0]["content"].startswith("Ты — Грань, собеседник-философ."), \
        "роль по умолчанию — из Роль.txt"
    assert texts()[0] == "Привет, Пётр! Дела хорошо.", texts()
    assert len([r for r in rows if r[2] == "Привет, Пётр! Дела хорошо."]) == 1, "ответ бота задублирован"
    assert texts()[1].startswith("Сократ учил, что добродетель есть знание [1]."), texts()[1]
    assert "Источники:\n[1] «Сократ», Глава 1. Сократ" in texts()[1], texts()[1]
    assert texts()[2] == "⚙️ В базе знаний ничего не нашлось.", texts()[2]
    assert len(LLM_CALLS) == 3, "на «ничего не нашлось» модель не вызывается"
    assert texts()[-1] == "Останавливаюсь.", texts()
    with open(log_file, encoding="utf-8") as f:
        log_text = f.read()
    assert "из них рассуждение" in log_text and "повтор с лимитом" in log_text, log_text
    assert "Бот остановлен." in log_text

    # ---------- запуск 2: плановый перезапуск ----------
    bot8 = load_bot8()
    configure(bot8, d)
    SENT.clear()
    LLM_CALLS.clear()
    SCRIPT[:] = [(lambda: True, lambda: [new(6, "Грань, ты тут?")])]
    started = time.time()
    code = run_main(bot8, ["--log-file", log_file, "--no-terminal", "--restart-after-hours", str(1 / 3600)])
    print("Запуск 2: код", code, "отправлено", texts(), f"за {time.time() - started:.1f} с")
    assert code == bot8.RESTART_EXIT_CODE == 75, code
    assert texts() == ["Привет, Пётр! Дела хорошо."], "перед перезапуском бот дождался ответа"
    assert time.time() - started >= 1

    # ---------- запуск 3: команды stdin исполняются главным потоком, только в терминал ----------
    bot8 = load_bot8()
    configure(bot8, d)
    SENT.clear()
    LLM_CALLS.clear()
    SCRIPT.clear()
    terminal = io.StringIO(f"/чат {PEER}\n/роль терминальный философ\n/контекст 7\n/помощь\n0\n")
    output = io.StringIO()
    with mock.patch('sys.stdin', terminal), mock.patch('sys.stdout', output):
        code = run_main(bot8, ["--log-file", log_file])
    assert code == 0, code
    assert not SENT and not LLM_CALLS, (SENT, LLM_CALLS)
    assert 'Команды бота' in output.getvalue(), output.getvalue()
    assert 'Роль обновлена' in output.getvalue(), output.getvalue()
    with sqlite3.connect(bot8.DB_PATH) as db:
        assert db.execute('SELECT role FROM roles WHERE peer_id=?', (PEER,)).fetchone()[0] == 'терминальный философ'
        setting = db.execute('SELECT value FROM state WHERE key=?', (f'ctx_{PEER}',)).fetchone()[0]
        assert setting.split('|')[0] == '7', setting
    print('Запуск 3: команды stdin и остановка, ни одного сообщения в ВК — OK')

print("OK")
