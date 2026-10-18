"""
Тесты для Прочее/bot4.py (задача #11). Без токенов и сети: ВК и LLM подменены.

Запуск: python3 -m unittest experiments/test_bot4.py -v
"""
import os
import sys
import json
import time
import sqlite3
import tempfile
import unittest
import importlib.util

HERE = os.path.dirname(os.path.abspath(__file__))
BOT4_PATH = os.path.join(HERE, "..", "Прочее", "bot4.py")

spec = importlib.util.spec_from_file_location("bot4", BOT4_PATH)
bot4 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot4)

GROUP = 555
BOT = -GROUP
CHAT = 2000000001
NOW = int(time.time())


class Obj:
    """Пространство имён для vk.messages / vk.users и т. п."""


class FakeVk:
    def __init__(self):
        self.sent = []
        self.next_cmid = 1000
        self.messages = Obj()
        self.messages.send = self._send
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


class FakeLLM:
    def __init__(self, answer="Ответ Грани", fail_with_images=False):
        self.calls = []
        self.answer = answer
        self.fail_with_images = fail_with_images
        self.chat = Obj()
        self.chat.completions = Obj()
        self.chat.completions.create = self._create

    def _create(self, **kw):
        self.calls.append(kw)
        content = kw["messages"][1]["content"]
        if self.fail_with_images and isinstance(content, list):
            raise RuntimeError("model does not support images")
        msg = Obj()
        msg.content = self.answer
        choice = Obj()
        choice.message = msg
        resp = Obj()
        resp.choices = [choice]
        return resp

    def last_prompt(self):
        content = self.calls[-1]["messages"][1]["content"]
        return content if isinstance(content, str) else content[0]["text"]

    def last_images(self):
        content = self.calls[-1]["messages"][1]["content"]
        return [] if isinstance(content, str) else [p["image_url"]["url"] for p in content[1:]]


def chat_msg(cmid, text, from_id=101, ts=None, peer_id=CHAT, **extra):
    """Сообщение беседы так, как его отдаёт Bots Long Poll: id = 0."""
    m = {"id": 0, "conversation_message_id": cmid, "peer_id": peer_id,
         "from_id": from_id, "text": text, "date": ts or NOW}
    m.update(extra)
    return m


def photo(url, w=800, h=600):
    return {"type": "photo", "photo": {"sizes": [
        {"type": "s", "url": url + "?small", "width": 75, "height": 56},
        {"type": "x", "url": url, "width": w, "height": h},
        {"type": "w", "url": url + "?huge", "width": 2560, "height": 1920},
    ]}}


class Bot4Case(unittest.TestCase):
    def setUp(self):
        bot4.DB = bot4.db_init(":memory:")
        bot4.vk = FakeVk()
        bot4.llm_client = FakeLLM()
        bot4.BOT_GROUP_ID = BOT
        bot4.VK_USER_TOKEN = None
        bot4.LLM_MIN_INTERVAL_SEC = 0
        bot4.IMAGE_MODE = "url"
        bot4.STOP_REQUESTED = False

    def tearDown(self):
        bot4.DB.close()

    def count(self, peer_id=CHAT):
        return bot4.DB.execute("SELECT COUNT(*) FROM messages WHERE peer_id=?", (peer_id,)).fetchone()[0]


class TestContextBugs(Bot4Case):
    def test_chat_messages_with_zero_id_are_all_stored(self):
        # bot3: сохранялось только первое (см. experiments/bot3_context_bug.py)
        for i, t in enumerate(["привет", "как дела?", "норм"]):
            bot4.handle_message(chat_msg(10 + i, t, ts=NOW + i))
        self.assertEqual(self.count(), 3)

    def test_duplicates_are_ignored(self):
        bot4.handle_message(chat_msg(10, "привет"))
        bot4.handle_message(chat_msg(10, "привет"))
        self.assertEqual(self.count(), 1)

    def test_bot_reply_is_saved_and_appears_in_next_context(self):
        bot4.handle_message(chat_msg(10, "Грань, как тебя зовут?"))
        self.assertEqual(len(bot4.vk.sent), 1)
        bot4.handle_message(chat_msg(12, "Грань, а что ты ответила выше?", ts=NOW + 5))
        prompt = bot4.llm_client.last_prompt()
        history = prompt.split("НОВОЕ СООБЩЕНИЕ")[0]
        self.assertIn("Грань разумного: Ответ Грани", history)
        self.assertIn("как тебя зовут", history)

    def test_bot_echo_event_does_not_duplicate(self):
        bot4.handle_message(chat_msg(10, "Грань, привет"))
        sent_cmid = bot4.vk.next_cmid
        # ВК присылает исходящее сообщение событием message_reply
        bot4.store_vk_message(CHAT, chat_msg(sent_cmid, "Ответ Грани", from_id=BOT))
        self.assertEqual(self.count(), 2)

    def test_echo_attaches_cmid_when_send_returned_none(self):
        bot4.db_add_message(CHAT, BOT, True, "текст бота", NOW)
        bot4.store_vk_message(CHAT, chat_msg(77, "текст бота", from_id=BOT))
        self.assertEqual(self.count(), 1)
        cmid = bot4.DB.execute("SELECT cmid FROM messages").fetchone()[0]
        self.assertEqual(cmid, 77)

    def test_old_conversation_still_has_context(self):
        # bot3: тишина > 30 минут → в контексте только само новое сообщение
        old = NOW - 5 * 3600 * 24
        for i in range(15):
            bot4.handle_message(chat_msg(10 + i, f"старое {i}", ts=old + i))
        bot4.handle_message(chat_msg(100, "грань, о чём мы говорили?"))
        history = bot4.llm_client.last_prompt().split("НОВОЕ СООБЩЕНИЕ")[0]
        lines = [l for l in history.splitlines() if "старое" in l]
        self.assertEqual(len(lines), bot4.CONTEXT_MIN_MESSAGES)
        self.assertIn("старое 14", history)  # самые свежие из старых
        self.assertNotIn("грань, о чём мы говорили", history)  # новое — не дублируется

    def test_context_takes_newest_messages(self):
        for i in range(bot4.CONTEXT_SIZE + 20):
            bot4.handle_message(chat_msg(10 + i, f"msg{i:03d}", ts=NOW - 600 + i))
        rows = bot4.db_get_context(CHAT, bot4.CONTEXT_SIZE)
        self.assertEqual(len(rows), bot4.CONTEXT_SIZE)
        self.assertEqual(rows[-1][2], f"msg{bot4.CONTEXT_SIZE + 19:03d}")
        self.assertLess(rows[0][3], rows[-1][3])

    def test_forget_command_resets_context(self):
        bot4.handle_message(chat_msg(10, "секретная тема", ts=NOW - 10))
        bot4.handle_message(chat_msg(11, "/забудь", ts=NOW - 5))
        bot4.handle_message(chat_msg(13, "грань, привет"))
        self.assertNotIn("секретная тема", bot4.llm_client.last_prompt())

    def test_context_char_limit_drops_oldest(self):
        rows = [(101, 0, "а" * 100, NOW + i, []) for i in range(10)]
        text = bot4.format_context(rows, max_chars=500)
        self.assertLessEqual(len(text), 500)
        # строка = «[дд.мм чч:мм] Имя101: » (22 символа) + 100 → влезают 4 последние
        self.assertEqual(len(text.splitlines()), 4)
        self.assertTrue(text.endswith("а" * 100))


class TestReplies(Bot4Case):
    def test_answers_reply_to_bot_without_trigger(self):
        bot_msg = chat_msg(20, "Я думаю, что это кот", from_id=BOT)
        bot4.handle_message(chat_msg(21, "почему ты так думаешь?", reply_message=bot_msg))
        self.assertEqual(len(bot4.vk.sent), 1)
        self.assertIn("в ответ Грань разумного", bot4.llm_client.last_prompt())

    def test_ignores_reply_to_human_without_trigger(self):
        human = chat_msg(20, "что-то", from_id=102)
        bot4.handle_message(chat_msg(21, "согласен", reply_message=human))
        self.assertEqual(bot4.vk.sent, [])

    def test_ignores_chat_message_without_trigger(self):
        bot4.handle_message(chat_msg(21, "просто болтаем"))
        self.assertEqual(bot4.vk.sent, [])

    def test_answers_in_private_without_trigger(self):
        bot4.handle_message(chat_msg(5, "привет", peer_id=101))
        self.assertEqual(len(bot4.vk.sent), 1)

    def test_chat_reply_uses_forward_with_cmid(self):
        bot4.handle_message(chat_msg(42, "грань, привет"))
        params = bot4.vk.sent[0]
        self.assertNotIn("reply_to", params)
        fwd = json.loads(params["forward"])
        self.assertEqual(fwd, {"peer_id": CHAT, "conversation_message_ids": [42], "is_reply": 1})

    def test_private_reply_without_cmid_uses_reply_to(self):
        m = {"id": 900, "peer_id": 101, "from_id": 101, "text": "привет", "date": NOW}
        bot4.handle_message(m)
        self.assertEqual(bot4.vk.sent[0]["reply_to"], 900)

    def test_long_answer_is_split(self):
        bot4.llm_client.answer = ("слово " * 2000).strip()
        bot4.handle_message(chat_msg(42, "грань, расскажи длинно"))
        self.assertGreater(len(bot4.vk.sent), 1)
        self.assertTrue(all(len(p["message"]) <= bot4.VK_MESSAGE_LIMIT for p in bot4.vk.sent))
        self.assertIn("forward", bot4.vk.sent[0])
        self.assertNotIn("forward", bot4.vk.sent[1])

    def test_answer_signature_is_removed(self):
        self.assertEqual(bot4.clean_answer("Грань разумного: привет"), "привет")
        self.assertEqual(bot4.clean_answer("[14.10 10:00] Грань: ок"), "ок")
        self.assertEqual(bot4.clean_answer("Грань — это я"), "Грань — это я")


class TestForwardedAndPosts(Bot4Case):
    def test_forwarded_text_is_in_history(self):
        nested = {"from_id": 103, "date": NOW - 100, "text": "глубоко вложенное"}
        fwd = {"from_id": 102, "date": NOW - 60, "text": "пересланный текст", "fwd_messages": [nested]}
        bot4.handle_message(chat_msg(30, "", fwd_messages=[fwd]))
        bot4.handle_message(chat_msg(31, "грань, что думаешь о пересланном?"))
        history = bot4.llm_client.last_prompt().split("НОВОЕ СООБЩЕНИЕ")[0]
        self.assertIn("[переслано от Имя102", history)
        self.assertIn("> пересланный текст", history)
        self.assertIn("> > глубоко вложенное", history)
        self.assertIn("Имя103", history)

    def test_forwarded_in_new_message(self):
        fwd = {"from_id": 102, "date": NOW - 60, "text": "мнение друга"}
        bot4.handle_message(chat_msg(31, "грань, глянь", fwd_messages=[fwd]))
        new = bot4.llm_client.last_prompt().split("НОВОЕ СООБЩЕНИЕ")[1]
        self.assertIn("мнение друга", new)

    def test_shared_post_text_and_repost(self):
        post = {"id": 7, "owner_id": -42, "from_id": -42, "text": "Учёные открыли новую звезду",
                "attachments": [photo("https://img/post.jpg")],
                "copy_history": [{"id": 1, "owner_id": 300, "from_id": 300, "text": "первоисточник"}]}
        bot4.handle_message(chat_msg(31, "грань, что скажешь?",
                                     attachments=[{"type": "wall", "wall": post}]))
        new = bot4.llm_client.last_prompt().split("НОВОЕ СООБЩЕНИЕ")[1]
        self.assertIn("[запись Паблик42: «Учёные открыли новую звезду»]", new)
        self.assertIn("репост", new)
        self.assertIn("первоисточник", new)
        self.assertIn("https://img/post.jpg", bot4.llm_client.last_images())

    def test_post_without_text_is_fetched_with_user_token(self):
        bot4.VK_USER_TOKEN = "user-token"
        bot4.vk.wall = Obj()
        bot4.vk.wall.getById = lambda posts, access_token: {
            "items": [{"id": 7, "owner_id": -42, "from_id": -42, "text": "полный текст поста"}]}
        bot4.handle_message(chat_msg(31, "грань?",
                                     attachments=[{"type": "wall", "wall": {"id": 7, "owner_id": -42}}]))
        self.assertIn("полный текст поста", bot4.llm_client.last_prompt())

    def test_summarize_shared_post(self):
        post = {"id": 7, "owner_id": -42, "from_id": -42, "text": "Длинный пост про космос"}
        bot4.handle_message(chat_msg(31, "грань, перескажи",
                                     attachments=[{"type": "wall", "wall": post}]))
        self.assertIn("Длинный пост про космос", bot4.llm_client.last_prompt())
        self.assertIn("Перескажи суть", bot4.llm_client.last_prompt())

    def test_audio_transcript_and_markers(self):
        text, images = bot4.render_message({"text": "", "attachments": [
            {"type": "audio_message", "audio_message": {"transcript": "привет голосом"}},
            {"type": "sticker", "sticker": {}},
        ]})
        self.assertIn("[голосовое: «привет голосом»]", text)
        self.assertIn("[стикер]", text)
        self.assertEqual(images, [])


class TestImages(Bot4Case):
    def test_photo_size_choice(self):
        self.assertEqual(bot4.photo_url(photo("https://img/a.jpg")["photo"]), "https://img/a.jpg")

    def test_images_sent_to_llm(self):
        bot4.handle_message(chat_msg(31, "грань, что на фото?",
                                     attachments=[photo("https://img/cat.jpg")]))
        self.assertEqual(bot4.llm_client.last_images(), ["https://img/cat.jpg"])
        self.assertIn("[фото]", bot4.llm_client.last_prompt())

    def test_recent_history_images_are_included(self):
        bot4.handle_message(chat_msg(30, "", attachments=[photo("https://img/earlier.jpg")]))
        bot4.handle_message(chat_msg(31, "грань, что было на картинке выше?"))
        self.assertEqual(bot4.llm_client.last_images(), ["https://img/earlier.jpg"])

    def test_image_limit(self):
        atts = [photo(f"https://img/{i}.jpg") for i in range(10)]
        bot4.handle_message(chat_msg(31, "грань, смотри", attachments=atts))
        self.assertEqual(len(bot4.llm_client.last_images()), bot4.MAX_IMAGES_PER_REQUEST)

    def test_fallback_without_images_on_error(self):
        bot4.llm_client = FakeLLM(fail_with_images=True)
        bot4.handle_message(chat_msg(31, "грань, что тут?", attachments=[photo("https://img/x.jpg")]))
        self.assertEqual(len(bot4.llm_client.calls), 2)
        self.assertEqual(bot4.vk.sent[0]["message"], "Ответ Грани")

    def test_image_in_forwarded_and_reply(self):
        rp = chat_msg(20, "", from_id=102, attachments=[photo("https://img/reply.jpg")])
        fwd = {"from_id": 103, "date": NOW, "text": "", "attachments": [photo("https://img/fwd.jpg")]}
        bot4.handle_message(chat_msg(31, "грань, сравни", reply_message=rp, fwd_messages=[fwd]))
        self.assertIn("https://img/fwd.jpg", bot4.llm_client.last_images())


class TestCommandsAndMigration(Bot4Case):
    def test_role_and_stop(self):
        bot4.handle_message(chat_msg(1, "/роль (Ты — строгий редактор)"))
        self.assertEqual(bot4.db_get_role(CHAT), "Ты — строгий редактор")
        bot4.handle_message(chat_msg(2, "Грань, стоп машина"))
        self.assertTrue(bot4.STOP_REQUESTED)

    def test_stale_message_is_not_answered(self):
        bot4.handle_message(chat_msg(1, "грань, ау", ts=NOW - 3600))
        self.assertEqual(bot4.vk.sent, [])

    def test_migration_from_bot3_db(self):
        with tempfile.TemporaryDirectory() as d:
            old_path = os.path.join(d, "gran.db")
            old = sqlite3.connect(old_path)
            old.execute("CREATE TABLE roles (peer_id INTEGER PRIMARY KEY, role TEXT)")
            old.execute("CREATE TABLE users (user_id INTEGER PRIMARY KEY, first_name TEXT, "
                        "last_name TEXT, updated_at INTEGER)")
            old.execute("INSERT INTO roles VALUES (?, ?)", (CHAT, "старая роль"))
            old.execute("INSERT INTO users VALUES (101, 'Иван', 'Петров', 1)")
            old.commit()
            old.close()
            bot4.migrate_from_bot3(old_path)
        self.assertEqual(bot4.db_get_role(CHAT), "старая роль")
        self.assertEqual(bot4.db_get_user_name(101), "Иван Петров")


class TestLoadHistory(Bot4Case):
    def test_initial_load_stores_chat_history(self):
        items = [chat_msg(i, f"история {i}", ts=NOW - 100 + i) for i in range(5, 0, -1)]
        bot4.vk.messages.getConversations = lambda count: {
            "items": [{"conversation": {"peer": {"id": CHAT}}}]}
        bot4.vk.messages.getHistory = lambda peer_id, count, offset: {
            "items": items[offset:offset + count]}
        bot4.load_history()
        bot4.load_history()  # повторный старт: догрузка без дублей
        self.assertEqual(self.count(), 5)
        rows = bot4.db_get_context(CHAT, 10)
        self.assertEqual([r[2] for r in rows], [f"история {i}" for i in range(1, 6)])


if __name__ == "__main__":
    unittest.main()
