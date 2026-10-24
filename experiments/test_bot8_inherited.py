"""
Тесты новых возможностей bot8 (задача #17): размер контекста (/контекст, /автоконтекст,
/сброс контекста), режим базы знаний (/раг вкл|выкл, /лимит чанков), рассуждения модели
(/рассуждения вкл|выкл), /мысль без повтора, остановка «0» из терминала и только админом.

Запуск: python3 -m unittest experiments/test_bot8_new.py -v
"""
import io
import os
import sys
import random
import inspect
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_bot8 import bot8, filosof, Bot8Case, CHAT, USER, ADMIN, START  # noqa: E402
from test_bot8_features import SOKRAT  # noqa: E402

rag = bot8.rag


def history_part(prompt):
    return prompt.split("ИСТОРИЯ:\n", 1)[1].split("\n\nНОВ", 1)[0]


class ApiError(Exception):
    def __init__(self, message, status_code):
        super().__init__(message)
        self.status_code = status_code


# ---------- /контекст, /автоконтекст, /сброс контекста ----------
class TestContextCommands(Bot8Case):
    def fill(self, n, start_ts, step=10, cmid0=5000):
        """n старых сообщений «старое i» с интервалом step секунд."""
        for i in range(n):
            bot8.db_add_message(CHAT, USER, False, f"старое {i}", start_ts + i * step, cmid=cmid0 + i)

    def ask_prompt(self):
        self.say("Грань, итог?")
        self.tick(9)
        return self.llm.prompt()

    def test_show_set_cap_and_reset(self):
        self.say("/контекст")
        self.assertIn(f"Контекст: {bot8.CONTEXT_SIZE} сообщений (стандарт)", self.sent()[-1])
        self.say("/Контекст 50")
        self.assertEqual(bot8.context_size(CHAT), 50)
        self.assertIn("Контекст: 50 последних сообщений.", self.sent()[-1])
        self.say("/контекст 100000")
        self.assertEqual(bot8.context_size(CHAT), bot8.CONTEXT_SIZE_MAX)
        self.assertIn(f"максимум {bot8.CONTEXT_SIZE_MAX}", self.sent()[-1])
        self.say("/контекст сброс")
        self.assertEqual(bot8.context_setting(CHAT), (bot8.CONTEXT_SIZE, "стандарт"))
        self.assertEqual(self.llm.calls, [])            # служебные команды LLM не тратят

    def test_context_is_per_chat(self):
        self.say("/контекст 70")
        self.assertEqual(bot8.context_size(CHAT + 1), bot8.CONTEXT_SIZE)

    def test_set_size_ignores_memory_hours(self):
        # 40 сообщений суточной давности: стандартный контекст берёт из них только CONTEXT_MIN_MESSAGES
        self.fill(40, START - 86400)
        history = history_part(self.ask_prompt())
        self.assertEqual(history.count("старое"), bot8.CONTEXT_MIN_MESSAGES)
        self.say("/контекст 100")
        history = history_part(self.ask_prompt())
        self.assertIn("старое 0\n", history)            # заданный размер — без ограничения по часам

    def test_autocontext_by_forwarded_message(self):
        self.fill(100, START - 2000)
        self.t = START
        self.say("/автоконтекст", fwd_messages=[{"from_id": USER, "conversation_message_id": 5040,
                                                  "peer_id": CHAT, "date": START - 2000 + 400,
                                                  "text": "старое 40"}])
        # от «старое 40» до самой команды: 60 + 1
        self.assertEqual(bot8.context_setting(CHAT), (61, "авто"))
        self.assertIn("Автоконтекст: от сообщения", self.sent()[-1])
        self.assertIn("61 сообщений", self.sent()[-1])
        history = history_part(self.ask_prompt())
        self.assertTrue(history.startswith("[16.10 14:"), history[:40])
        self.assertIn("старое 40\n", history)
        self.assertNotIn("старое 39\n", history)

    def test_autocontext_by_reply_and_by_text_without_cmid(self):
        self.fill(50, START - 1000)
        self.say("/автоконтекст", reply_message={"from_id": USER, "conversation_message_id": 5030,
                                                  "date": START - 1000 + 300, "text": "старое 30"})
        self.assertEqual(bot8.context_size(CHAT), 21)
        # пересланное без cmid (ВК так присылает пересланные из беседы) — по автору и времени
        self.say("/автоконтекст", fwd_messages=[{"from_id": USER, "date": START - 1000 + 100,
                                                  "text": "старое 10"}])
        # старые 10..49 + обе команды; служебный ответ бота на первую не считается
        self.assertEqual(bot8.context_size(CHAT), 40 + 2)

    def test_autocontext_message_not_in_history(self):
        self.fill(20, START - 500)
        self.say("/контекст 40")
        self.say("/автоконтекст", fwd_messages=[{"from_id": 777, "date": START - 99999,
                                                  "text": "из другого чата"}])
        self.assertIn("нет в истории беседы", self.sent()[-1])
        self.assertEqual(bot8.context_setting(CHAT), (40, "задан"))

    def test_autocontext_period(self):
        self.fill(10, START - 3 * 3600, step=60, cmid0=6000)        # 3 часа назад
        self.fill(5, START - 3600, step=60, cmid0=7000)             # час назад
        self.say("/Автоконтекст 2 ч")
        self.assertEqual(bot8.context_setting(CHAT), (6, "авто"))   # 5 + сама команда
        self.assertIn("за последние 2 ч — 6 сообщений", self.sent()[-1])
        self.say("/автоконтекст завтра")
        self.assertIn("Не понял период", self.sent()[-1])

    def test_autocontext_without_argument_explains(self):
        self.say("/автоконтекст")
        self.assertIn("перешлите сообщение", self.sent()[-1])
        self.assertEqual(bot8.context_setting(CHAT)[1], "стандарт")

    def test_reset_context(self):
        self.fill(30, START - 600)
        self.say("/контекст 50")
        self.say("/автоконтекст 1 ч")
        self.assertEqual(bot8.context_setting(CHAT)[1], "авто")
        self.say("/сброс контекста")
        self.assertEqual(bot8.context_setting(CHAT), (50, "задан"))   # к заданному
        self.assertIn("Контекст сброшен: 50 сообщений", self.sent()[-1])
        self.say("/контекст сброс")
        self.say("/автоконтекст 1 ч")
        self.say("/сброс контекста")
        self.assertEqual(bot8.context_setting(CHAT), (bot8.CONTEXT_SIZE, "стандарт"))
        self.say("/сбросить контекст 20")
        self.assertEqual(bot8.context_setting(CHAT), (20, "задан"))

    def test_help_mentions_context_commands(self):
        self.say("/помощь")
        text = self.sent()[-1]
        for cmd in ("/контекст N", "/автоконтекст", "/сброс контекста", "/раг вкл", "/рассуждения вкл",
                    "/лимит чанков N"):
            self.assertIn(cmd, text)
        self.assertNotIn("РАГ+", text)
        self.assertIn(f"• контекст: {bot8.CONTEXT_SIZE} сообщений (стандарт)", text)
        self.assertIn("• рассуждения: выключены", text)


class TestContextParsing(unittest.TestCase):
    def test_commands(self):
        P = bot8.parse_command
        self.assertEqual(P("/Контекст 50"), ("контекст", "50"))
        self.assertEqual(P("/контекст"), ("контекст", None))
        self.assertEqual(P("/контекст сброс"), ("контекст", "сброс"))
        self.assertEqual(P("/Автоконтекст 2 ч"), ("автоконтекст", "2 ч"))
        self.assertEqual(P("/автоконтекст"), ("автоконтекст", None))
        self.assertEqual(P("/авто контекст 1д"), ("автоконтекст", "1д"))
        self.assertEqual(P("/сброс контекста"), ("сброс контекста", None))
        self.assertEqual(P("/сброс контекста 20"), ("сброс контекста", "20"))
        self.assertEqual(P("/Рассуждения вкл"), ("рассуждения", True))
        self.assertEqual(P("/рассуждения выкл"), ("рассуждения", False))
        self.assertEqual(P("/рассуждения"), ("рассуждения", None))
        self.assertEqual(P("/лимит чанков 10"), ("лимит чанков", "10"))
        self.assertEqual(P("/лимит фрагментов"), ("лимит чанков", None))

    def test_parse_period(self):
        pp = bot8.parse_period
        self.assertEqual(pp("2 ч"), 7200)
        self.assertEqual(pp("2ч"), 7200)
        self.assertEqual(pp("2 часа"), 7200)
        self.assertEqual(pp("2,5 ч"), 9000)
        self.assertEqual(pp("30 мин"), 1800)
        self.assertEqual(pp("1 день"), 86400)
        self.assertEqual(pp("1 неделя"), 7 * 86400)
        self.assertEqual(pp("3"), 3 * 3600)
        self.assertIsNone(pp("2 месяца"))
        self.assertIsNone(pp("вчера"))
        self.assertIsNone(pp("0"))


# ---------- /раг вкл | выкл: последовательная генерация ----------
class TestRagMode(Bot8Case):
    def setUp(self):
        super().setUp()
        os.makedirs(bot8.RAG_DIR)
        with open(os.path.join(bot8.RAG_DIR, "sokrat.txt"), "w", encoding="utf-8") as f:
            f.write(SOKRAT)
        self.llm.answer = lambda kw: ("Добродетель есть знание [1]."
                                      if "ФРАГМЕНТЫ БАЗЫ ЗНАНИЙ" in kw["messages"][1]["content"]
                                      else "Ответ по истории")

    def test_mode_off_by_default_and_toggle(self):
        self.assertFalse(bot8.rag_mode(CHAT))
        self.say("Грань, что такое добродетель?")
        self.tick(9)
        self.assertNotIn("ФРАГМЕНТЫ", self.llm.prompt())
        self.assertEqual(self.sent()[-1], "Ответ по истории")
        self.say("/раг вкл")
        self.assertTrue(bot8.rag_mode(CHAT))
        self.assertIn("включён", self.sent()[-1])
        self.say("/раг выкл")
        self.assertFalse(bot8.rag_mode(CHAT))
        self.assertIn("выключен", self.sent()[-1])
        self.assertEqual(len(self.llm.calls), 1)

    def test_found_answer_only_from_fragments_with_history(self):
        self.say("мы спорили о Сократе")
        self.say("/раг вкл")
        self.say("Грань, что такое добродетель?")
        self.tick(9)
        self.assertEqual(len(self.llm.calls), 1)
        call = self.llm.calls[0]
        self.assertIn(rag.CHAT_RULES, call["messages"][0]["content"])
        prompt = self.llm.prompt()
        self.assertIn("мы спорили о Сократе", history_part(prompt))          # история — тоже есть
        self.assertIn("ФРАГМЕНТЫ БАЗЫ ЗНАНИЙ:\n\n[1]", prompt)
        self.assertIn("добродетель есть знание", prompt)
        self.assertIn(rag.NO_ANSWER, prompt)
        self.assertGreaterEqual(call["max_tokens"], bot8.RAG_MAX_TOKENS)
        text = self.sent()[-1]
        self.assertTrue(text.startswith("Добродетель есть знание [1]."))
        self.assertIn("Источники:\n[1] «sokrat»", text)
        self.assertNotIn(bot8.RAG_NO_RESULT_PREFIX, text)

    def test_nothing_found_prefix_and_normal_answer(self):
        self.say("/раг вкл")
        self.say("Грань, какая погода в Тюмени?")
        self.tick(9)
        self.assertEqual(len(self.llm.calls), 1)                 # поиск пуст — сразу обычный ответ
        self.assertNotIn("ФРАГМЕНТЫ", self.llm.prompt())
        self.assertEqual(self.sent()[-1], f"{bot8.RAG_NO_RESULT_PREFIX}\n\nОтвет по истории")
        self.assertTrue(self.sent()[-1].startswith("Поиск по базе знаний не дал результата"))
        self.assertEqual(bot8.daily_state(CHAT)["count"], 1)

    def test_model_says_no_answer_falls_back(self):
        self.llm.answer = lambda kw: (rag.NO_ANSWER if "ФРАГМЕНТЫ" in kw["messages"][1]["content"]
                                      else "Ответ по истории")
        self.say("/раг вкл")
        self.say("Грань, что говорил Сократ о погоде?")
        self.tick(9)
        self.assertEqual(len(self.llm.calls), 2)                 # по фрагментам, затем обычный
        self.assertIn("ФРАГМЕНТЫ", self.llm.prompt(0))
        self.assertNotIn("ФРАГМЕНТЫ", self.llm.prompt(1))
        self.assertNotIn(rag.CHAT_RULES, self.llm.calls[1]["messages"][0]["content"])
        self.assertEqual(self.sent()[-1], f"{bot8.RAG_NO_RESULT_PREFIX}\n\nОтвет по истории")

    def test_prefix_not_in_next_history(self):
        self.say("/раг вкл")
        self.say("Грань, какая погода?")
        self.tick(9)
        self.say("Грань, а завтра?")
        self.tick(9)
        history = history_part(self.llm.prompt())
        self.assertIn("Ответ по истории", history)
        self.assertNotIn("Поиск по базе знаний", history)

    def test_search_query_without_address(self):
        self.say("/раг вкл")
        self.assertEqual(bot8.rag_query([bot8.Request({"peer_id": CHAT}, text="Грань, что такое благо?")]),
                         "что такое благо?")

    def test_chunks_limit_admin_and_passed_to_search(self):
        self.say("/лимит чанков 1")
        self.assertIn("только админу", self.sent()[-1])
        self.assertEqual(bot8.chunks_limit(CHAT), bot8.RAG_TOP_K)
        self.say("/лимит чанков 1", from_id=ADMIN)
        self.assertEqual(bot8.chunks_limit(CHAT), 1)
        self.say("/раг вкл")
        self.say("Грань, кто ученик Сократа и что такое добродетель?")
        self.tick(9)
        prompt = self.llm.prompt()
        self.assertIn("[1]", prompt)
        self.assertNotIn("\n[2] ", prompt)                     # передан только один фрагмент
        self.say("/лимит чанков 999", from_id=ADMIN)
        self.assertEqual(bot8.chunks_limit(CHAT), bot8.RAG_TOP_K_MAX)
        self.say("/лимит чанков сброс", from_id=ADMIN)
        self.assertEqual(bot8.chunks_limit(CHAT), bot8.RAG_TOP_K)

    def test_one_shot_rag_still_strict(self):
        self.say("/раг вкл")
        self.say("/раг квантовая хромодинамика")
        self.assertEqual(self.llm.calls, [])
        self.assertEqual(self.sent()[-1], f"{bot8.SYSTEM_MARK} {bot8.RAG_NOTHING_FOUND}")

    def test_status_shows_mode_and_limit(self):
        self.say("/раг вкл")
        self.say("/раг")
        self.assertIn("Режим базы знаний в этой беседе: включён, фрагментов в запрос — до "
                      f"{bot8.RAG_TOP_K}", self.sent()[-1])

    def test_broken_kb_answers_without_it(self):
        self.say("/раг вкл")
        with mock.patch.object(bot8, "sync_kb", side_effect=RuntimeError("диск")):
            self.say("Грань, что такое добродетель?")
            self.tick(9)
        self.assertEqual(self.sent()[-1], f"{bot8.RAG_NO_RESULT_PREFIX}\n\nОтвет по истории")


# ---------- /рассуждения вкл | выкл ----------
class TestReasoning(Bot8Case):
    def test_off_by_default_thinking_disabled(self):
        self.assertFalse(bot8.REASONING_DEFAULT)
        self.llm.answer = ("Ответ", "stop", "я подумал")
        self.say("Грань, привет")
        self.tick(9)
        self.assertEqual(self.llm.calls[0]["extra_body"], {"thinking": {"type": "disabled"}})
        self.assertEqual(self.sent(), ["Ответ"])                 # рассуждение не показано

    def test_on_prepends_block(self):
        self.say("/Рассуждения вкл")
        self.assertIn("Рассуждения включены", self.sent()[-1])
        self.llm.answer = ("Ответ", "stop", "Сначала вспомню, кто спрашивает.")
        self.say("Грань, привет")
        self.tick(9)
        self.assertEqual(self.llm.calls[0]["extra_body"], {"thinking": {"type": "enabled"}})
        self.assertEqual(self.sent()[-1], "#рассуждения#\nСначала вспомню, кто спрашивает.\n"
                                          "#рассуждения#\n\nОтвет")
        self.say("/рассуждения выкл")
        self.say("Грань, ещё")
        self.tick(9)
        self.assertEqual(self.sent()[-1], "Ответ")
        self.assertEqual(self.llm.calls[-1]["extra_body"], {"thinking": {"type": "disabled"}})

    def test_think_tags_and_missing_reasoning(self):
        self.say("/рассуждения вкл")
        self.llm.answer = "<think>мысль в тегах</think>\nОтвет"
        self.say("Грань, привет")
        self.tick(9)
        self.assertEqual(self.sent()[-1], "#рассуждения#\nмысль в тегах\n#рассуждения#\n\nОтвет")
        self.llm.answer = "Ответ без рассуждения"
        self.say("Грань, а так?")
        self.tick(9)
        self.assertEqual(self.sent()[-1], "#рассуждения#\n(модель не вернула рассуждение)\n"
                                          "#рассуждения#\n\nОтвет без рассуждения")

    def test_reasoning_not_in_history(self):
        self.say("/рассуждения вкл")
        self.llm.answer = ("Ответ", "stop", "тайная мысль")
        self.say("Грань, привет")
        self.tick(9)
        self.say("Грань, и?")
        self.tick(9)
        history = history_part(self.llm.prompt())
        self.assertIn("Ответ", history)
        self.assertNotIn("тайная мысль", history)
        self.assertNotIn(bot8.REASONING_MARK, history)

    def test_per_chat_and_help(self):
        self.say("/рассуждения вкл")
        self.assertTrue(bot8.reasoning_enabled(CHAT))
        self.assertFalse(bot8.reasoning_enabled(CHAT + 1))
        self.say("/рассуждения")
        self.assertIn("Рассуждения вкл", self.sent()[-1])

    def test_api_rejects_thinking_param(self):
        def answer(kw):
            if "thinking" in (kw.get("extra_body") or {}):
                raise ApiError("Error code: 400 - unknown parameter thinking", 400)
            return ("Ответ", "stop", "рассуждение всё равно есть")
        self.llm.answer = answer
        self.say("/рассуждения вкл")
        self.say("Грань, привет")
        self.tick(9)
        self.assertEqual(len(self.llm.calls), 2)                  # отказ, повтор без параметра
        self.assertNotIn("extra_body", self.llm.calls[1])
        self.assertFalse(bot8.REASONING_PARAM_OK)
        self.assertEqual(self.sent()[-1], "#рассуждения#\nрассуждение всё равно есть\n#рассуждения#\n\nОтвет")
        self.say("Грань, ещё")
        self.tick(9)
        self.assertEqual(len(self.llm.calls), 3)                  # параметр больше не передаётся
        self.assertNotIn("extra_body", self.llm.calls[2])

    def test_other_api_error_not_retried(self):
        def boom(kw):
            raise ApiError("Error code: 402 - insufficient balance", 402)
        self.llm.answer = boom
        self.say("/рассуждения вкл")
        self.say("Грань, привет")
        self.tick(9)
        self.assertEqual(len(self.llm.calls), 1)
        self.assertTrue(bot8.REASONING_PARAM_OK)
        self.assertIn("ошибка API: Error code: 402", self.sent()[-1])

    def test_reasoning_shown_when_answer_empty(self):
        self.say("/рассуждения вкл")
        self.llm.answer = ("", "length", "думал-думал")
        self.say("Грань, привет")
        self.tick(9)
        text = self.sent()[-1]
        self.assertTrue(text.startswith(bot8.SYSTEM_MARK))
        self.assertIn("#рассуждения#\nдумал-думал", text)
        self.assertIn("/рассуждения выкл", text)

    def test_long_reasoning_shortened(self):
        self.say("/рассуждения вкл")
        self.llm.answer = ("Ответ", "stop", "а" * 5000 + "б" * 5000)
        self.say("Грань, привет")
        self.tick(9)
        text = "".join(self.sent()[-3:])
        self.assertIn("сокращено", text)
        self.assertLess(text.count("а") + text.count("б"), bot8.REASONING_MAX_CHARS + 100)

    def test_compression_request_without_reasoning(self):
        bot8.db_state_set(f"chars_{CHAT}", f"3000|{int(self.t)}")
        for i in range(30):
            bot8.db_add_message(CHAT, USER, False, f"довод {i} " + "x" * 150, int(self.t) - 900 + i * 10,
                                cmid=7000 + i)
        self.llm.answer = lambda kw: (("СЖАТО", "stop", "мысль о сжатии")
                                      if "Сожми" in kw["messages"][1]["content"]
                                      else ("Ответ", "stop", "мысль об ответе"))
        self.say("/рассуждения вкл")
        self.say("Грань, кто прав?")
        self.tick(9)
        self.assertEqual(self.llm.calls[0]["extra_body"], {"thinking": {"type": "disabled"}})
        self.assertEqual(self.llm.calls[1]["extra_body"], {"thinking": {"type": "enabled"}})
        self.assertIn("мысль об ответе", self.sent()[-1])
        self.assertNotIn("мысль о сжатии", self.sent()[-1])

    def test_strip_bot_extras(self):
        s = bot8.strip_bot_extras
        self.assertEqual(s("#рассуждения#\nдумаю\n#рассуждения#\n\nОтвет"), "Ответ")
        self.assertEqual(s("#рассуждения#\nдлинное начало"), "")             # 1-я часть длинного
        self.assertEqual(s("конец рассуждения\n#рассуждения#\n\nОтвет"), "Ответ")  # 2-я часть
        self.assertEqual(s(f"{bot8.RAG_NO_RESULT_PREFIX}\n\nОтвет"), "Ответ")
        self.assertEqual(s("Обычный ответ"), "Обычный ответ")


# ---------- /мысль: без повтора, «Пиши кратко» вместо «100 слов» ----------
class TestThoughtBot8(Bot8Case):
    def test_prompts_without_word_limit(self):
        self.assertIn("Пиши кратко", filosof.SYSTEM_PROMPT)
        code = [ln for ln in inspect.getsource(filosof).splitlines() if not ln.lstrip().startswith("#")]
        self.assertNotIn("100 слов", "\n".join(code))
        for prompt in (filosof.SYSTEM_PROMPT, filosof.PROMPT_IMAGE, filosof.PROMPT_QUESTION,
                       filosof.PROMPT_ANSWER):
            self.assertNotIn("100", prompt)

    def test_no_retry_no_bigger_limit(self):
        bot8.FILOSOF = filosof.ThoughtGenerator(rng=random.Random(1))
        self.llm.answer = ("", "length")
        self.say("/мысль")
        self.assertEqual([c["max_tokens"] for c in self.llm.calls], [bot8.tokens_limit(CHAT)])

    def test_thought_with_reasoning(self):
        bot8.FILOSOF = filosof.ThoughtGenerator(rng=random.Random(1))
        replies = iter([("Образ", "stop", "р1"), ("Вопрос?", "stop", "р2"), ("Ответ.", "stop", "р3")])
        self.llm.answer = lambda kw: next(replies)
        self.say("/рассуждения вкл")
        self.say("/мысль")
        text = self.sent()[-1]
        self.assertTrue(text.startswith("#рассуждения#\nр1\n\nр2\n\nр3\n#рассуждения#"))
        self.assertIn("Ответ.", text)


# ---------- остановка: «0» в терминале и «Грань, стоп машина» только админу ----------
class TestTerminalStop(Bot8Case):
    def test_zero_stops(self):
        bot8.terminal_stop_listener(io.StringIO("привет\n\n 0 \nещё\n"))
        self.assertTrue(bot8.STOP_REQUESTED)

    def test_other_input_and_eof_do_not_stop(self):
        bot8.terminal_stop_listener(io.StringIO("1\n00\nстоп\n"))
        self.assertFalse(bot8.STOP_REQUESTED)
        bot8.terminal_stop_listener(io.StringIO(""))
        self.assertFalse(bot8.STOP_REQUESTED)

    def test_broken_stdin_ignored(self):
        class Broken:
            def readline(self):
                raise OSError("stdin закрыт")
        bot8.terminal_stop_listener(Broken())
        self.assertFalse(bot8.STOP_REQUESTED)

    def test_thread_started_only_with_stdin(self):
        with mock.patch.object(bot8.sys, "stdin", None):
            self.assertIsNone(bot8.start_terminal_stop())
        with mock.patch.object(bot8.sys, "stdin", io.StringIO("0\n")):
            t = bot8.start_terminal_stop()
            t.join(5)
        self.assertTrue(bot8.STOP_REQUESTED)

    def test_cli_flag(self):
        self.assertTrue(bot8.parse_args(["--no-terminal-stop"]).no_terminal_stop)
        self.assertFalse(bot8.parse_args([]).no_terminal_stop)

    def test_stop_phrase_admin_only(self):
        self.say("Грань, стоп машина")
        self.assertFalse(bot8.STOP_REQUESTED)
        self.assertEqual(self.sent()[-1], f"{bot8.SYSTEM_MARK} Остановить бота может только админ беседы.")
        self.say("Грань, стоп машина", from_id=ADMIN)
        self.assertTrue(bot8.STOP_REQUESTED)


if __name__ == "__main__":
    unittest.main()
