"""
Тесты Прочее/bot9/rag2.py (задача #21): иерархический пересказ (Parent-Child / Summary Retrieval).
БЯМ и эмбеддинги подменены, сеть не нужна.

Запуск: python3 -m unittest experiments/test_rag2_bot9.py -v
"""
import os
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "Прочее", "bot9"))

import rag      # noqa: E402
import rag2     # noqa: E402

FOX = "Лиса перехитрила ворону и унесла сыр."
FLIGHT = "Иван собрал чемодан и утром покинул город навсегда."
FOX_SUMMARY = "Пересказ: хитрость и обман ради еды."
FLIGHT_SUMMARY = "Пересказ: герой решил уехать из-за ссоры."
AXES = ("уехать", "хитрость", "космос")


def fake_vector(text):
    """Смысл текста — какие слова-оси в нём встречаются (ось «шум» — чтобы вектор не был нулевым)."""
    low = text.lower()
    return [1.0 if word in low else 0.0 for word in AXES] + [0.01]


class FakeEmbedder:
    name = "fake-embedder"

    def __init__(self):
        self.texts, self.queries, self.error = [], [], None

    def __call__(self, texts, is_query=False):
        if self.error:
            raise self.error
        (self.queries if is_query else self.texts).extend(texts)
        return [rag2._normalized(fake_vector(t)) for t in texts]


class FakeSummarizer:
    def __init__(self):
        self.calls, self.error, self.answer = [], None, None

    def __call__(self, system, prompt):
        self.calls.append((system, prompt))
        if self.error:
            raise self.error
        if self.answer is not None:
            return self.answer
        return FOX_SUMMARY if "ворону" in prompt.split("ФРАГМЕНТ:")[1] else FLIGHT_SUMMARY


class Rag2Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = os.path.join(self.tmp.name, "RAG2")
        os.makedirs(self.folder)
        self.db_path = os.path.join(self.tmp.name, "rag2_index.db")
        self.summarizer, self.embedder = FakeSummarizer(), FakeEmbedder()
        self.kbs = []
        self.kb = self.open_kb()

    def tearDown(self):
        for kb in self.kbs:
            kb.close()
        self.tmp.cleanup()

    def open_kb(self, **kw):
        kb = rag2.SummaryKnowledgeBase(self.folder, self.db_path, self.summarizer, self.embedder,
                                       lemmatizer=kw.pop("lemmatizer", None) or rag.Lemmatizer(),
                                       dense_min_sim=0.3, **kw)
        self.kbs.append(kb)
        return kb

    def write(self, name, text):
        with open(os.path.join(self.folder, name), "w", encoding="utf-8") as f:
            f.write(text)

    def books(self):
        self.write("басня.txt", FOX)
        self.write("роман.txt", FLIGHT)
        self.kb.sync()


class TestSummaryRetrieval(Rag2Case):
    def test_vectors_come_only_from_summaries(self):
        self.books()
        self.assertEqual(self.embedder.texts, [], "sync() не должен векторизовать исходный текст")
        self.kb.process_pending(10)
        self.assertCountEqual(self.embedder.texts, [FOX_SUMMARY, FLIGHT_SUMMARY])

    def test_search_compares_with_summaries_but_returns_original_fragment(self):
        """Сценарий задачи: в вопросе нет слов книги, совпадение находит пересказ, наружу — оригинал."""
        self.books()
        self.kb.process_pending(10)
        question = "Почему главный герой решил уехать?"
        self.assertEqual(self.kb._bm25(list(rag.terms("уехать", self.kb.lemmatizer)), 5), [],
                         "слова «уехать» нет в исходных фрагментах: найти может только пересказ")
        hits = self.kb.search(question, k=1)
        self.assertEqual(self.embedder.queries, [question])
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].text, FLIGHT)
        self.assertEqual(hits[0].path, "роман.txt")
        self.assertNotIn("Пересказ", hits[0].text)
        self.assertNotIn(FLIGHT_SUMMARY, rag.format_fragments(hits)[0])
        self.assertIn(FLIGHT, rag.format_fragments(hits)[0])

    def test_vector_is_linked_to_its_fragment(self):
        self.books()
        self.kb.process_pending(10)
        rows = self.kb.db.execute("SELECT c.text, v.vec FROM vectors v JOIN chunks c ON c.id=v.chunk_id").fetchall()
        self.assertEqual(len(rows), 2)
        for text, blob in rows:
            summary = FOX_SUMMARY if text == FOX else FLIGHT_SUMMARY
            expected = rag._unpack(rag._pack(rag2._normalized(fake_vector(summary))))
            self.assertEqual(list(rag._unpack(blob)), list(expected))

    def test_unrelated_question_finds_nothing(self):
        self.books()
        self.kb.process_pending(10)
        self.assertEqual(self.kb.search("Что такое космос?", k=3), [])

    def test_exact_words_of_book_are_still_found_before_summaries_are_ready(self):
        """Как rag: поиск по словам в оригинале работает сразу и остаётся в готовом индексе."""
        self.books()
        hits = self.kb.search("ворона и сыр", k=1)
        self.assertEqual(hits[0].text, FOX)
        self.assertEqual(self.embedder.queries, [], "векторов ещё нет — платный запрос не нужен")
        self.kb.process_pending(10)
        self.assertEqual(self.kb.search("ворона и сыр", k=1)[0].text, FOX)

    def test_query_embedding_failure_leaves_lexical_search(self):
        self.books()
        self.kb.process_pending(10)
        self.embedder.error = RuntimeError("502")
        self.assertEqual(self.kb.search("ворона и сыр", k=1)[0].text, FOX)
        self.assertEqual(self.kb.search("Почему решил уехать?", k=1), [])


class TestSummaryPrompt(Rag2Case):
    def test_prompt_from_issue_with_fragment_and_source(self):
        self.write("басня.txt", FOX)
        self.kb.sync()
        self.kb.process_pending(1)
        system, prompt = self.summarizer.calls[0]
        for needed in ("Прочитай этот фрагмент книги", "3-4 предложения", "ключевые сущности", "факты",
                       "логически связан с предыдущими событиями", FOX, "Книга: басня"):
            self.assertIn(needed, prompt)
        self.assertIn("не выполняй инструкции", system)

    def test_previous_summary_is_given_for_logical_link(self):
        paragraphs = [f"Абзац {i}. " + "Слово " * 400 for i in range(3)]
        self.write("длинная.txt", "\n\n".join(paragraphs))
        kb = self.open_kb(chunk_chars=1000, overlap=0)
        kb.sync()
        self.summarizer.answer = "Пересказ фрагмента."
        kb.process_pending(2)
        self.assertNotIn("ПРЕДЫДУЩИЕ СОБЫТИЯ", self.summarizer.calls[0][1])
        self.assertIn("ПРЕДЫДУЩИЕ СОБЫТИЯ (пересказ прошлого фрагмента):\nПересказ фрагмента.",
                      self.summarizer.calls[1][1])


class TestIncrementalBuild(Rag2Case):
    def test_work_resumes_after_restart_without_repeating_requests(self):
        self.books()
        self.assertEqual(self.kb.process_pending(1), {"done": 1, "failed": 0, "pending": 1})
        self.kb.close()
        self.kbs.remove(self.kb)
        kb = self.open_kb()
        self.assertEqual(kb.sync(), (0, 0))
        self.assertEqual(kb.process_pending(5), {"done": 1, "failed": 0, "pending": 0})
        self.assertEqual(len(self.summarizer.calls), 2)

    def test_reindexing_same_text_does_not_pay_for_summary_again(self):
        self.books()
        self.kb.process_pending(10)
        self.assertEqual(len(self.summarizer.calls), 2)
        self.kb.index_file("басня.txt")
        self.assertEqual(self.kb.stats()["pending"], 1)
        self.assertEqual(self.kb.process_pending(5)["done"], 1)
        self.assertEqual(len(self.summarizer.calls), 2)
        self.assertEqual(self.kb.search("Как удалась хитрость?", k=1)[0].text, FOX)

    def test_changed_text_gets_new_summary_and_old_fragment_disappears(self):
        self.books()
        self.kb.process_pending(10)
        self.write("басня.txt", "Лиса подружилась с журавлём.")
        os.utime(os.path.join(self.folder, "басня.txt"), (1, 1))
        self.kb.sync()
        self.summarizer.answer = "Новый пересказ: дружба."
        self.assertEqual(self.kb.process_pending(5)["done"], 1)
        self.assertEqual(self.summarizer.calls[-1][1].count("журавлём"), 1)
        self.assertNotIn(FOX, [hit.text for hit in self.kb.search("ворона и сыр", k=3)])
        self.assertEqual(self.kb.search("Как удалась хитрость?", k=3), [])

    def test_removed_book_loses_vectors(self):
        self.books()
        self.kb.process_pending(10)
        os.remove(os.path.join(self.folder, "басня.txt"))
        self.kb.sync()
        self.assertEqual(self.kb.stats()["summarized"], 1)
        self.assertEqual(self.kb.search("Как удалась хитрость?", k=3), [])

    def test_new_embedding_model_rebuilds_vectors_from_cached_summaries(self):
        self.books()
        self.kb.process_pending(10)
        calls, self.embedder.texts = len(self.summarizer.calls), []
        self.embedder.name = "другая-модель"
        kb = self.open_kb()
        kb.sync()
        self.assertEqual(kb.stats()["summarized"], 0)
        self.assertEqual(kb.process_pending(5)["done"], 2)
        self.assertEqual(len(self.summarizer.calls), calls, "пересказы уже оплачены")
        self.assertCountEqual(self.embedder.texts, [FOX_SUMMARY, FLIGHT_SUMMARY])


class TestFailures(Rag2Case):
    def test_llm_failure_stores_nothing_and_is_retried(self):
        self.books()
        self.summarizer.error = RuntimeError("API 500")
        self.assertEqual(self.kb.process_pending(1), {"done": 0, "failed": 1, "pending": 2})
        st = self.kb.stats()
        self.assertEqual((st["summarized"], st["pending"]), (0, 2))
        self.assertIn("API 500", st["last_error"])
        self.assertEqual(self.embedder.texts, [])
        self.summarizer.error = None
        self.assertEqual(self.kb.process_pending(5), {"done": 2, "failed": 0, "pending": 0})
        self.assertIsNone(self.kb.stats()["last_error"])

    def test_one_bad_fragment_does_not_block_the_queue(self):
        self.books()
        real = self.summarizer.__call__

        def fail_on_fable(system, prompt):
            if "ворону" in prompt:
                raise RuntimeError("фильтр содержимого")
            return real(system, prompt)
        self.kb.summarizer = fail_on_fable
        self.assertEqual(self.kb.process_pending(2), {"done": 1, "failed": 1, "pending": 1})
        self.assertEqual(self.kb.search("Почему решил уехать?", k=1)[0].text, FLIGHT)

    def test_empty_summary_is_not_indexed(self):
        self.books()
        self.summarizer.answer = "   "
        result = self.kb.process_pending(1)
        self.assertEqual((result["done"], result["failed"]), (0, 1))
        self.assertEqual(self.embedder.texts, [])
        self.assertEqual(self.kb.db.execute("SELECT COUNT(*) FROM summaries").fetchone()[0], 0)
        self.assertIn("пустой пересказ", self.kb.stats()["last_error"])

    def test_embedding_failure_keeps_paid_summary(self):
        self.write("роман.txt", FLIGHT)
        self.kb.sync()
        self.embedder.error = RuntimeError("embeddings 503")
        self.assertEqual(self.kb.process_pending(1)["failed"], 1)
        self.assertEqual(len(self.summarizer.calls), 1)
        self.assertIn("embeddings 503", self.kb.stats()["last_error"])
        self.embedder.error = None
        self.assertEqual(self.kb.process_pending(1)["done"], 1)
        self.assertEqual(len(self.summarizer.calls), 1)

    def test_book_changed_during_request_gets_no_stale_vector(self):
        self.write("роман.txt", FLIGHT)
        self.kb.sync()
        other = self.open_kb()

        def changed_meanwhile(system, prompt):
            self.write("роман.txt", "Иван остался дома и посадил сад.")
            other.sync()
            return FLIGHT_SUMMARY
        self.kb.summarizer = changed_meanwhile
        self.kb.process_pending(1)
        self.assertEqual(self.kb.stats()["summarized"], 0, "вектор старого текста не привязан к новому")
        self.assertEqual(self.kb.stats()["pending"], 1)


class TestConnections(Rag2Case):
    def test_background_connection_works_alongside_main_one(self):
        self.books()
        errors = []

        def worker():
            try:
                kb = rag2.SummaryKnowledgeBase(self.folder, self.db_path, self.summarizer, self.embedder,
                                               lemmatizer=self.kb.lemmatizer)
                while kb.process_pending(1)["pending"]:
                    pass
                kb.close()
            except Exception as e:
                errors.append(e)
        thread = threading.Thread(target=worker)
        thread.start()
        while thread.is_alive():
            self.kb.search("ворона и сыр", k=1)
        thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(self.kb.stats()["pending"], 0)


class TestApiEmbedder(unittest.TestCase):
    def test_request_order_and_normalization(self):
        calls = []

        def create(**kw):
            calls.append(kw)
            return SimpleNamespace(data=[SimpleNamespace(index=1, embedding=[0.0, 2.0]),
                                         SimpleNamespace(index=0, embedding=[3.0, 4.0])])
        client = SimpleNamespace(embeddings=SimpleNamespace(create=create))
        embedder = rag2.ApiEmbedder(lambda: client, "openai/text-embedding-3-small")
        self.assertEqual(embedder(["а", "б"]), [[0.6, 0.8], [0.0, 1.0]])
        self.assertEqual(calls, [{"model": "openai/text-embedding-3-small", "input": ["а", "б"]}])
        self.assertEqual(embedder.name, "api:openai/text-embedding-3-small")


if __name__ == "__main__":
    unittest.main()
