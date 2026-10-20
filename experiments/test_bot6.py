"""
Тесты для Прочее/bot6/bot6.py (задача #15). Без токенов и сети: ВК и LLM подменены,
время — управляемые часы (bot6.now). Первая часть — тесты bot5, перенесённые без изменения
смысла; новые возможности bot6 — в test_bot6_features.py.

Запуск: python3 -m unittest experiments/test_bot6.py -v
"""
import os
import json
import random
import sqlite3
import tempfile
import unittest
import importlib.util
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
BOT6_DIR = os.path.join(HERE, "..", "Прочее", "bot6")

spec = importlib.util.spec_from_file_location("bot6", os.path.join(BOT6_DIR, "bot6.py"))
bot6 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot6)
filosof = bot6.filosof

GROUP = 555
BOT = -GROUP
CHAT = 2000000001
USER = 101
ADMIN = 202
PRIVATE = 303
START = int(datetime(2026, 10, 16, 15, 0).timestamp())


class Obj:
    """Пространство имён для vk.messages / vk.users и т. п."""


class FakeVk:
    def __init__(self):
        self.sent = []
        self.history = {}              # peer_id -> сообщения от старых к новым
        self.history_calls = []
        self.next_cmid = 1000
        self.messages = Obj()
        self.messages.send = self._send
        self.messages.getHistory = self._history
        self.messages.getConversationMembers = lambda peer_id, **kw: {"items": [
            {"member_id": USER}, {"member_id": ADMIN, "is_admin": True}, {"member_id": 9, "is_owner": True}]}
        self.users = Obj()
        self.users.get = lambda user_ids, **kw: [
            {"id": int(i), "first_name": f"Имя{i}", "last_name": ""} for i in user_ids.split(",")]
        self.groups = Obj()
        self.groups.getById = lambda group_ids, **kw: {
            "groups": [{"id": int(i), "name": f"Паблик{i}"} for i in group_ids.split(",")]}

    def _send(self, **params):
        self.sent.append(params)
        self.next_cmid += 1
        return [{"peer_id": int(params["peer_ids"]), "message_id": 0,
                 "conversation_message_id": self.next_cmid}]

    def _history(self, peer_id, count, offset=0, **kw):
        self.history_calls.append((count, offset))
        newest_first = list(reversed(self.history.get(peer_id, [])))
        return {"items": newest_first[offset:offset + count]}

    def texts(self):
        return [p["message"] for p in self.sent]


class FakeLLM:
    def __init__(self, answer="Ответ Грани"):
        self.calls = []
        self.answer = answer
        self.chat = Obj()
        self.chat.completions = Obj()
        self.chat.completions.create = self._create

    def _create(self, **kw):
        self.calls.append(kw)
        answer = self.answer(kw) if callable(self.answer) else self.answer
        finish = "stop"
        if isinstance(answer, tuple):           # (текст, finish_reason)
            answer, finish = answer
        msg = Obj()
        msg.content = answer
        choice = Obj()
        choice.message = msg
        choice.finish_reason = finish
        resp = Obj()
        resp.choices = [choice]
        resp.usage = None
        return resp

    def prompt(self, i=-1):
        content = self.calls[i]["messages"][1]["content"]
        return content if isinstance(content, str) else content[0]["text"]


def chat_msg(cmid, text, from_id=USER, ts=None, peer_id=CHAT, **extra):
    """Сообщение беседы так, как его отдаёт Bots Long Poll: id = 0."""
    m = {"id": 0, "conversation_message_id": cmid, "peer_id": peer_id,
         "from_id": from_id, "text": text, "date": ts}
    m.update(extra)
    return m


class Bot6Case(unittest.TestCase):
    def setUp(self):
        self.t = float(START)
        bot6.now = lambda: self.t
        bot6.DB = bot6.db_init(":memory:")
        bot6.vk = FakeVk()
        bot6.llm_client = self.llm = FakeLLM()
        bot6.BOT_GROUP_ID = BOT
        bot6.VK_USER_TOKEN = None
        bot6.ADMIN_IDS = set()
        bot6.LLM_MIN_INTERVAL_SEC = 0
        bot6.IMAGE_MODE = "url"
        bot6.STOP_REQUESTED = False
        bot6.PEERS.clear()
        bot6._admin_cache.clear()
        bot6._image_cache.clear()
        bot6._ssl_contexts = None
        bot6._role_file_cache = (None, None)
        bot6.DEBUG_MODE = False
        bot6._last_limit_check = 0.0
        bot6.LAST_LLM_ERROR = ""
        # файл роли, база знаний и их индексы — во временной папке, а не рядом с ботом
        self.tmp = tempfile.TemporaryDirectory()
        bot6.ROLE_FILE = os.path.join(self.tmp.name, "Роль.txt")
        bot6.RAG_DIR = os.path.join(self.tmp.name, "RAG")
        bot6.RAG_INDEX_PATH = os.path.join(self.tmp.name, "rag_index.db")
        bot6.KB = None
        self.cmid = 0

    def tearDown(self):
        bot6.DB.close()
        if bot6.KB is not None:
            bot6.KB.close()
            bot6.KB = None
        self.tmp.cleanup()

    def tick(self, sec):
        self.t += sec
        bot6.process_pending()

    def say(self, text, from_id=USER, peer_id=CHAT, **extra):
        self.cmid += 1
        msg = chat_msg(self.cmid, text, from_id=from_id, ts=int(self.t), peer_id=peer_id, **extra)
        bot6.handle_message(msg)
        return msg

    def sent(self):
        return bot6.vk.texts()


# ---------- R2: личные сообщения и права ----------
class TestPrivateAndAdmins(Bot6Case):
    def test_private_messages_ignored_by_default(self):
        self.assertFalse(bot6.ALLOW_PRIVATE_MESSAGES)
        self.say("Грань, привет", from_id=PRIVATE, peer_id=PRIVATE)
        self.say("/помощь", from_id=PRIVATE, peer_id=PRIVATE)
        self.tick(30)
        self.assertEqual(self.sent(), [])
        self.assertEqual(self.llm.calls, [])
        # но сообщения сохранены
        self.assertEqual(bot6.DB.execute("SELECT COUNT(*) FROM messages WHERE peer_id=?",
                                         (PRIVATE,)).fetchone()[0], 2)

    def test_private_allowed_any_message_answered_and_user_is_admin(self):
        bot6.ALLOW_PRIVATE_MESSAGES = True
        try:
            self.say("просто привет", from_id=PRIVATE, peer_id=PRIVATE)
            self.tick(9)
            self.assertEqual(self.sent(), ["Ответ Грани"])
            self.say("/лимит токенов 30000", from_id=PRIVATE, peer_id=PRIVATE)
            self.assertEqual(bot6.tokens_limit(PRIVATE), 30000)
        finally:
            bot6.ALLOW_PRIVATE_MESSAGES = False

    def test_chat_admins_from_conversation_members(self):
        self.assertTrue(bot6.is_admin(CHAT, ADMIN))
        self.assertTrue(bot6.is_admin(CHAT, 9))        # создатель
        self.assertFalse(bot6.is_admin(CHAT, USER))
        self.assertTrue(bot6.is_admin(PRIVATE, PRIVATE))

    def test_admin_ids_from_env_and_api_failure(self):
        def boom(**kw):
            raise RuntimeError("bot is not admin of the chat")
        bot6.vk.messages.getConversationMembers = boom
        bot6.ADMIN_IDS = {USER}
        self.assertTrue(bot6.is_admin(CHAT, USER))
        self.assertFalse(bot6.is_admin(CHAT, ADMIN))


# ---------- R3: /помощь ----------
class TestHelp(Bot6Case):
    def test_help_lists_all_commands_with_rights(self):
        self.say("/помощь")
        text = self.sent()[0]
        self.assertTrue(text.startswith(bot6.SYSTEM_MARK))
        for cmd in ("/пересказ N", "/пересказ N от", "/мысль", "/мысль подробно", "/пауза",
                    "/забудь", "/роль", "/помощь", "/лимит токенов", "/лимит символов",
                    "/сбросить лимит сообщений", "Грань, стоп машина"):
            self.assertIn(cmd, text)
        self.assertIn("Кто: админ", text)
        self.assertIn("Кто: все", text)
        self.assertIn("лимит токенов ответа: 800", text)
        self.assertEqual(self.llm.calls, [])            # помощь не тратит LLM

    def test_help_does_not_go_to_llm_context(self):
        self.say("/помощь")
        self.say("Грань, что нового?")
        self.tick(9)
        self.assertNotIn("Команды бота", self.llm.prompt())


# ---------- R4: /пауза ----------
class TestPause(Bot6Case):
    def test_pause_collects_and_answers_after_minute(self):
        self.say("/пауза")
        self.say("Грань, вопрос один", from_id=USER)
        self.tick(20)
        self.say("Грань, вопрос два", from_id=ADMIN)
        self.tick(20)
        self.assertEqual(self.llm.calls, [])            # на паузе — молчит
        self.tick(25)                                   # минута прошла
        self.assertEqual(len(self.llm.calls), 1)
        prompt = self.llm.prompt()
        self.assertIn("вопрос один", prompt)
        self.assertIn("вопрос два", prompt)
        self.assertIn("НОВЫЕ СООБЩЕНИЯ (их 2", prompt)

    def test_pause_minutes_and_unpause(self):
        self.say("/пауза 5")
        self.say("Грань, ау")
        self.tick(120)
        self.assertEqual(self.llm.calls, [])
        self.say("/пауза 0")
        self.tick(1)
        self.assertEqual(len(self.llm.calls), 1)

    def test_llm_commands_wait_for_pause_end(self):
        self.say("Тема про космос")
        self.say("/пауза")
        self.say("/пересказ 10")
        self.say("/помощь")                              # служебная — сразу
        self.assertEqual(self.llm.calls, [])
        self.assertTrue(any("Команды бота" in s for s in self.sent()))
        self.tick(61)
        self.assertEqual(len(self.llm.calls), 1)
        self.assertIn("Тема про космос", self.llm.prompt())

    def test_new_messages_after_expired_pause_still_wait_batch(self):
        self.say("/пауза")
        self.tick(61)                                   # пауза кончилась без обращений
        self.say("Грань, привет")
        self.tick(1)
        self.assertEqual(self.llm.calls, [])            # ждём BATCH_WAIT_SEC, как обычно
        self.tick(8)
        self.assertEqual(len(self.llm.calls), 1)


# ---------- R5, R13: лимиты токенов и символов ----------
class TestLimits(Bot6Case):
    def test_token_limit_admin_only_in_chat(self):
        self.say("/лимит токенов 30000", from_id=USER)
        self.assertIn("только админу", self.sent()[-1])
        self.assertEqual(bot6.tokens_limit(CHAT), 800)
        self.say("/лимит токенов 30000", from_id=ADMIN)
        self.assertEqual(bot6.tokens_limit(CHAT), 30000)
        self.say("Грань, напиши роман")
        self.tick(9)
        self.assertEqual(self.llm.calls[-1]["max_tokens"], 30000)

    def test_token_limit_reset_and_auto_return(self):
        self.say("/лимит токенов 5000", from_id=ADMIN)
        self.say("/лимит токенов сброс", from_id=ADMIN)
        self.assertEqual(bot6.tokens_limit(CHAT), 800)
        self.say("/лимит токенов 5000", from_id=ADMIN)
        self.t += bot6.TOKEN_LIMIT_RESET_MINUTES * 60
        self.assertEqual(bot6.tokens_limit(CHAT), 800)

    def test_token_limit_capped(self):
        self.say("/лимит токенов 999999", from_id=ADMIN)
        self.assertEqual(bot6.tokens_limit(CHAT), bot6.MAX_TOKENS_CAP)

    def test_chars_limit_admin_only(self):
        self.say("/лимит символов 20000", from_id=USER)
        self.assertEqual(bot6.chars_limit(CHAT), bot6.MAX_CONTEXT_CHARS)
        self.say("/лимит символов 20000", from_id=ADMIN)
        self.assertEqual(bot6.chars_limit(CHAT), 20000)
        self.say("/лимит символов стандарт", from_id=ADMIN)
        self.assertEqual(bot6.chars_limit(CHAT), bot6.MAX_CONTEXT_CHARS)

    def test_limits_are_per_chat(self):
        self.say("/лимит токенов 3000", from_id=ADMIN)
        self.assertEqual(bot6.tokens_limit(CHAT + 1), 800)


# ---------- R6: разбивка длинных сообщений ----------
class TestSplit(Bot6Case):
    def test_long_answer_sent_in_parts(self):
        self.llm.answer = ("слово " * 1500).strip()   # 8999 символов
        self.say("Грань, расскажи подробно")
        self.tick(9)
        parts = self.sent()
        self.assertEqual(len(parts), 3)
        self.assertTrue(all(len(p) <= bot6.VK_MESSAGE_LIMIT for p in parts))
        self.assertEqual(" ".join(parts), self.llm.answer)
        self.assertIn("forward", bot6.vk.sent[0])       # reply только у первой части
        self.assertNotIn("forward", bot6.vk.sent[1])


# ---------- R7: растущий контекст ----------
class TestDynamicContext(Bot6Case):
    def test_context_grows_in_active_argument_and_resets_after_silence(self):
        self.assertEqual(bot6.context_size(CHAT), bot6.CONTEXT_SIZE)
        # ответы бота всё чаще: 100 с, потом 50 с
        for gap in (0, 100, 50):
            self.t += gap
            self.say("Грань, а ты что думаешь?")
            self.tick(9)
        self.assertTrue(bot6.peer_state(CHAT).growing)
        size = bot6.context_size(CHAT)
        for i in range(5):
            self.t += 10
            self.say(f"реплика {i}")
        self.assertEqual(bot6.context_size(CHAT), size + 5)
        self.t += bot6.CONTEXT_RESET_MINUTES * 60
        self.assertEqual(bot6.context_size(CHAT), bot6.CONTEXT_SIZE)
        self.say("тишина кончилась")
        self.assertFalse(bot6.peer_state(CHAT).growing)
        self.assertEqual(bot6.context_size(CHAT), bot6.CONTEXT_SIZE)

    def test_no_growth_when_answers_slow_down(self):
        for gap in (0, 50, 100):
            self.t += gap
            self.say("Грань, ну?")
            self.tick(9)
        self.assertFalse(bot6.peer_state(CHAT).growing)

    def test_grown_context_used_in_prompt(self):
        for i in range(60):
            bot6.db_add_message(CHAT, USER, False, f"старое {i}", int(self.t) - 600 + i, cmid=5000 + i)
        st = bot6.peer_state(CHAT)
        st.ctx_size, st.growing, st.last_activity = 50, True, self.t
        self.say("Грань, итог?")
        self.tick(9)
        prompt = self.llm.prompt()
        # 50 + 1: обращение «Грань, итог?» в споре тоже увеличило контекст
        self.assertEqual(bot6.context_size(CHAT), 51)
        self.assertIn("старое 9\n", prompt)
        self.assertNotIn("старое 8\n", prompt)


# ---------- R8: пакетный ответ ----------
class TestBatch(Bot6Case):
    def test_waits_8_seconds_and_answers_once(self):
        self.say("Грань, первое")
        self.tick(5)
        self.say("Грань, второе", from_id=ADMIN)
        self.tick(5)
        self.assertEqual(self.llm.calls, [])            # с последнего прошло 5 с
        self.tick(3)
        self.assertEqual(len(self.llm.calls), 1)
        self.assertEqual(self.sent(), ["Ответ Грани"])
        prompt = self.llm.prompt()
        self.assertIn("Имя101: Грань, первое", prompt)
        self.assertIn("Имя202: Грань, второе", prompt)
        self.assertNotIn("ИСТОРИЯ:\n[16.10 15:00] Имя101: Грань, первое", prompt)  # не в истории
        # reply — на последнее обращение
        self.assertIn('"conversation_message_ids": [2]', bot6.vk.sent[0]["forward"])

    def test_max_wait_caps_batch(self):
        for _ in range(10):
            self.say("Грань, ещё")
            self.tick(7)
        self.assertGreaterEqual(len(self.llm.calls), 1)

    def test_not_addressed_messages_not_answered(self):
        self.say("просто болтаем")
        self.tick(20)
        self.assertEqual(self.llm.calls, [])


# ---------- R9: дневной лимит ----------
class TestDailyLimit(Bot6Case):
    def setUp(self):
        super().setUp()
        bot6.DAILY_MESSAGE_LIMIT = 4

    def tearDown(self):
        bot6.DAILY_MESSAGE_LIMIT = 100
        super().tearDown()

    def ask(self):
        self.say("Грань, вопрос")
        self.tick(9)

    def test_notifications_and_stop(self):
        notes = []
        for _ in range(4):
            before = len(self.sent())
            self.ask()
            notes.append([s for s in self.sent()[before:] if s.startswith(bot6.SYSTEM_MARK)])
        self.assertIn("1 из 4", notes[0][0])
        self.assertIn("25%", notes[0][0])
        self.assertIn("50%", notes[1][0])
        # при лимите 4 пороги 90 % и 100 % совпали (4 из 4) — одно оповещение о старшем
        self.assertEqual(len(notes[3]), 1)
        self.assertIn("исчерпан", notes[3][0])
        self.assertEqual(notes[2], [])                  # 3 из 4 = 75 % — без оповещения
        calls = len(self.llm.calls)
        self.ask()
        self.assertEqual(len(self.llm.calls), calls)    # LLM больше не вызывается
        self.assertIn("лимит сообщений достигнут", self.sent()[-1])
        self.say("/мысль")
        self.assertEqual(len(self.llm.calls), calls)

    def test_percent_thresholds_with_100(self):
        bot6.DAILY_MESSAGE_LIMIT = 100
        hits = {}
        for i in range(1, 101):
            before = len(self.sent())
            bot6.count_answer(CHAT)
            if len(self.sent()) > before:
                hits[i] = self.sent()[-1]
        self.assertEqual(sorted(hits), [25, 50, 90, 100])

    def test_per_chat_and_global_scope(self):
        for _ in range(4):
            self.ask()
        self.assertFalse(bot6.daily_limit_reached(CHAT + 1))   # у другой беседы — свой лимит
        bot6.DAILY_LIMIT_PER_CHAT = False
        try:
            for _ in range(4):
                bot6.count_answer(CHAT + 1)
            self.assertTrue(bot6.daily_limit_reached(CHAT + 2))  # общий лимит на все беседы
        finally:
            bot6.DAILY_LIMIT_PER_CHAT = True

    def test_reset_next_day(self):
        for _ in range(4):
            self.ask()
        self.assertTrue(bot6.daily_limit_reached(CHAT))
        self.t += 24 * 3600
        self.assertFalse(bot6.daily_limit_reached(CHAT))

    def test_reset_command_admin_only(self):
        for _ in range(4):
            self.ask()
        self.say("/сбросить лимит сообщений", from_id=USER)
        self.assertTrue(bot6.daily_limit_reached(CHAT))
        self.say("/сбросить лимит сообщений", from_id=ADMIN)
        self.assertFalse(bot6.daily_limit_reached(CHAT))
        self.ask()
        self.assertEqual(self.sent()[-2:][0], "Ответ Грани")


# ---------- R10, R12: пересказ ----------
class TestSummary(Bot6Case):
    def fill(self, n, start_ts, step=60, peer_id=CHAT):
        for i in range(n):
            bot6.db_add_message(peer_id, USER, False, f"сообщение {i}", start_ts + i * step, cmid=10000 + i)

    def test_summary_over_100_admin_only(self):
        self.fill(150, START - 20000)
        self.say("/пересказ 150", from_id=USER)
        self.assertEqual(self.llm.calls, [])
        self.assertIn("только админу", self.sent()[-1])
        self.say("/пересказ 100", from_id=USER)
        self.assertEqual(len(self.llm.calls), 1)
        self.say("/пересказ 150", from_id=ADMIN)
        self.assertEqual(len(self.llm.calls), 2)
        # 150 последних строк БД: 4 — команды и ответы бота, остальные — сообщения с 4-го
        self.assertIn("] Имя101: сообщение 4\n", self.llm.prompt())
        self.assertNotIn("сообщение 3\n", self.llm.prompt())

    def test_summary_from_date(self):
        self.fill(300, START - 300 * 60)                # по сообщению в минуту
        self.say("/пересказ 10 от 12:00 16.10.2026")
        prompt = self.llm.prompt()
        lines = [l for l in prompt.splitlines() if "сообщение" in l]
        self.assertEqual(len(lines), 10)
        self.assertIn("16.10 12:00", lines[0])
        self.assertIn("Пересказ 10 сообщений с 12:00 16.10.2026", self.sent()[-1])

    def test_summary_from_date_loads_missing_history(self):
        # в ВК — 500 сообщений, по одному в минуту с 06:40, в БД — только последние 100
        vkhist = [chat_msg(i + 1, f"вк {i}", ts=START - 500 * 60 + i * 60) for i in range(500)]
        bot6.vk.history[CHAT] = vkhist
        for m in vkhist[-100:]:
            bot6.store_vk_message(CHAT, m)
        self.cmid = 1000
        self.say("/пересказ 5 от 9:00 16.10.2026")
        self.assertTrue(bot6.vk.history_calls)
        self.assertEqual(bot6.vk.history_calls[0][1], 0)   # 100 в БД < 200 запаса
        prompt = self.llm.prompt()
        self.assertIn("] Имя101: вк 140\n", prompt)        # 9:00 = 6:40 + 140 мин
        self.assertNotIn("вк 139\n", prompt)
        n = bot6.DB.execute("SELECT COUNT(*) FROM messages WHERE peer_id=?", (CHAT,)).fetchone()[0]
        self.assertLess(n, 502)                             # грузили не всю историю, а до 9:00
        self.assertGreaterEqual(n, 400)

    def test_summary_before_chat_start_ignored(self):
        vkhist = [chat_msg(i + 1, f"вк {i}", ts=START - 50 * 60 + i * 60) for i in range(50)]
        bot6.vk.history[CHAT] = vkhist
        self.cmid = 1000
        self.say("/пересказ 10 от 01.01.2026")
        self.assertIn("вк 0\n", self.llm.prompt() + "\n")
        self.assertIn("раньше", self.sent()[-1])
        calls = len(bot6.vk.history_calls)
        self.say("/пересказ 10 от 01.01.2026")
        self.assertEqual(len(bot6.vk.history_calls), calls)  # начало чата запомнено

    def test_bad_date(self):
        self.say("/пересказ 10 от вчера")
        self.assertIn("Не понял дату", self.sent()[-1])
        self.assertEqual(self.llm.calls, [])

    def test_summary_tokens_follow_limit(self):
        self.fill(20, START - 3000)
        self.say("/лимит токенов 9000", from_id=ADMIN)
        self.say("/пересказ 20")
        self.assertEqual(self.llm.calls[-1]["max_tokens"], 9000)


class TestParseStartTime(unittest.TestCase):
    T = datetime(2026, 10, 16, 15, 0).timestamp()

    def p(self, s):
        ts = bot6.parse_start_time(s, self.T)
        return datetime.fromtimestamp(ts) if ts is not None else None

    def test_formats(self):
        self.assertEqual(self.p("12:00 16.10.2026"), datetime(2026, 10, 16, 12, 0))
        self.assertEqual(self.p("16.10.2026 12:00"), datetime(2026, 10, 16, 12, 0))
        self.assertEqual(self.p("16.10.26"), datetime(2026, 10, 16))
        self.assertEqual(self.p("1.9"), datetime(2026, 9, 1))
        self.assertEqual(self.p("10:30"), datetime(2026, 10, 16, 10, 30))

    def test_future_without_year_means_past(self):
        self.assertEqual(self.p("20.12"), datetime(2025, 12, 20))
        self.assertEqual(self.p("18:00"), datetime(2026, 10, 15, 18, 0))

    def test_invalid(self):
        self.assertIsNone(self.p("вчера"))
        self.assertIsNone(self.p("31.02.2026"))
        self.assertIsNone(self.p("25:00"))


class TestCommandParsing(unittest.TestCase):
    def test_commands(self):
        P = bot6.parse_command
        self.assertEqual(P("/пересказ 100 от 12:00 16.10.2026"), ("пересказ", (100, "12:00 16.10.2026")))
        self.assertEqual(P("/пересказ с 16.10"), ("пересказ", (None, "16.10")))
        self.assertEqual(P("/пересказ"), ("пересказ", (None, None)))
        self.assertEqual(P("/мысль подробно"), ("мысль", True))
        self.assertEqual(P("/мысль"), ("мысль", False))
        self.assertEqual(P("/лимит токенов 30000"), ("лимит токенов", "30000"))
        self.assertEqual(P("/лимит токенов"), ("лимит токенов", None))
        self.assertEqual(P("/лимит символов сброс"), ("лимит символов", "сброс"))
        self.assertEqual(P("/сбросить лимит сообщений"), ("сбросить лимит", None))
        self.assertEqual(P("/пауза 3"), ("пауза", "3"))
        self.assertEqual(P("Грань, стоп машина"), ("стоп", None))
        self.assertEqual(P("/роль (поэт)"), ("роль", "поэт"))
        self.assertIsNone(P("Грань, как дела?"))
        self.assertIsNone(P("мысль вслух"))


# ---------- R11: /мысль ----------
class TestThought(Bot6Case):
    def setUp(self):
        super().setUp()
        bot6.FILOSOF = filosof.ThoughtGenerator(rng=random.Random(1))
        replies = iter(["Образ: я — маяк", "Что светит маяку?", "Маяку светит тьма."])
        self.llm.answer = lambda kw: next(replies)

    def test_thought(self):
        self.say("/мысль")
        self.assertEqual(len(self.llm.calls), 3)
        self.assertEqual(self.sent(), ["🧠 Философская мысль:\n\nМаяку светит тьма."])
        self.assertIn("Существительные:", self.llm.prompt(0))
        self.assertEqual(self.llm.calls[0]["messages"][0]["content"], filosof.SYSTEM_PROMPT)
        self.assertIn("Образ: я — маяк", self.llm.prompt(1))
        self.assertIn("Что светит маяку?", self.llm.prompt(2))

    def test_thought_detailed(self):
        self.say("/мысль подробно")
        text = self.sent()[0]
        for part in ("Полный процесс", "Существительные:", "Глаголы:", "Прилагательные:",
                     "Образ: я — маяк", "Что светит маяку?", "Маяку светит тьма."):
            self.assertIn(part, text)

    def test_thought_counts_as_one_answer(self):
        self.say("/мысль")
        self.assertEqual(bot6.daily_state(CHAT)["count"], 1)


class TestFilosofModule(unittest.TestCase):
    def test_word_lists_copied(self):
        g = filosof.ThoughtGenerator()
        self.assertGreater(len(g.words["nouns"]), 40000)
        self.assertGreater(len(g.words["verbs"]), 20000)
        self.assertGreater(len(g.words["adjectives"]), 20000)

    def test_random_words_format(self):
        words = filosof.ThoughtGenerator(rng=random.Random(5)).random_words()
        lines = words.splitlines()
        self.assertEqual([l.split(":")[0] for l in lines], ["Существительные", "Глаголы", "Прилагательные"])

    def test_empty_step_stops_chain(self):
        steps = filosof.ThoughtGenerator(rng=random.Random(2)).generate(lambda s, p: "")
        self.assertEqual((steps["image"], steps["question"], steps["answer"]), ("", "", ""))


# ---------- R13: сжатие контекста и посты ----------
class TestContextCompression(Bot6Case):
    def test_history_over_limit_is_compressed_and_cached(self):
        bot6.db_state_set(f"chars_{CHAT}", f"3000|{int(self.t)}")
        for i in range(30):
            bot6.db_add_message(CHAT, USER, False, f"довод {i} " + "x" * 150, int(self.t) - 900 + i * 10,
                                cmid=7000 + i)
        self.llm.answer = lambda kw: ("СЖАТО" if "Сожми" in kw["messages"][1]["content"]
                                      else "Ответ Грани")
        self.say("Грань, кто прав?")
        self.tick(9)
        self.assertEqual(len(self.llm.calls), 2)        # сжатие + ответ
        prompt = self.llm.prompt()
        self.assertIn("(Пересказ более ранней части беседы: СЖАТО)", prompt)
        self.assertIn("довод 29", prompt)               # свежие — дословно
        self.assertIn("довод 0", self.llm.prompt(0))    # старые — в сжатие
        history = prompt.split("ИСТОРИЯ:\n")[1].split("\n\nНОВОЕ")[0]
        self.assertLessEqual(len(history), 3000 + 100)
        # следующий ответ — без нового сжатия (пересказ из кэша)
        self.say("Грань, а ещё?")
        self.tick(9)
        self.assertEqual(len(self.llm.calls), 3)
        self.assertIn("СЖАТО", self.llm.prompt())
        self.assertEqual(self.llm.calls[-1]["max_tokens"], 800)

    def test_compression_failure_falls_back_to_trim(self):
        bot6.db_state_set(f"chars_{CHAT}", f"3000|{int(self.t)}")
        for i in range(30):
            bot6.db_add_message(CHAT, USER, False, f"довод {i} " + "x" * 150, int(self.t) - 900 + i * 10,
                                cmid=7000 + i)
        self.llm.answer = lambda kw: "" if "Сожми" in kw["messages"][1]["content"] else "Ответ"
        self.say("Грань, кто прав?")
        self.tick(9)
        self.assertIn("довод 29", self.llm.prompt())
        self.assertNotIn("довод 0 ", self.llm.prompt())

    def test_forget_drops_summary(self):
        bot6.db_state_set(f"ctxsum_{CHAT}", json.dumps({"first_id": 1, "last_id": 2, "text": "x"}))
        self.say("/забудь")
        self.assertIsNone(bot6.db_state_get(f"ctxsum_{CHAT}"))

    def test_post_kept_in_full(self):
        long_text = "пост " * 2000
        msg = self.say("смотрите", attachments=[{"type": "wall", "wall": {
            "id": 1, "owner_id": -77, "from_id": -77, "text": long_text}}])
        row = bot6.DB.execute("SELECT text FROM messages WHERE cmid=?", (msg["conversation_message_id"],)).fetchone()
        self.assertIn(long_text.strip(), row[0])
        self.assertFalse(hasattr(bot6, "POST_MAX_CHARS"))


# ---------- переход с bot5 ----------
class TestMigration(Bot6Case):
    def test_migrate_from_bot5(self):
        with tempfile.TemporaryDirectory() as d:
            old_path = os.path.join(d, "gran5.db")
            old = sqlite3.connect(old_path)
            old.executescript("""
                CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, peer_id INTEGER NOT NULL,
                    cmid INTEGER, vk_msg_id INTEGER, from_id INTEGER NOT NULL,
                    is_bot INTEGER NOT NULL DEFAULT 0, text TEXT, images TEXT, ts INTEGER NOT NULL);
                CREATE TABLE state (key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE roles (peer_id INTEGER PRIMARY KEY, role TEXT);
                CREATE TABLE users (user_id INTEGER PRIMARY KEY, first_name TEXT, last_name TEXT,
                    updated_at INTEGER);
            """)
            old.execute("INSERT INTO messages(peer_id,cmid,from_id,text,ts) VALUES(?,?,?,?,?)",
                        (CHAT, 1, USER, "старое", START - 100))
            old.execute("INSERT INTO roles VALUES(?,?)", (CHAT, "поэт"))
            old.execute("INSERT INTO users VALUES(?,?,?,?)", (USER, "Пётр", "", START))
            old.execute("INSERT INTO state VALUES(?,?)", (f"forget_{CHAT}", "5"))
            old.execute("INSERT INTO state VALUES(?,?)", (f"chars_{CHAT}", f"50000|{START}"))
            old.execute("INSERT INTO state VALUES(?,?)", (f"history_complete_{CHAT}", "1"))
            old.execute("INSERT INTO state VALUES(?,?)", (f"daily_{CHAT}", "{}"))   # не переносится
            old.commit()
            old.close()
            bot6.migrate_from_bot5(old_path)
            bot6.migrate_from_bot5(old_path)            # повторно — без дублей
            bot6.db_state_del("migrated_bot5")
            bot6.migrate_from_bot5(old_path)            # и даже если флаг потерян
        self.assertEqual(bot6.DB.execute("SELECT text FROM messages").fetchall(), [("старое",)])
        self.assertEqual(bot6.db_get_role(CHAT), "поэт")
        self.assertEqual(bot6.display_name(USER), "Пётр")
        self.assertEqual(bot6.db_state_get(f"forget_{CHAT}"), "5")
        self.assertEqual(bot6.chars_limit(CHAT), 50000)
        self.assertEqual(bot6.db_state_get(f"history_complete_{CHAT}"), "1")
        self.assertIsNone(bot6.db_state_get(f"daily_{CHAT}"))
        self.assertEqual(bot6.db_state_get("initial_load_done"), "1")
        self.assertEqual(bot6.db_state_get("migrated_bot5"), "1")

    def test_find_old_db(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "gran5.db")
            bot6.OLD_DB_PATH = path
            try:
                self.assertIsNone(bot6.find_old_db())
                sqlite3.connect(path).close()
                self.assertEqual(bot6.find_old_db(), path)
            finally:
                bot6.OLD_DB_PATH = None


# ---------- прежние возможности bot4 ----------
class TestBot4Features(Bot6Case):
    def test_reply_to_bot_is_addressed(self):
        self.say("Грань, привет")
        self.tick(9)
        bot_cmid = bot6.vk.next_cmid
        self.say("а почему?", reply_message={"from_id": BOT, "conversation_message_id": bot_cmid,
                                             "text": "Ответ Грани"})
        self.tick(9)
        self.assertEqual(len(self.llm.calls), 2)

    def test_retell_quoted(self):
        self.say("Грань, перескажи", reply_message={"from_id": ADMIN, "text": "длинный текст"})
        self.assertEqual(len(self.llm.calls), 1)
        self.assertIn("длинный текст", self.llm.prompt())

    def test_stop(self):
        self.say("Грань, стоп машина")
        self.assertTrue(bot6.STOP_REQUESTED)

    def test_role(self):
        self.say("/роль (строгий учитель)")
        self.assertEqual(bot6.db_get_role(CHAT), "строгий учитель")

    def test_stale_message_ignored(self):
        self.cmid += 1
        bot6.handle_message(chat_msg(self.cmid, "Грань, ау", ts=int(self.t) - 3600))
        self.tick(9)
        self.assertEqual(self.llm.calls, [])


if __name__ == "__main__":
    unittest.main()
