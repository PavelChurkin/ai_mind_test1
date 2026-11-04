"""
Новое в bot9 (задача #21): команды /раг2 рядом с неизменным /раг и название группы из .env.
ВК, БЯМ и эмбеддинги подменены, сеть не нужна.

Запуск: python3 -m unittest experiments/test_bot9_new.py -v
"""
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from types import SimpleNamespace

from test_bot9 import bot9 as bot, Bot9Case, FakeLLM, Obj, BOT9_DIR, CHAT, USER, ADMIN, START

FRAGMENT1 = "Сократ учил, что добродетель есть знание."
FRAGMENT2 = "Иван собрал чемодан и утром покинул город навсегда."
SUMMARY2 = "Пересказ: герой решил уехать из-за ссоры."
NOT_FOUND = "⚙️ " + bot.RAG_NOTHING_FOUND


class FakeRagLLM(FakeLLM):
    """Отличает запрос пересказа от ответа и умеет эмбеддинги: вектор — есть ли в тексте слово «уехать»."""

    def __init__(self):
        super().__init__(self.reply)
        self.embedded = []
        self.embeddings = Obj()
        self.embeddings.create = self.embed

    def reply(self, kw):
        if "Прочитай этот фрагмент книги" in kw["messages"][1]["content"]:
            return SUMMARY2
        return "Ответ по книге [1]"

    def embed(self, model, input):
        self.embedded.append((model, list(input)))
        data = [SimpleNamespace(index=i, embedding=[1.0 if "уехать" in t.lower() else 0.0, 0.01])
                for i, t in enumerate(input)]
        return SimpleNamespace(data=data)

    def summary_calls(self):
        return [c for c in self.calls if "Прочитай этот фрагмент книги" in c["messages"][1]["content"]]

    def answer_calls(self):
        return [c for c in self.calls if "Прочитай этот фрагмент книги" not in c["messages"][1]["content"]]


class Rag2BotCase(Bot9Case):
    def setUp(self):
        super().setUp()
        bot.llm_client = self.llm = FakeRagLLM()
        os.makedirs(bot.RAG_DIR)
        os.makedirs(bot.RAG2_DIR)
        self.book(bot.RAG_DIR, "сократ.txt", FRAGMENT1)
        self.book(bot.RAG2_DIR, "роман.txt", FRAGMENT2)

    @staticmethod
    def book(folder, name, text):
        with open(os.path.join(folder, name), "w", encoding="utf-8") as f:
            f.write(text)

    def build_index2(self):
        kb = bot.sync_kb2()
        while kb.process_pending(1)["pending"]:
            pass
        return kb

    def full_prompt(self, call):
        return "\n".join(m["content"] for m in call["messages"])

    def answer_prompts(self):
        return [self.full_prompt(c) for c in self.llm.answer_calls()]


class TestParseCommands(unittest.TestCase):
    def test_rag2_forms(self):
        cases = {
            "/раг2 вкл": ("раг2 режим", True), "/раг2 выкл": ("раг2 режим", False),
            "/rag2 on": ("раг2 режим", True), "/раг2 почему он уехал?": ("раг2", "почему он уехал?"),
            "/раг2": ("раг2", ""), "Грань, РАГ2 почему он уехал?": ("раг2", "почему он уехал?"),
            "Грань, раг2 почему?": ("раг2", "почему?"),
        }
        for text, expected in cases.items():
            self.assertEqual(bot.parse_command(text), expected, text)

    def test_rag_forms_are_unchanged(self):
        cases = {
            "/раг вкл": ("раг режим", True), "/раг выкл": ("раг режим", False),
            "/раг вопрос": ("раг", "вопрос"), "/раг": ("раг", ""),
            "Грань, РАГ вопрос": ("раг", "вопрос"), "/rag off": ("раг режим", False),
        }
        for text, expected in cases.items():
            self.assertEqual(bot.parse_command(text), expected, text)

    def test_random_words_are_not_commands(self):
        self.assertIsNone(bot.parse_command("раг2 тут случайно", addressed=False))
        self.assertIsNone(bot.parse_command("РАГ2 без обращения", addressed=False))
        self.assertIsNone(bot.parse_command("/раг22 вкл"))


class TestRag2Mode(Rag2BotCase):
    def test_rag2_mode_searches_index2_by_summary_and_gives_original_fragment(self):
        self.build_index2()
        self.say("/раг2 вкл")
        self.assertIn("Режим РАГ2 включён", self.sent()[-1])
        self.say("Грань, почему герой решил уехать?")
        self.tick(30)
        prompts = self.answer_prompts()
        self.assertEqual(len(prompts), 1)
        self.assertIn(FRAGMENT2, prompts[0], "в контексте — исходный фрагмент")
        self.assertNotIn(SUMMARY2, prompts[0], "пересказ наружу не выдаётся")
        self.assertNotIn(FRAGMENT1, prompts[0], "индекс /раг здесь не участвует")
        self.assertIn("Ответ по книге [1]", self.sent()[-1])
        self.assertIn("роман", self.sent()[-1])

    def test_rag_mode_still_searches_only_index1(self):
        self.build_index2()
        self.llm.embedded.clear()
        self.say("/раг вкл")
        self.assertEqual(self.sent()[-1], (
            f"⚙️ Режим базы знаний включён: каждый ответ сначала ищется в базе знаний (до {bot.RAG_TOP_K} "
            f"фрагментов). Нашлось — отвечаю только по фрагментам, не нашлось — пишу "
            f"«{bot.RAG_NO_RESULT_PREFIX}» и отвечаю как обычно. Выключить: «/раг выкл»."))
        self.say("Грань, чему учил Сократ?")
        self.tick(30)
        prompt = self.answer_prompts()[0]
        self.assertIn(FRAGMENT1, prompt)
        self.assertNotIn(FRAGMENT2, prompt)
        self.assertEqual(self.llm.embedded, [], "/раг не использует эмбеддинги индекса 2")

    def test_modes_replace_each_other(self):
        self.say("/раг вкл")
        self.say("/раг2 вкл")
        self.assertTrue(bot.rag2_mode(CHAT))
        self.assertFalse(bot.rag_mode(CHAT))
        self.say("/раг вкл")
        self.assertTrue(bot.rag_mode(CHAT))
        self.assertFalse(bot.rag2_mode(CHAT))

    def test_mode_off_and_other_chat(self):
        self.say("/раг2 вкл")
        self.assertFalse(bot.rag2_mode(CHAT + 1))
        self.say("/раг2 выкл")
        self.assertFalse(bot.rag2_mode(CHAT))
        self.assertIn("Режим РАГ2 выключен", self.sent()[-1])
        self.say("Грань, почему герой решил уехать?")
        self.tick(30)
        self.assertNotIn(FRAGMENT2, self.answer_prompts()[0])

    def test_rag_off_warns_when_rag2_is_still_on(self):
        self.say("/раг2 вкл")
        self.say("/раг выкл")
        self.assertTrue(bot.rag2_mode(CHAT))
        self.assertIn("продолжает действовать /раг2 вкл", self.sent()[-1])

    def test_nothing_found_falls_back_like_rag(self):
        self.build_index2()
        self.say("/раг2 вкл")
        self.say("Грань, как дела?")
        self.tick(30)
        self.assertTrue(self.sent()[-1].startswith(bot.RAG_NO_RESULT_PREFIX), self.sent()[-1])

    def test_model_says_no_answer_falls_back_like_rag(self):
        self.build_index2()
        self.say("/раг2 вкл")
        answers = iter([bot.rag.NO_ANSWER, "Обычный ответ"])
        self.llm.answer = lambda kw: next(answers)
        self.say("Грань, почему герой решил уехать?")
        self.tick(30)
        self.assertEqual(self.sent()[-1], f"{bot.RAG_NO_RESULT_PREFIX}\n\nОбычный ответ")

    def test_broken_index_still_answers(self):
        self.say("/раг2 вкл")
        with mock.patch.object(bot, "sync_kb2", side_effect=RuntimeError("индекс сломан")):
            self.say("Грань, как дела?")
            self.tick(30)
        self.assertTrue(self.sent()[-1].startswith(bot.RAG_NO_RESULT_PREFIX), self.sent()[-1])

    def test_unfinished_index_answers_by_exact_words(self):
        self.say("/раг2 вкл")
        self.say("Грань, куда Иван собрал чемодан?")
        self.tick(30)
        self.assertEqual(self.llm.summary_calls(), [], "пересказы строятся фоном, а не во время ответа")
        self.assertIn(FRAGMENT2, self.answer_prompts()[0])


class TestRag2OneOff(Rag2BotCase):
    def test_one_off_question_is_strict_and_uses_index2(self):
        self.build_index2()
        self.say("/раг2 почему герой решил уехать?")
        self.tick(5)
        call = self.llm.answer_calls()[-1]
        self.assertIn("ФРАГМЕНТЫ", call["messages"][1]["content"])
        self.assertIn(FRAGMENT2, call["messages"][1]["content"])
        self.assertNotIn(SUMMARY2, self.full_prompt(call))
        self.assertIn("Источники", self.sent()[-1])
        self.assertFalse(bot.rag2_mode(CHAT), "разовый вопрос не включает режим")

    def test_addressed_form(self):
        self.build_index2()
        self.say("Грань, РАГ2 почему герой решил уехать?")
        self.tick(5)
        self.assertIn(FRAGMENT2, self.answer_prompts()[-1])

    def test_nothing_found_is_strict(self):
        self.build_index2()
        before = len(self.llm.answer_calls())
        self.say("/раг2 квантовая хромодинамика")
        self.tick(5)
        self.assertEqual(self.sent()[-1], NOT_FOUND)
        self.assertEqual(len(self.llm.answer_calls()), before)

    def test_rag1_one_off_does_not_see_index2(self):
        self.say("/раг куда Иван собрал чемодан?")
        self.tick(5)
        self.assertEqual(self.sent()[-1], NOT_FOUND)

    def test_status_reports_progress_and_last_error(self):
        self.say("/раг2")
        text = self.sent()[-1]
        self.assertIn("папка RAG2", text)
        self.assertIn("с пересказом 0, в очереди 1", text)
        self.assertIn("роман.txt", text)
        self.assertIn("/раг2 вкл", text)
        self.llm.answer = lambda kw: (_ for _ in ()).throw(RuntimeError("API упал"))
        bot.get_kb2().process_pending(1)
        self.say("/раг2")
        self.assertIn("API упал", self.sent()[-1])
        self.llm.answer = self.llm.reply
        self.build_index2()
        self.say("/раг2")
        self.assertIn("с пересказом 1, в очереди 0", self.sent()[-1])
        self.assertNotIn("API упал", self.sent()[-1])

    def test_help_lists_rag2_and_keeps_rag(self):
        self.say("/помощь")
        text = "\n".join(self.sent())
        for needed in ("/раг2 вкл", "/раг2 вопрос", "/раг вкл", "/раг вопрос"):
            self.assertIn(needed, text)


class TestSummaryRequests(Rag2BotCase):
    def test_summary_request_goes_to_model_without_reasoning(self):
        self.build_index2()
        call = self.llm.summary_calls()[0]
        self.assertEqual(call["model"], bot.MODEL_NAME)
        self.assertEqual(call["max_tokens"], bot.RAG2_SUMMARY_MAX_TOKENS)
        self.assertEqual(call["extra_body"], bot.REASONING_OFF_BODY)
        self.assertIn(FRAGMENT2, call["messages"][1]["content"])
        self.assertEqual(self.llm.embedded, [(bot.RAG2_EMBEDDING_MODEL, [SUMMARY2])])

    def test_summary_does_not_touch_main_thread_state(self):
        self.llm.answer = lambda kw: ("", "length", "рассуждение")
        bot.summarize_fragment("система", "запрос")
        self.assertEqual(bot.LLM_REASONINGS, [])
        self.assertEqual(bot.LAST_LLM_ERROR, "")
        self.assertEqual(bot.db_state_get(f"daily_{CHAT}"), None)

    def test_empty_answer_at_token_limit_is_retried_once(self):
        answers = iter([("", "length"), ("Пересказ", "stop")])
        self.llm.answer = lambda kw: next(answers)
        self.assertEqual(bot.summarize_fragment("с", "з"), "Пересказ")
        self.assertEqual([c["max_tokens"] for c in self.llm.calls],
                         [bot.RAG2_SUMMARY_MAX_TOKENS, bot.EMPTY_ANSWER_RETRY_TOKENS])

    def test_rejected_reasoning_parameter_is_dropped(self):
        def create(**kw):
            self.llm.calls.append(kw)
            if kw.get("extra_body"):
                error = RuntimeError("bad param")
                error.status_code = 400
                raise error
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="Пересказ"),
                                                            finish_reason="stop")])
        self.llm.chat.completions.create = create
        self.assertEqual(bot.summarize_fragment("с", "з"), "Пересказ")
        self.assertFalse(bot.REASONING_PARAM_OK)
        self.assertNotIn("extra_body", self.llm.calls[-1])


class TestWorker(Rag2BotCase):
    def run_worker_until(self, done):
        """Настоящий поток со своим соединением; бот не ждёт его, тест ждёт результата."""
        import threading
        import time
        bot.RAG2_SUMMARY_INTERVAL_SEC = 0
        self.addCleanup(setattr, bot, "RAG2_SUMMARY_INTERVAL_SEC", 3)
        self.addCleanup(setattr, bot, "STOP_REQUESTED", False)
        thread = bot.start_rag2_worker()
        self.assertIsInstance(thread, threading.Thread)
        self.assertTrue(thread.daemon)
        started = time.time()
        while not done() and time.time() - started < 20:
            time.sleep(0.02)
        bot.STOP_REQUESTED = True
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())

    def test_worker_builds_index_and_stops(self):
        kb = bot.sync_kb2()
        self.run_worker_until(lambda: kb.stats()["summarized"] == 1)
        self.assertEqual(kb.stats()["pending"], 0)
        self.assertEqual(len(self.llm.summary_calls()), 1)

    def test_worker_survives_api_failures(self):
        kb = bot.sync_kb2()
        attempts = []

        def fail(kw):
            attempts.append(1)
            raise RuntimeError("API упал")
        self.llm.answer = fail
        self.run_worker_until(lambda: kb.stats()["last_error"])
        self.assertEqual(kb.stats()["summarized"], 0)
        self.assertIn("API упал", kb.stats()["last_error"])
        self.assertTrue(attempts)

    def test_build_can_be_disabled(self):
        bot.RAG2_BUILD = False
        self.addCleanup(setattr, bot, "RAG2_BUILD", True)
        self.assertIsNone(bot.start_rag2_worker())

    def test_backoff_grows_to_limit(self):
        delays = [bot.rag2_next_delay(1, n) for n in (1, 2, 3, 10)]
        self.assertEqual(delays, [30, 60, 120, bot.RAG2_MAX_BACKOFF_SEC])
        self.assertEqual(bot.rag2_next_delay(1, 0), bot.RAG2_SUMMARY_INTERVAL_SEC)
        self.assertEqual(bot.rag2_next_delay(0, 0), bot.RAG2_IDLE_POLL_SEC)


class TestReflectionMirror(Rag2BotCase):
    def reflection(self, folder):
        with open(os.path.join(folder, bot.REMEMBER_FILE), encoding="utf-8") as f:
            return f.read()

    def test_remember_reaches_both_indexes_and_reset_replaces_old_text(self):
        self.say("/запомни Аметист означает трезвость.", from_id=ADMIN)
        self.assertIn("Запомнила", self.sent()[-1])
        self.assertEqual(self.reflection(bot.RAG2_DIR), self.reflection(bot.RAG_DIR))
        self.assertEqual(bot.get_kb2().search("аметист")[0].path, bot.REMEMBER_FILE)
        self.say("/запомни с нуля Янтарь означает тепло.", from_id=ADMIN)
        self.assertEqual(self.reflection(bot.RAG2_DIR), self.reflection(bot.RAG_DIR))
        self.assertEqual(bot.get_kb2().search("аметист"), [])
        self.assertEqual(bot.get_kb2().search("янтарь")[0].path, bot.REMEMBER_FILE)
        self.assertEqual(bot.get_kb().search("аметист"), [])

    def test_mirror_failure_keeps_main_reflection_and_is_reported(self):
        shutil.rmtree(bot.RAG2_DIR)
        with open(bot.RAG2_DIR, "w", encoding="utf-8") as f:
            f.write("не папка")
        self.say("/запомни Аметист означает трезвость.", from_id=ADMIN)
        self.assertIn("Запомнила", self.sent()[-1])
        self.assertIn("/раг2 записать не удалось", self.sent()[-1])
        self.assertIn("Аметист", self.reflection(bot.RAG_DIR))

    def test_reflection_is_summarized_by_build(self):
        self.say("/запомни Аметист означает трезвость.", from_id=ADMIN)
        self.build_index2()
        self.assertTrue(any("Аметист" in c["messages"][1]["content"] for c in self.llm.summary_calls()))


class TestDefaultGroupName(Bot9Case):
    def test_default_value(self):
        self.assertEqual(bot.GROUP_NAME, "грань")
        self.assertEqual(bot.DEFAULT_GROUP_NAME, "грань")
        self.assertEqual(bot.BOT_NAME, "Грань")
        self.assertEqual(bot.STOP_PHRASE, "грань, стоп машина")

    def test_env_example_ships_the_value(self):
        with open(os.path.join(BOT9_DIR, ".env.example"), encoding="utf-8") as f:
            self.assertIn("GROUP_NAME=грань\n", f.read())


class TestNameIsNotHardcoded(unittest.TestCase):
    """Название группы в коде и настройках встречается только как значение по умолчанию."""
    ALLOWED = {
        "bot9.py": ['DEFAULT_GROUP_NAME = "грань"'],
        ".env.example": ["GROUP_NAME=грань"],
        "playbook.yml": ["bot9_group_name: грань"],
        "secrets.example.yml": ["# bot9_group_name: грань"],
    }

    def test_only_default_value_mentions_the_name(self):
        found = []
        for root, dirs, files in os.walk(BOT9_DIR):
            dirs[:] = [d for d in dirs if d not in ("__pycache__", "RAG", "RAG2", "logs")]
            for name in files:
                if name.endswith((".json", ".md", ".db", ".pyc", ".db-wal", ".db-shm", ".log")) or name == ".env":
                    continue
                with open(os.path.join(root, name), encoding="utf-8") as f:
                    for line in f:
                        if re.search(r"(?<!\w)[Гг]ран(ь|и|ью|ей)(?!\w)", line):
                            allowed = any(a in line for a in self.ALLOWED.get(name, []))
                            if not allowed:
                                found.append((name, line.strip()))
        self.assertEqual(found, [])


class TestCustomGroupName(Bot9Case):
    """Отдельный экземпляр модуля с другим GROUP_NAME: то, что вычисляется при запуске бота."""

    @classmethod
    def load(cls, name):
        saved = os.environ.get("GROUP_NAME")
        os.environ["GROUP_NAME"] = name
        try:
            spec = importlib.util.spec_from_file_location("bot9_custom", os.path.join(BOT9_DIR, "bot9.py"))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        finally:
            os.environ["GROUP_NAME"] = saved if saved is not None else ""
        return module

    @classmethod
    def setUpClass(cls):
        cls.custom = cls.load("  Сова  ")

    def test_normalized_value(self):
        self.assertEqual(self.custom.GROUP_NAME, "сова")
        self.assertEqual(self.custom.GROUP_TITLE, "Сова")
        self.assertEqual(self.custom.BOT_NAME, "Сова")

    def test_blank_value_means_default(self):
        self.assertEqual(self.load("   ").GROUP_NAME, "грань")

    def test_trigger(self):
        m = self.custom
        self.assertTrue(m.has_trigger("Сова, привет"))
        self.assertTrue(m.has_trigger("а что думает сова?"))
        self.assertFalse(m.has_trigger("совами интересуюсь"))
        self.assertFalse(m.has_trigger("Грань, привет"))

    def test_commands_use_the_name(self):
        m = self.custom
        self.assertEqual(m.parse_command("Сова, стоп машина"), ("стоп", None))
        self.assertNotEqual(m.parse_command("Грань, стоп машина"), ("стоп", None))
        self.assertEqual(m.parse_command("Сова, РАГ вопрос"), ("раг", "вопрос"))
        self.assertEqual(m.parse_command("Сова, раг2 вопрос"), ("раг2", "вопрос"))
        self.assertEqual(m.parse_command("Сова, запомни мысль")[0], "запомни")
        self.assertIsNone(m.parse_command("Грань, запомни мысль"))
        self.assertTrue(m.is_summary_request("Сова, перескажи"))
        self.assertTrue(m.is_summary_request("сова, о чём тут"))
        self.assertFalse(m.is_summary_request("Грань, перескажи"))

    def test_clean_answer_and_address(self):
        m = self.custom
        self.assertEqual(m.clean_answer("Сова: привет"), "привет")
        self.assertEqual(m.clean_answer("Грань: привет"), "Грань: привет")
        self.assertEqual(m.ADDRESS_PREFIX_RE.sub("", "Сова, вопрос", count=1), "вопрос")

    def test_help_and_role_use_the_name(self):
        m = self.custom
        self.assertTrue(m.DEFAULT_ROLE.startswith("Ты — Сова,"))
        usages = " ".join(row[1] + row[3] for row in m.HELP)
        self.assertIn("«Сова, РАГ2 вопрос»", usages)
        self.assertIn("Сова, стоп машина", usages)
        self.assertNotRegex(usages, r"\bГрань\b")
        with tempfile.TemporaryDirectory() as d:
            m.ROLE_FILE = os.path.join(d, "Роль.txt")
            with open(m.ROLE_FILE, "w", encoding="utf-8") as f:
                f.write("Ты — {GROUP_NAME}, собеседник.")
            self.assertEqual(m.default_role(), "Ты — Сова, собеседник.")

    def test_answer_prompt_uses_the_name(self):
        m = self.custom
        calls = []
        m.llm_call = lambda system, prompt, **kw: calls.append((system, prompt)) or "ок"
        m.DB = bot.db_init(":memory:")
        self.addCleanup(m.DB.close)
        self.assertEqual(m.ask_gran(CHAT, [m.Request(dict(peer_id=CHAT, from_id=USER, date=START, text="вопрос"))], []),
                         "ок")
        self.assertIn("Ответь как Сова.", calls[0][1])
        self.assertNotIn("Грань", "".join(calls[0]))

    def test_multiword_name(self):
        m = self.load("Новая Волна")
        self.assertEqual(m.GROUP_NAME, "новая волна")
        self.assertEqual(m.GROUP_TITLE, "Новая волна")
        self.assertTrue(m.has_trigger("Новая  волна, привет"))
        self.assertFalse(m.has_trigger("новая привет"))
        self.assertEqual(m.parse_command("Новая волна, стоп машина"), ("стоп", None))

    def test_name_with_regex_characters(self):
        m = self.load("c++")
        self.assertTrue(m.has_trigger("C++, привет"))
        self.assertFalse(m.has_trigger("c, привет"))


class TestEnvFile(unittest.TestCase):
    def test_value_is_read_from_env_file_next_to_the_bot(self):
        """Настоящий запуск: GROUP_NAME берётся из .env в папке бота."""
        with tempfile.TemporaryDirectory() as d:
            copy = os.path.join(d, "bot9")
            shutil.copytree(BOT9_DIR, copy, ignore=shutil.ignore_patterns(
                "__pycache__", "RAG", "RAG2", "logs", "*.db", "*.db-*", ".env"))
            with open(os.path.join(copy, ".env"), "w", encoding="utf-8") as f:
                f.write("GROUP_NAME=сова\n")
            env = {k: v for k, v in os.environ.items() if k != "GROUP_NAME"}
            code = ("import bot9; print(bot9.GROUP_NAME, bot9.BOT_NAME, bot9.STOP_PHRASE, "
                    "bot9.TRIGGER_RE.search('Сова, привет') is not None, sep='|')")
            out = subprocess.run([sys.executable, "-c", code], cwd=copy, env=env, capture_output=True,
                                 text=True, encoding="utf-8", timeout=60)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertEqual(out.stdout.strip(), "сова|Сова|сова, стоп машина|True")


if __name__ == "__main__":
    unittest.main()
