"""
Тесты новых возможностей bot8 (задача #17). Окружение (подменные ВК, LLM, часы) — из test_bot8.py.

Запуск: python3 -m unittest experiments/test_bot8_features.py -v
"""
import os
import ssl
import sys
import shutil
import signal
import logging
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_bot8 import bot8, Bot8Case, CHAT, USER, ADMIN, START  # noqa: E402

rag = bot8.rag
FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rag_fixtures")
SOKRAT = ("Глава 1. Сократ\n\nСократ учил, что добродетель есть знание: кто знает, что такое "
          "благо, тот не может поступать дурно. Он говорил: «Я знаю, что ничего не знаю».\n\n"
          "Глава 2. Платон\n\nПлатон, ученик Сократа, создал учение об идеях.\n")


def system_texts(case):
    return [t for t in case.sent() if t.startswith(bot8.SYSTEM_MARK)]


def answer_texts(case):
    return [t for t in case.sent() if not t.startswith(bot8.SYSTEM_MARK)]


# ---------- пустой ответ модели: почему «Не получилось сформулировать ответ» ----------
class TestEmptyAnswer(Bot8Case):
    def test_reasoning_ate_limit_retry_with_bigger_limit(self):
        answers = iter([("", "length"), ("Наконец ответ", "stop")])
        self.llm.answer = lambda kw: next(answers)
        self.say("Грань, привет")
        self.tick(9)
        self.assertEqual([c["max_tokens"] for c in self.llm.calls],
                         [bot8.MAX_TOKENS_DEFAULT, bot8.EMPTY_ANSWER_RETRY_TOKENS])
        self.assertEqual(self.sent(), ["Наконец ответ"])
        self.assertEqual(bot8.daily_state(CHAT)["count"], 1)

    def test_still_empty_reason_sent_as_system_message_not_counted(self):
        self.llm.answer = ("", "length")
        self.say("Грань, привет")
        self.tick(9)
        self.assertEqual(len(self.llm.calls), 2)
        [text] = self.sent()
        self.assertTrue(text.startswith(bot8.SYSTEM_MARK))
        self.assertIn("Не получилось сформулировать ответ.", text)
        self.assertIn("Причина: модель израсходовала весь лимит на рассуждение даже при 3000", text)
        self.assertIn("/лимит токенов", text)                 # подсказка, что делать
        self.assertEqual(bot8.daily_state(CHAT)["count"], 0)   # в дневной лимит не идёт
        # и в контекст следующего ответа служебное сообщение не попадает
        self.llm.answer = "Ответ"
        self.say("Грань, ещё раз")
        self.tick(9)
        self.assertNotIn("Причина", self.llm.prompt())

    def test_no_retry_when_limit_already_big(self):
        bot8.db_state_set(f"tokens_{CHAT}", f"5000|{int(self.t)}")
        self.llm.answer = ("", "length")
        self.say("Грань, привет")
        self.tick(9)
        self.assertEqual([c["max_tokens"] for c in self.llm.calls], [5000])
        self.assertIn("весь лимит (5000 токенов) на рассуждение", self.sent()[0])

    def test_empty_with_stop_and_api_error(self):
        self.llm.answer = ("", "stop")
        self.say("Грань, привет")
        self.tick(9)
        self.assertEqual(len(self.llm.calls), 1)
        self.assertIn("модель вернула пустой ответ (finish_reason=stop)", self.sent()[-1])

        def boom(kw):
            raise RuntimeError("Error code: 402 - insufficient balance")
        self.llm.answer = boom
        self.say("Грань, а сейчас?")
        self.tick(9)
        self.assertIn("Причина: ошибка API: Error code: 402", self.sent()[-1])

    def test_thought_reason(self):
        self.llm.answer = ("", "length")
        self.say("/мысль")
        self.assertEqual(len(self.llm.calls), 1)      # bot8: без повтора и без роста лимита
        self.assertEqual(self.llm.calls[0]["max_tokens"], bot8.tokens_limit(CHAT))
        self.assertIn("Мысль не родилась", self.sent()[-1])
        self.assertIn("рассуждение", self.sent()[-1])

    def test_extra_body_passed(self):
        bot8.LLM_EXTRA_BODY = {"thinking": {"type": "disabled"}}
        try:
            self.say("Грань, привет")
            self.tick(9)
        finally:
            bot8.LLM_EXTRA_BODY = None
        self.assertEqual(self.llm.calls[0]["extra_body"], {"thinking": {"type": "disabled"}})


# ---------- картинки: SSL, без ссылки-запасного варианта, кэш ----------
class FakeResponse:
    def __init__(self, data=b"\x89PNG", mime="image/png"):
        self.data = data
        self.headers = mock.Mock()
        self.headers.get_content_type.return_value = mime

    def read(self, n=-1):
        return self.data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def cert_error():
    return urllib.error.URLError(ssl.SSLCertVerificationError(
        1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local issuer certificate"))


class TestImages(Bot8Case):
    URL = "https://sun9-1.userapi.com/photo.jpg"

    def setUp(self):
        super().setUp()
        bot8.IMAGE_MODE = "base64"
        self.system_ctx, self.certifi_ctx = object(), object()
        bot8._ssl_contexts = [("системные сертификаты", self.system_ctx), ("certifi", self.certifi_ctx)]
        self.opened = []

    def urlopen(self, behaviour):
        def fake(req, timeout=None, context=None):
            self.opened.append(context)
            result = behaviour(context)
            if isinstance(result, Exception):
                raise result
            return result
        return mock.patch.object(bot8.urllib.request, "urlopen", fake)

    def test_certificate_error_falls_back_to_certifi(self):
        with self.urlopen(lambda ctx: cert_error() if ctx is self.system_ctx else FakeResponse()):
            data, mime = bot8.download_image(self.URL)
        self.assertEqual((data, mime), (b"\x89PNG", "image/png"))
        self.assertEqual(self.opened, [self.system_ctx, self.certifi_ctx])

    def test_other_errors_not_retried_with_next_context(self):
        with self.urlopen(lambda ctx: urllib.error.HTTPError(self.URL, 429, "Too Many Requests", {}, None)):
            with self.assertRaises(urllib.error.HTTPError):
                bot8.download_image(self.URL)
        self.assertEqual(self.opened, [self.system_ctx])

    def test_failed_image_skipped_not_sent_as_url(self):
        """Ссылка вместо картинки заставляла прокси качать её с ВК: ВК отвечал 429 → 400."""
        with self.urlopen(lambda ctx: cert_error()):
            self.say("Грань, что на фото?", attachments=[{"type": "photo", "photo": {
                "sizes": [{"type": "x", "width": 604, "height": 400, "url": self.URL}]}}])
            self.tick(9)
        self.assertEqual(len(self.llm.calls), 1)
        content = self.llm.calls[0]["messages"][1]["content"]
        self.assertIsInstance(content, str)                  # картинок в запросе нет
        self.assertNotIn("image_url", str(content))
        self.assertIn("(1 картинок не удалось скачать.)", content)
        self.assertEqual(self.sent(), ["Ответ Грани"])

    def test_url_fallback_setting_restores_old_behaviour(self):
        bot8.IMAGE_URL_FALLBACK = True
        try:
            with self.urlopen(lambda ctx: cert_error()):
                part = bot8._image_part(self.URL)
        finally:
            bot8.IMAGE_URL_FALLBACK = False
        self.assertEqual(part, {"type": "image_url", "image_url": {"url": self.URL}})

    def test_downloaded_image_sent_as_base64_and_cached(self):
        with self.urlopen(lambda ctx: FakeResponse()):
            bot8.llm_call("s", "u", images=[self.URL])
            bot8.llm_call("s", "u", images=[self.URL])
        self.assertEqual(len(self.opened), 1)                # второй раз — из кэша
        content = self.llm.calls[-1]["messages"][1]["content"]
        self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/png;base64,"))

    def test_api_error_with_images_retried_without_them_once(self):
        calls = []

        def answer(kw):
            calls.append(kw["messages"][1]["content"])
            if not isinstance(kw["messages"][1]["content"], str):
                raise RuntimeError("Error code: 400")
            return "текстом"
        self.llm.answer = answer
        with self.urlopen(lambda ctx: FakeResponse()):
            self.assertEqual(bot8.llm_call("s", "u", images=[self.URL, "https://x/broken.jpg"]), "текстом")
        self.assertEqual(len(calls), 2)
        self.assertIn("Картинки не удалось передать", calls[1])

    def test_real_contexts_include_certifi_with_harica_root(self):
        """*.userapi.com подписан HARICA: в certifi этот корень есть (в хранилище Windows — не всегда)."""
        import certifi
        bot8._ssl_contexts = None
        names = [name for name, _ctx in bot8.ssl_contexts()]
        self.assertEqual(names[:2], ["системные сертификаты", "certifi"])
        with open(certifi.where(), encoding="ascii") as f:
            self.assertIn("HARICA TLS RSA Root CA 2021", f.read())
        bot8.IMAGE_SSL_INSECURE_FALLBACK = True
        bot8._ssl_contexts = None
        try:
            self.assertEqual(bot8.ssl_contexts()[-1][0], "без проверки сертификата")
        finally:
            bot8.IMAGE_SSL_INSECURE_FALLBACK = False
            bot8._ssl_contexts = None

    def test_is_cert_error(self):
        self.assertTrue(bot8.is_cert_error(cert_error()))
        self.assertFalse(bot8.is_cert_error(urllib.error.URLError("timed out")))


# ---------- подробный лог ----------
class TestDebugLogging(Bot8Case):
    def setUp(self):
        super().setUp()
        root = logging.getLogger()
        self.saved = (root.handlers[:], root.level)
        self.log_file = os.path.join(self.tmp.name, "logs", "bot8.log")

    def tearDown(self):
        root = logging.getLogger()
        for h in root.handlers[:]:
            root.removeHandler(h)
            h.close()
        handlers, level = self.saved
        for h in handlers:
            root.addHandler(h)
        root.setLevel(level)
        bot8.DEBUG_MODE = False
        super().tearDown()

    def run_dialog(self):
        self.say("Грань, привет")
        self.tick(9)
        for h in logging.getLogger().handlers:
            h.flush()
        with open(self.log_file, encoding="utf-8") as f:
            return f.read()

    def test_normal_mode_is_quiet(self):
        bot8.setup_logging(False, self.log_file, console=False)
        text = self.run_dialog()
        self.assertNotIn(">> handle_message", text)
        self.assertNotIn("LLM system:", text)
        self.assertIn("LLM: ", text)                       # строка о расходе токенов — всегда
        self.assertEqual(logging.getLogger("httpx").level, logging.WARNING)

    def test_debug_mode_traces_functions_and_prompts(self):
        bot8.setup_logging(True, self.log_file, console=False)
        text = self.run_dialog()
        for fn in ("handle_message", "flush", "answer_batch", "ask_gran", "llm_call"):
            self.assertIn(f">> {fn}(", text)
            self.assertIn(f"<< {fn} = ", text)
        self.assertIn("LLM system:", text)
        self.assertIn("Грань, привет", text)
        self.assertEqual(logging.getLogger("httpx").level, logging.INFO)

    def test_env_and_args(self):
        args = bot8.parse_args(["--debug", "--log-file", "x.log", "--restart-after-hours", "168"])
        self.assertEqual((args.debug, args.log_file, args.restart_after_hours), (True, "x.log", 168))
        with mock.patch.dict(os.environ, {"BOT_DEBUG": "1"}):
            self.assertTrue(bot8.parse_args([]).debug)
        with mock.patch.dict(os.environ, {"BOT_DEBUG": ""}):
            self.assertFalse(bot8.parse_args([]).debug)


# ---------- сообщения, которые ВК разрезал на части ----------
class TestSplitMessages(Bot8Case):
    LONG = 3500

    def test_long_addressed_message_joined(self):
        self.say("Грань, " + "а" * self.LONG)
        self.t += 2
        self.say("б" * 500)                       # продолжение без «Грань»
        self.tick(9)
        self.assertEqual(len(self.llm.calls), 1)
        prompt = self.llm.prompt()
        self.assertIn("а" * self.LONG + "\n" + "б" * 500, prompt)
        self.assertEqual(self.sent(), ["Ответ Грани"])

    def test_waits_longer_after_long_part(self):
        self.say("Грань, " + "а" * self.LONG)
        self.tick(bot8.BATCH_WAIT_SEC + 0.5)       # 8 с мало: часть длинная, ждём SPLIT_JOIN_SEC
        self.assertEqual(self.llm.calls, [])
        self.tick(bot8.SPLIT_JOIN_SEC)
        self.assertEqual(len(self.llm.calls), 1)

    def test_address_in_last_part(self):
        self.say("а" * self.LONG)
        self.t += 1
        self.say("в" * self.LONG)
        self.t += 1
        self.say("конец. Грань, что скажешь?")
        self.tick(9)
        self.assertEqual(len(self.llm.calls), 1)
        prompt = self.llm.prompt()
        self.assertIn("НОВОЕ СООБЩЕНИЕ", prompt)
        self.assertIn("а" * self.LONG + "\n" + "в" * self.LONG + "\nконец. Грань, что скажешь?", prompt)
        self.assertNotIn("ИСТОРИЯ", prompt)        # части не дублируются в истории

    def test_short_or_old_messages_not_prepended(self):
        self.say("короткое сообщение")
        self.t += 1
        self.say("Грань, привет")
        self.tick(9)
        self.assertIn("ИСТОРИЯ", self.llm.prompt())
        self.say("а" * self.LONG)
        self.t += bot8.SPLIT_JOIN_SEC + 5           # слишком поздно для продолжения
        self.say("Грань, а теперь?")
        self.tick(9)
        new_part = self.llm.prompt().split("НОВОЕ СООБЩЕНИЕ")[1]
        self.assertNotIn("а" * 100, new_part)

    def test_other_author_not_joined(self):
        self.say("Грань, " + "а" * self.LONG)
        self.t += 2
        self.say("б" * 500, from_id=ADMIN)
        self.tick(11)
        self.assertNotIn("а" * self.LONG + "\n" + "б" * 500, self.llm.prompt())

    def test_role_with_unclosed_paren_joined(self):
        self.say("/роль (" + "р" * self.LONG)
        self.assertEqual(self.sent(), [])          # ждём продолжения
        self.t += 2
        self.say("с" * self.LONG)
        self.t += 2
        self.say("конец роли)")
        self.assertEqual(bot8.db_get_role(CHAT), "р" * self.LONG + "\n" + "с" * self.LONG + "\nконец роли")
        self.assertIn("Роль собрана из 3 сообщений", self.sent()[-1])
        self.tick(30)
        self.assertEqual(self.llm.calls, [])        # роль — не обращение

    def test_role_paren_never_closed_timeout(self):
        self.say("/роль (" + "р" * self.LONG)
        self.tick(bot8.ROLE_JOIN_SEC - 1)
        self.assertEqual(self.sent(), [])
        self.tick(2)
        self.assertEqual(bot8.db_get_role(CHAT), "р" * self.LONG)
        self.assertIn("Закрывающая скобка так и не пришла", self.sent()[-1])

    def test_role_bare_form_joined(self):
        self.say("Грань, /роль " + "р" * self.LONG)
        self.t += 1
        self.say("хвост")
        self.assertEqual(bot8.db_get_role(CHAT), "р" * self.LONG + "\nхвост")

    def test_short_role_not_buffered(self):
        self.say("/роль (поэт)")
        self.assertEqual(bot8.db_get_role(CHAT), "поэт")
        self.say("/роль философ")
        self.assertEqual(bot8.db_get_role(CHAT), "философ")

    def test_command_interrupts_buffer(self):
        self.say("/роль (" + "р" * self.LONG)
        self.t += 1
        self.say("/помощь")
        self.assertEqual(bot8.db_get_role(CHAT), "р" * self.LONG)
        self.assertIn("Команды бота", self.sent()[-1])

    def test_remember_text_joined(self):
        self.say("Грань, запомни " + "з" * self.LONG, from_id=ADMIN)
        self.t += 1
        self.say("конец", from_id=ADMIN)
        with open(os.path.join(bot8.RAG_DIR, bot8.REMEMBER_FILE), encoding="utf-8") as f:
            self.assertEqual(f.read(), "з" * self.LONG + "\nконец\n")


# ---------- база знаний: РАГ и РАГ+ ----------
class TestRag(Bot8Case):
    def setUp(self):
        super().setUp()
        os.makedirs(bot8.RAG_DIR)
        with open(os.path.join(bot8.RAG_DIR, "sokrat.txt"), "w", encoding="utf-8") as f:
            f.write(SOKRAT)

        def answer(kw):
            if kw["messages"][0]["content"] == rag.SYSTEM_PROMPT:
                return "Сократ считал, что добродетель есть знание [1]."
            return "Свободный ответ модели"
        self.llm.answer = answer

    def test_rag_answer_with_sources(self):
        self.say("Грань, РАГ что такое добродетель?")
        self.assertEqual(len(self.llm.calls), 1)
        call = self.llm.calls[0]
        self.assertEqual(call["messages"][0]["content"], rag.SYSTEM_PROMPT)
        self.assertIn("добродетель есть знание", self.llm.prompt())
        self.assertIn("ВОПРОС: что такое добродетель?", self.llm.prompt())
        self.assertGreaterEqual(call["max_tokens"], bot8.RAG_MAX_TOKENS)
        [text] = self.sent()
        self.assertIn("добродетель есть знание [1]", text)
        self.assertIn("Источники:\n[1] «sokrat»", text)
        self.assertNotIn("---", text)
        self.assertEqual(bot8.daily_state(CHAT)["count"], 1)

    def test_nothing_found_no_llm_no_invention(self):
        self.say("Грань, РАГ квантовая хромодинамика")
        self.assertEqual(self.llm.calls, [])
        self.assertEqual(self.sent(), [f"{bot8.SYSTEM_MARK} {bot8.RAG_NOTHING_FOUND}"])
        self.assertEqual(bot8.daily_state(CHAT)["count"], 0)

    def test_model_says_no_answer(self):
        self.llm.answer = rag.NO_ANSWER
        self.say("Грань, РАГ кто такой Сократ")
        self.assertEqual(len(self.llm.calls), 1)
        self.assertEqual(self.sent(), [f"{bot8.SYSTEM_MARK} {bot8.RAG_NOTHING_FOUND}"])

    def test_rag_plus_removed(self):
        """bot8: «РАГ+» убран — «+» перед вопросом просто отбрасывается, ответ только по книгам."""
        self.say("Грань, РАГ+ что такое добродетель?")
        self.assertEqual(len(self.llm.calls), 1)
        self.assertIn("ВОПРОС: что такое добродетель?", self.llm.prompt())
        self.assertNotIn("Свободный ответ модели", self.sent()[0])
        self.assertFalse(hasattr(bot8, "RAG_SEPARATOR"))

    def test_status_without_query(self):
        self.say("/раг")
        self.assertEqual(self.llm.calls, [])
        self.assertIn("книг 1", self.sent()[0])
        self.assertIn("• sokrat.txt — «sokrat», фрагментов 2", self.sent()[0])
        with open(os.path.join(bot8.RAG_DIR, "broken.pdf"), "wb") as f:
            f.write(b"not a pdf")
        self.say("/раг")
        self.assertIn("не прочитано файлов: 1", self.sent()[-1])
        self.assertIn("• broken.pdf — «broken», не прочитан:", self.sent()[-1])

    def test_lowercase_word_is_not_command(self):
        self.say("Грань, что такое раг?")
        self.tick(9)
        self.assertNotEqual(self.llm.calls[0]["messages"][0]["content"], rag.SYSTEM_PROMPT)
        self.assertEqual(self.sent(), ["Свободный ответ модели"])
        self.say("у нас тут РАГ без обращения")
        self.tick(9)
        self.assertEqual(len(self.llm.calls), 1)

    def test_new_book_indexed_on_next_question(self):
        self.say("Грань, РАГ учение об идеях")
        shutil.copy(os.path.join(FIXTURES, "sokrat.docx"), os.path.join(bot8.RAG_DIR, "platon.docx"))
        self.say("/раг")
        self.assertIn("книг 2", self.sent()[-1])

    def test_rag_waits_for_pause_and_respects_daily_limit(self):
        self.say("/пауза 1")
        self.say("Грань, РАГ что такое добродетель?")
        self.assertEqual(self.llm.calls, [])
        self.tick(61)
        self.assertEqual(len(self.llm.calls), 1)
        bot8.db_state_set(bot8.daily_key(CHAT), '{"day": "%s", "count": %d, "notified": [25, 50, 90, 100]}'
                          % (bot8.day_key(), bot8.DAILY_MESSAGE_LIMIT))
        self.say("Грань, РАГ что такое добродетель?")
        self.assertEqual(self.sent()[-1], f"{bot8.SYSTEM_MARK} {bot8.LIMIT_REACHED_TEXT}")


class TestRagParsing(unittest.TestCase):
    def test_forms(self):
        p = bot8.parse_command
        self.assertEqual(p("Грань, РАГ что такое добродетель?"), ("раг", "что такое добродетель?"))
        self.assertEqual(p("Грань, раг: кто такой Сократ"), ("раг", "кто такой Сократ"))
        self.assertEqual(p("Грань, РАГ+ кто такой Сократ"), ("раг", "кто такой Сократ"))
        self.assertEqual(p("/RAG вопрос", addressed=False), ("раг", "вопрос"))
        self.assertEqual(p("/раг"), ("раг", ""))
        self.assertEqual(p("/раг вкл"), ("раг режим", True))
        self.assertEqual(p("/раг выкл"), ("раг режим", False))
        self.assertEqual(p("Грань, РАГ включить"), ("раг режим", True))
        self.assertEqual(p("/rag off"), ("раг режим", False))
        self.assertEqual(p("/раг включить свет"), ("раг", "включить свет"))
        self.assertIsNone(p("Грань, что такое раг?"))
        self.assertIsNone(p("РАГ вопрос", addressed=False))
        self.assertIsNone(p("Грань, тараг и драг"))


# ---------- «Грань, запомни» → Рефлексия.txt ----------
class TestRemember(Bot8Case):
    def fwd(self, text, from_id=USER):
        return {"from_id": from_id, "text": text, "date": int(self.t) - 60}

    def path(self):
        return os.path.join(bot8.RAG_DIR, bot8.REMEMBER_FILE)

    def test_forwarded_messages_overwrite_file_and_reindex(self):
        self.say("Грань, запомни", from_id=ADMIN, fwd_messages=[self.fwd("Истина рождается в споре.")])
        with open(self.path(), encoding="utf-8") as f:
            self.assertEqual(f.read(), "Истина рождается в споре.\n")
        self.assertIn("Запомнила", self.sent()[-1])
        self.say("Грань, запомни", from_id=ADMIN,
                 fwd_messages=[self.fwd("Сомнение — начало мудрости."), self.fwd("Согласен.", from_id=ADMIN)])
        with open(self.path(), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("Сомнение — начало мудрости.", text)
        self.assertIn("Согласен.", text)
        self.assertNotIn("Истина", text)                       # перезаписан, а не дописан
        with open(self.path() + ".bak", encoding="utf-8") as f:
            self.assertEqual(f.read(), "Истина рождается в споре.\n")
        # переиндексирован: по базе знаний находится новое, старое — нет
        self.assertTrue(bot8.get_kb().search("сомнение мудрость"))
        self.assertFalse(bot8.get_kb().search("истина рождается в споре"))
        self.assertEqual(self.llm.calls, [])

    def test_reply_and_text_after_command(self):
        self.say("Грань, запомни", from_id=ADMIN, reply_message=self.fwd("Из ответа."))
        with open(self.path(), encoding="utf-8") as f:
            self.assertEqual(f.read(), "Из ответа.\n")
        self.say("/запомни Текст прямо в команде", from_id=ADMIN)
        with open(self.path(), encoding="utf-8") as f:
            self.assertEqual(f.read(), "Текст прямо в команде\n")

    def test_admin_only_and_empty(self):
        self.say("Грань, запомни", fwd_messages=[self.fwd("не админ")])
        self.assertFalse(os.path.exists(self.path()))
        self.assertIn("только админу", self.sent()[-1])
        self.say("Грань, запомни", from_id=ADMIN)
        self.assertIn("Что запомнить?", self.sent()[-1])
        self.assertFalse(os.path.exists(self.path()))

    def test_remembered_text_answers_rag(self):
        self.say("Грань, запомни", from_id=ADMIN, fwd_messages=[self.fwd("Пароль беседы — синий кит.")])
        self.llm.answer = "Пароль — синий кит [1]."
        self.say("Грань, РАГ какой пароль беседы?")
        self.assertIn("[1] «Рефлексия»", self.sent()[-1])


# ---------- Роль.txt и /сбросить роль ----------
class TestDefaultRole(Bot8Case):
    def write_role(self, text, encoding="utf-8", mtime=None):
        with open(bot8.ROLE_FILE, "w", encoding=encoding) as f:
            f.write(text)
        if mtime:
            os.utime(bot8.ROLE_FILE, (mtime, mtime))

    def test_without_file_builtin_role(self):
        self.assertEqual(bot8.db_get_role(CHAT), bot8.DEFAULT_ROLE)

    def test_role_file_is_default_and_reread_when_changed(self):
        self.write_role("Ты — пират.\n", mtime=START - 100)
        self.say("Грань, привет")
        self.tick(9)
        self.assertIn("Ты — пират.", self.llm.calls[0]["messages"][0]["content"])
        self.write_role("Ты — поэт.", encoding="cp1251", mtime=START - 50)   # Блокнот, ANSI
        self.assertEqual(bot8.db_get_role(CHAT), "Ты — поэт.")
        self.write_role("   \n", mtime=START - 10)                           # пустой файл
        self.assertEqual(bot8.db_get_role(CHAT), bot8.DEFAULT_ROLE)

    def test_set_show_reset(self):
        self.write_role("Ты — пират.")
        self.say("/роль (поэт)")
        self.assertEqual(bot8.db_get_role(CHAT), "поэт")
        self.say("/роль")
        self.assertIn("Роль (задана в этой беседе):\nпоэт", self.sent()[-1])
        self.say("/сбросить роль")
        self.assertIn("Роль сброшена", self.sent()[-1])
        self.assertEqual(bot8.db_get_role(CHAT), "Ты — пират.")
        self.say("/роль")
        self.assertIn("по умолчанию, Роль.txt", self.sent()[-1].replace(bot8.ROLE_FILE, "Роль.txt"))
        self.assertIn("Ты — пират.", self.sent()[-1])

    def test_role_file_shipped_with_bot(self):
        path = os.path.join(bot8.BOT_DIR, "Роль.txt")
        self.assertEqual(bot8.read_text_file(path).strip(), bot8.DEFAULT_ROLE)


# ---------- несколько собеседников: ждём 8 секунд ----------
class TestSeveralSpeakers(Bot8Case):
    def test_two_speakers_within_wait_get_one_answer(self):
        self.assertEqual(bot8.BATCH_WAIT_SEC, 8)
        self.say("Грань, привет")
        self.tick(7)
        self.say("Грань, и мне ответь", from_id=ADMIN)
        self.tick(7)
        self.assertEqual(self.llm.calls, [])        # с последнего обращения ещё нет 8 с
        self.tick(1.5)
        self.assertEqual(len(self.llm.calls), 1)
        self.assertIn("НОВЫЕ СООБЩЕНИЯ (их 2", self.llm.prompt())


# ---------- лимит токенов ----------
class TestTokenLimit(Bot8Case):
    def test_number_formats(self):
        for text, value in (("/лимит токенов 3000", 3000), ("/лимит токенов 30 000", 30000),
                            ("/лимит токенов 30 000", 30000), ("/лимит токенов: 3000", 3000),
                            ("/лимит токенов=4000", 4000), ("/лимит токенов 30к", 30000),
                            ("/лимит токенов 2k", 2000), ("/лимит токенов 3 тыс.", 3000)):
            bot8.DB.execute("DELETE FROM state")
            self.say(text, from_id=ADMIN)
            self.assertEqual(bot8.tokens_limit(CHAT), value, text)

    def test_parse_command_args(self):
        p = bot8.parse_command
        self.assertEqual(p("/лимит токенов 30 000 пожалуйста"), ("лимит токенов", "30 000"))
        self.assertEqual(p("/лимит токенов сброс"), ("лимит токенов", "сброс"))
        self.assertEqual(p("/лимит токенов"), ("лимит токенов", None))
        self.assertEqual(bot8.parse_limit_value("сброс"), None)

    def test_resets_after_exactly_60_minutes_with_notice(self):
        self.say("/лимит токенов 3000", from_id=ADMIN)
        self.assertIn("вернётся к стандарту 800", self.sent()[-1])
        self.tick(59 * 60)
        self.assertEqual(bot8.tokens_limit(CHAT), 3000)
        sent = len(self.sent())
        self.tick(60)                                     # 60 мин — process_pending сам сбросил
        self.assertEqual(len(self.sent()), sent + 1)
        self.assertIn("лимит токенов вернулся к стандарту 800", self.sent()[-1])
        self.assertEqual(bot8.tokens_limit(CHAT), bot8.MAX_TOKENS_DEFAULT)
        self.tick(120)
        self.assertEqual(len(self.sent()), sent + 1)      # сообщение — один раз

    def test_help_and_show_reset_time(self):
        self.say("/лимит токенов 3000", from_id=ADMIN)
        self.say("/помощь")
        self.assertIn("лимит токенов ответа: 3000 (до 16:00, потом 800)", self.sent()[-1])
        self.say("/лимит токенов", from_id=ADMIN)
        self.assertIn("Лимит выходных токенов: 3000 (стандарт 800). До 16:00", self.sent()[-1])

    def test_limit_used_in_requests(self):
        self.say("/лимит токенов 30 000", from_id=ADMIN)
        self.say("Грань, привет")
        self.tick(9)
        self.assertEqual(self.llm.calls[0]["max_tokens"], 30000)


# ---------- плановый перезапуск ----------
class FakeLongPoll:
    def __init__(self):
        self.checks = 0

    def check(self):
        self.checks += 1
        return []


class TestRestart(Bot8Case):
    def test_is_idle(self):
        self.assertTrue(bot8.is_idle())
        self.say("Грань, привет")
        self.assertFalse(bot8.is_idle())
        self.tick(9)
        self.assertTrue(bot8.is_idle())
        self.say("/роль (" + "р" * 3500)
        self.assertFalse(bot8.is_idle())                  # недописанная роль
        self.tick(20)
        self.assertTrue(bot8.is_idle())
        self.say("/пауза 5")
        self.assertFalse(bot8.is_idle())

    def test_main_exits_with_restart_code(self):
        lp = FakeLongPoll()
        root = logging.getLogger()
        saved = (root.handlers[:], root.level)
        handlers = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
        try:
            with mock.patch.object(bot8, "init", lambda: lp), \
                    mock.patch.object(bot8, "load_history", lambda: None), \
                    mock.patch.object(bot8, "setup_logging", lambda *a, **k: None):
                with self.assertRaises(SystemExit) as cm:
                    bot8.main(["--restart-after-hours", "0.000001"])
        finally:
            for s, h in handlers.items():
                signal.signal(s, h)
            root.handlers[:] = saved[0]
            root.setLevel(saved[1])
            bot8.EXIT_CODE = 0
            bot8.STOP_REQUESTED = False
            bot8.DB = bot8.db_init(":memory:")            # main закрыл БД; tearDown закроет эту
        self.assertEqual(cm.exception.code, bot8.RESTART_EXIT_CODE)
        self.assertGreaterEqual(lp.checks, 1)


if __name__ == "__main__":
    unittest.main()
