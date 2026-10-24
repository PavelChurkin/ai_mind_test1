"""Issue #19: executable acceptance cases, fake VK/LLM and controlled clock."""
import io
import os
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
from test_bot8 import bot8 as bot, Bot8Case, CHAT, USER, ADMIN, START


class Issue19Case(Bot8Case):
    def seed(self, n=5, length=20):
        messages = []
        for i in range(n):
            m = dict(peer_id=CHAT, from_id=USER, date=START - 86400 + i,
                     conversation_message_id=5000+i, text=f'interval-{i} ' + 'x'*length)
            bot.store_vk_message(CHAT, m)
            messages.append(m)
        return messages

    def ask(self, text='Грань, объясни'):
        self.say(text)
        self.tick(11)
        return self.llm.prompt()

    def terminal(self, text):
        out = io.StringIO()
        bot.execute_terminal(text, stream=out)
        return out.getvalue()


class TestInterval(Issue19Case):
    def test_selection_separates_next_question_from_previous_long_request(self):
        messages = self.seed(40)
        self.say('/пауза 1')
        self.say('Грань, предыдущий вопрос ' + 'x'*4000)
        self.say('/контекст', fwd_messages=messages[:2])
        self.say('Грань, новый вопрос')
        pending = bot.peer_state(CHAT).pending
        self.assertEqual(len(pending), 2)
        self.assertIsNone(pending[0].interval)
        self.assertEqual(pending[1].interval, [messages[0]['date'], messages[1]['date']])

    def test_vk_pagination_repairs_local_gaps_including_boundary_second(self):
        messages = self.seed(405)
        # The earlier second spans two pages; fetch the whole second, not just an endpoint.
        for message in messages[:6]:
            message['date'] = messages[5]['date']
        bot.vk.history[CHAT] = messages
        bot.DB.execute('DELETE FROM messages WHERE peer_id=?', (CHAT,))
        bot.store_vk_message(CHAT, messages[3])
        bot.store_vk_message(CHAT, messages[-1])
        self.say('/контекст', fwd_messages=[messages[3], messages[-1]])
        self.assertEqual(bot.vk.history_calls, [(200, 0), (200, 200), (200, 400)])
        self.assertEqual(self.ask().count('interval-'), 405)

    def test_history_failure_preserves_pending_interval(self):
        messages = self.seed(40)
        self.say('/контекст', fwd_messages=messages[:2])
        before = bot.db_state_get(f'interval_{CHAT}')
        with mock.patch.object(bot.vk.messages, 'getHistory', side_effect=RuntimeError('offline')):
            self.say('/контекст', fwd_messages=messages[2:4])
        self.assertIn('Не удалось загрузить полную историю', self.sent()[-1])
        self.assertEqual(bot.db_state_get(f'interval_{CHAT}'), before)
        self.assertIn('interval-0 ', self.ask())

    def test_only_next_request_inclusive_reversed_and_no_model_on_command(self):
        messages = self.seed(40)
        self.say('/контекст', fwd_messages=[messages[3], messages[1]])
        self.assertEqual(self.llm.calls, [])
        self.assertIn('следующ', self.sent()[-1].lower())
        self.say('между командой и вопросом')
        prompt = self.ask()
        self.assertIn('interval-1', prompt)
        self.assertIn('interval-2', prompt)
        self.assertIn('interval-3', prompt)
        self.assertNotIn('interval-0', prompt)
        self.assertNotIn('interval-4', prompt)
        self.assertNotIn('между командой и вопросом', prompt)
        self.assertEqual(bot.context_size(CHAT), bot.CONTEXT_SIZE)
        self.assertNotIn('interval-1 ', self.ask())  # old messages outside ordinary time window/min count

    def test_all_messages_no_500_row_or_character_truncation(self):
        messages = self.seed(510, 30)
        self.say('/контекст', fwd_messages=[messages[0], messages[-1]])
        self.tick(61)  # command budget expires, but the next request still needs full interval
        prompt = self.ask()
        self.assertIn('interval-0 ', prompt)
        self.assertIn('interval-509 ', prompt)
        self.assertEqual(prompt.count('interval-'), 510)
        self.assertEqual(len(self.llm.calls), 1)  # no summary call

    def test_queued_before_command_and_after_next_request_do_not_share_interval(self):
        messages = self.seed(40)
        self.say('/пауза 1')
        self.say('Грань, первый')
        self.say('/контекст', fwd_messages=[messages[0], messages[1]])
        self.say('Грань, второй')
        self.say('Грань, третий')
        self.tick(61)
        self.assertEqual(len(self.llm.calls), 3)
        self.assertNotIn('interval-0 ', self.llm.prompt(0))
        self.assertIn('interval-0 ', self.llm.prompt(1))
        self.assertNotIn('interval-0 ', self.llm.prompt(2))

    def test_invalid_foreign_missing_and_duplicate_bounds(self):
        messages = self.seed()
        for bounds in ([messages[0]], [messages[0]]*3,
                       [messages[0], dict(messages[1], peer_id=CHAT+1)],
                       [messages[0], dict(messages[1], text='missing', date=1)],
                       [messages[0], messages[0]]):
            self.say('/контекст', fwd_messages=bounds)
            self.assertIn('⚙️', self.sent()[-1])
            self.assertIsNone(bot.db_state_get(f'interval_{CHAT}'))

    def test_same_timestamp_includes_every_message_in_second(self):
        messages = self.seed()
        bot.db_add_message(CHAT, ADMIN, False, 'same-second', messages[1]['date'], cmid=6000)
        self.say('/контекст', fwd_messages=[messages[1], messages[3]])
        self.assertIn('same-second', self.ask())

    def test_failed_answer_consumes_interval_and_reset_cancels(self):
        messages = self.seed(40)
        self.say('/контекст', fwd_messages=messages[:2])
        self.llm.answer = ''
        self.ask()
        self.llm.answer = 'ok'
        self.assertNotIn('interval-0 ', self.ask())
        self.say('/контекст', fwd_messages=messages[:2])
        self.say('/сброс контекста')
        self.assertNotIn('interval-0 ', self.ask())

    def test_interval_restores_manual_or_auto_context(self):
        messages = self.seed(40)
        self.say('/контекст 2')
        self.say('/контекст', fwd_messages=messages[:2])
        self.ask()
        self.assertEqual(bot.context_size(CHAT), 2)
        self.say('/автоконтекст 2 ч')
        old = bot.context_setting(CHAT)
        self.say('/контекст', fwd_messages=messages[:2])
        self.ask()
        self.assertEqual(bot.context_setting(CHAT), old)


class TestAttention(Issue19Case):
    def test_mixed_case_commands_and_rag_mode_keep_attention(self):
        messages = self.seed(40)
        self.say('/ВНИМАНИЕ', fwd_messages=[messages[0]])
        self.say('/РАГ ВКЛ')
        self.assertIn('interval-0 ', self.ask())  # fallback when no book matches
        self.say('/ВнИмАнИе СБРОС')
        self.assertEqual(bot.attention_messages(CHAT), [])

    def test_every_chunk_of_system_responses_is_excluded_from_model_history(self):
        self.say('/помощь')
        self.assertGreater(len(self.sent()), 1)
        self.assertTrue(all(text.startswith(bot.SYSTEM_MARK) for text in self.sent()))
        self.assertTrue(all(len(text) <= bot.VK_MESSAGE_LIMIT for text in self.sent()))
        prompt = self.ask()
        self.assertNotIn('• терминал:', prompt)
        self.assertNotIn('Команды бота', prompt)

    def test_pins_survive_database_reopen_and_are_not_duplicated(self):
        path = os.path.join(self.tmp.name, 'persistent.db')
        bot.DB.close()
        bot.DB = bot.db_init(path)
        message = dict(from_id=USER, date=START-86400, text='durable-pin')
        self.say('/внимание', fwd_messages=[message, message])
        bot.DB.close()
        bot.DB = bot.db_init(path)
        self.assertEqual(len(bot.attention_messages(CHAT)), 1)
        self.assertIn('durable-pin', self.ask())
        self.say('/забудь')
        self.assertEqual(bot.attention_messages(CHAT), [])

    def test_add_list_persist_append_and_reset(self):
        messages = self.seed(40)
        self.say('/внимание', fwd_messages=messages[:2])
        self.assertEqual(self.llm.calls, [])
        self.say('/внимание')
        self.assertTrue(self.sent()[-1].startswith(bot.SYSTEM_MARK))
        self.assertIn(bot.fmt_ts(messages[0]['date']), self.sent()[-1])
        bot.PEERS.clear()  # durable state, independent from in-memory queue
        self.say('/внимание', fwd_messages=[messages[2]])
        for _ in range(2):
            prompt = self.ask()
            self.assertIn('ВНИМАНИЕ', prompt)
            self.assertIn('interval-0 ', prompt)
            self.assertIn('interval-2 ', prompt)
            self.assertIn('Грань, объясни', prompt)
        self.assertNotIn('interval-0 ', self.ask_after_reset())

    def ask_after_reset(self):
        self.say('/внимание сброс')
        return self.ask()

    def test_attention_supplements_recent_and_interval_and_is_per_peer(self):
        messages = self.seed(40)
        self.say('/внимание', fwd_messages=[messages[0]])
        self.say('recent-unaddressed')
        prompt = self.ask()
        self.assertIn('recent-unaddressed', prompt)
        self.assertIn('interval-0 ', prompt)
        self.say('/контекст', fwd_messages=messages[2:4])
        prompt = self.ask()
        self.assertIn('interval-0 ', prompt)
        self.assertIn('interval-2 ', prompt)
        self.say('Грань, другой чат', peer_id=CHAT+1)
        self.tick(11)
        self.assertNotIn('interval-0 ', self.llm.prompt())

    def test_external_forward_and_images_are_retained(self):
        fwd = dict(from_id=USER, date=START-999, text='external-attention', attachments=[
            {'type':'photo','photo':{'sizes':[{'url':'https://image.example/pin','width':100,'height':100}]}}])
        self.say('/внимание', fwd_messages=[fwd])
        self.ask()
        content = self.llm.calls[-1]['messages'][1]['content']
        self.assertIsInstance(content, list)
        self.assertIn('https://image.example/pin', str(content))
        self.assertIn('external-attention', str(content))


class TestTemporaryCharacters(Issue19Case):
    def test_later_request_grows_active_budget_without_extending_deadline(self):
        self.say('/лимит символов 1000', from_id=ADMIN)
        self.seed(40, 100)
        self.say('/контекст 40')
        original = bot.db_state_get(f'autochars_{CHAT}')
        self.tick(20)
        prompt = self.ask('Грань, вопрос ' + 'q'*10000)
        self.assertIn('q'*10000, prompt)
        self.assertEqual(bot.db_state_get(f'autochars_{CHAT}').split('|')[1], original.split('|')[1])
        self.tick(30)
        self.assertEqual(bot.chars_limit(CHAT), 1000)

    def test_context_growth_expiry_preserves_base_no_refresh_from_reads(self):
        self.say('/лимит символов 1000', from_id=ADMIN)
        self.seed(40, 100)
        self.say('/контекст 40')
        self.assertGreater(bot.chars_limit(CHAT), 1000)
        self.assertEqual(bot.chars_limit(CHAT+1), bot.MAX_CONTEXT_CHARS)
        self.tick(59)
        self.assertGreater(bot.chars_limit(CHAT), 1000)
        self.tick(1)
        self.assertEqual(bot.chars_limit(CHAT), 1000)

    def test_attention_is_complete_after_expiry_without_renewing_budget(self):
        self.say('/лимит символов 1000', from_id=ADMIN)
        self.say('/внимание', fwd_messages=[dict(from_id=USER,date=START,text='PIN-'+'p'*13000)])
        self.assertGreater(bot.chars_limit(CHAT), 1000)
        self.tick(60)
        self.assertEqual(bot.chars_limit(CHAT), 1000)
        self.assertIn('p'*13000, self.ask())
        self.assertEqual(bot.chars_limit(CHAT), 1000)

    def test_small_context_does_not_raise_and_manual_limit_wins(self):
        self.say('/контекст 1')
        self.assertEqual(bot.chars_limit(CHAT), bot.MAX_CONTEXT_CHARS)
        self.seed(40, 1000)
        self.say('/контекст 40')
        self.assertGreater(bot.chars_limit(CHAT), bot.MAX_CONTEXT_CHARS)
        self.say('/лимит символов 2000', from_id=ADMIN)
        self.assertEqual(bot.chars_limit(CHAT), 2000)


class TestReflection(Issue19Case):
    def test_forwarded_reset_admin_check_and_reply_append(self):
        forwarded = dict(from_id=USER, date=START, text='forwarded-memory')
        self.say('/запомни old', from_id=ADMIN)
        self.say('/запомни с нуля', fwd_messages=[forwarded])
        self.assertEqual(self.reflection(), 'old\n')
        self.say('/запомни с нуля', from_id=ADMIN, fwd_messages=[forwarded])
        self.assertIn('forwarded-memory', self.reflection())
        self.assertNotIn('old', self.reflection())
        self.say('/запомни', from_id=ADMIN, reply_message=dict(forwarded, text='reply-memory'))
        self.assertIn('forwarded-memory', self.reflection())
        self.assertIn('reply-memory', self.reflection())

    def reflection(self):
        with open(os.path.join(bot.RAG_DIR, bot.REMEMBER_FILE), encoding='utf-8') as f:
            return f.read()

    def test_append_reset_backup_and_reindex(self):
        self.say('Грань, запомни Истина рождается в споре.', from_id=ADMIN)
        self.say('Грань, запомни Сомнение — начало мудрости.', from_id=ADMIN)
        self.assertEqual(self.reflection(), 'Истина рождается в споре.\nСомнение — начало мудрости.\n')
        self.assertTrue(bot.get_kb().search('истина рождается в споре'))
        self.say('Грань, запомни с нуля Новая память.', from_id=ADMIN)
        self.assertEqual(self.reflection(), 'Новая память.\n')
        self.assertFalse(bot.get_kb().search('истина рождается в споре'))
        with open(os.path.join(bot.RAG_DIR, bot.REMEMBER_FILE)+'.bak', encoding='utf-8') as f:
            self.assertIn('Истина', f.read())

    def test_reset_empty_does_not_erase_and_split_reset_keeps_mode(self):
        self.say('/запомни старое', from_id=ADMIN)
        self.say('/запомни с нуля', from_id=ADMIN)
        self.assertEqual(self.reflection(), 'старое\n')
        self.say('/запомни с нуля '+'z'*3500, from_id=ADMIN)
        self.t += 1
        self.say('конец', from_id=ADMIN)
        self.assertEqual(self.reflection(), 'z'*3500+'\nконец\n')


class TestTerminal(Issue19Case):
    def test_commands_privileged_terminal_only_no_history_pollution(self):
        self.seed()
        before = bot.DB.execute('SELECT count(*) FROM messages').fetchone()[0]
        self.assertIn('Команды бота', self.terminal('/помощь'))
        self.assertIn('2000', self.terminal('/лимит токенов 2000'))
        self.assertIn('Роль обновлена', self.terminal('/роль поэт'))
        self.terminal('/запомни terminal-memory')
        self.assertEqual(bot.tokens_limit(CHAT), 2000)
        self.assertEqual(bot.db_get_role(CHAT), 'поэт')
        self.assertEqual(self.sent(), [])
        self.assertEqual(bot.DB.execute('SELECT count(*) FROM messages').fetchone()[0], before)
        notice = io.StringIO()
        with mock.patch('sys.stdout', notice):
            self.tick(3600)
        self.assertIn('лимит токенов вернулся', notice.getvalue())
        self.assertEqual(self.sent(), [])  # delayed token reset also stays in terminal

    def test_explicit_peer_selection_multiple_peers_and_invalid_commands(self):
        self.seed()
        bot.db_add_message(CHAT+1, USER, False, 'другой чат', START)
        self.assertIn('/чат', self.terminal('/помощь'))
        self.assertIn(str(CHAT+1), self.terminal('/чаты'))
        self.assertIn(str(CHAT+1), self.terminal(f'/чат {CHAT+1}'))
        self.terminal('/контекст 7')
        self.assertEqual(bot.context_size(CHAT+1), 7)
        self.assertEqual(bot.context_size(CHAT), bot.CONTEXT_SIZE)
        self.assertIn('Неизвестная', self.terminal('/неизвестная'))

    def test_deferred_llm_commands_route_to_terminal(self):
        self.seed()
        self.terminal('/пауза 1')
        out = io.StringIO()
        bot.execute_terminal('/пересказ 2', stream=out)
        self.assertEqual(self.llm.calls, [])
        self.tick(61)
        self.assertIn('Пересказ', out.getvalue())
        self.assertEqual(self.sent(), [])

    def test_reader_queues_commands_without_accessing_sqlite(self):
        self.seed()
        thread = threading.Thread(target=bot.terminal_stop_listener, args=(io.StringIO('/роль поэт\n'),))
        thread.start(); thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertNotEqual(bot.db_get_role(CHAT), 'поэт')
        with mock.patch('sys.stdout', new=io.StringIO()):
            bot.process_terminal_commands()
        self.assertEqual(bot.db_get_role(CHAT), 'поэт')
        self.assertEqual(self.sent(), [])


if __name__ == '__main__':
    unittest.main()
