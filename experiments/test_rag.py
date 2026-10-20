"""
Тесты для Прочее/bot6/rag.py (база знаний bot6, задача #15): чтение всех форматов, индекс,
поиск по леммам, «ничего не нашлось», промпт и разбор ответа. Без сети и без LLM.

Файлы doc/docx/pdf/epub — в experiments/rag_fixtures (сделаны LibreOffice из одного текста),
txt/fb2/fb2.zip создаются в тесте.

Запуск: python3 -m unittest experiments/test_rag.py -v
"""
import io
import os
import shutil
import contextlib
import zipfile
import tempfile
import unittest
import importlib.util

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "rag_fixtures")
spec = importlib.util.spec_from_file_location("rag", os.path.join(HERE, "..", "Прочее", "bot6", "rag.py"))
rag = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rag)

SOKRAT = ("Глава 1. Сократ\n\nСократ учил, что добродетель есть знание: кто знает, что такое "
          "благо, тот не может поступать дурно. Он говорил: «Я знаю, что ничего не знаю».\n\n"
          "Глава 2. Платон\n\nПлатон, ученик Сократа, создал учение об идеях.\n")

FB2 = """<?xml version="1.0" encoding="utf-8"?>
<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0" xmlns:l="http://www.w3.org/1999/xlink">
 <description><title-info><book-title>Диалоги</book-title></title-info></description>
 <body>
  <title><p>Диалоги</p></title>
  <section><title><p>Глава 1. Сократ</p></title>
   <p>Сократ учил, что добродетель есть знание.</p>
   <p>Он говорил: «Я знаю, что ничего не знаю».</p>
  </section>
  <section><title><p>Глава 2. Платон</p></title>
   <p>Платон, ученик Сократа, создал учение об идеях.</p>
  </section>
 </body>
 <body name="notes"><section><p>Примечание о пещере.</p></section></body>
 <binary id="x.jpg" content-type="image/jpeg">AAAA</binary>
</FictionBook>
"""


class TestReaders(unittest.TestCase):
    """Каждый формат: текст читается, главы становятся метками разделов."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, data):
        path = os.path.join(self.dir, name)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def check_sokrat(self, path, title=None, labels=True):
        got_title, sections = rag.read_book(path)
        text = "\n".join(t for _, t in sections)
        self.assertIn("добродетель есть знание", text, path)
        self.assertIn("Я знаю, что ничего не знаю", text, path)
        self.assertIn("учение об идеях", text, path)
        if title is not None:
            self.assertEqual(got_title, title)
        if labels:
            self.assertEqual([l for l, _ in sections if l.startswith("Глава")],
                             ["Глава 1. Сократ", "Глава 2. Платон"], path)
        return got_title, sections

    def test_txt_encodings(self):
        for enc in ("utf-8", "utf-8-sig", "cp1251", "koi8-r", "utf-16"):
            text = SOKRAT.replace("«", '"').replace("»", '"') if enc == "koi8-r" else SOKRAT  # в koi8-r нет «»
            self.check_sokrat(self.write(f"s_{enc}.txt", text.encode(enc)))

    def test_sentence_is_not_heading(self):
        # строки «Книга про…», «Часть людей…» — текст, а не заголовки: раньше они терялись
        text = "Книга про логику.\nЧасть людей думает иначе, чем Сократ.\nГлава 5\nО дружбе"
        self.assertEqual(rag.split_headings(text),
                         [("", "Книга про логику.\nЧасть людей думает иначе, чем Сократ."), ("Глава 5", "О дружбе")])

    def test_fb2_and_fb2_zip(self):
        title, sections = self.check_sokrat(self.write("d.fb2", FB2.encode("utf-8")), title="Диалоги")
        self.assertIn(("примечания", "Примечание о пещере."), sections)
        self.assertNotIn("AAAA", "".join(t for _, t in sections))     # картинки не текст
        zpath = os.path.join(self.dir, "d.fb2.zip")
        with zipfile.ZipFile(zpath, "w") as z:
            z.writestr("d.fb2", FB2.encode("utf-8"))
        self.check_sokrat(zpath, title="Диалоги")
        self.assertEqual(rag.file_ext(zpath), ".fb2.zip")

    def test_docx(self):
        self.check_sokrat(os.path.join(FIXTURES, "sokrat.docx"))

    def test_doc_russian_and_english(self):
        self.check_sokrat(os.path.join(FIXTURES, "sokrat.doc"))
        _title, sections = rag.read_book(os.path.join(FIXTURES, "socrates_en.doc"))
        self.assertIn("virtue is knowledge", "\n".join(t for _, t in sections).lower())

    def test_pdf_text_layer(self):
        self.check_sokrat(os.path.join(FIXTURES, "sokrat.pdf"), labels=False)

    def test_epub(self):
        self.check_sokrat(os.path.join(FIXTURES, "sokrat.epub"))

    def test_unsupported_and_broken(self):
        with self.assertRaises(ValueError):
            rag.read_book(self.write("a.rtf", b"{\\rtf1}"))
        with self.assertRaises(Exception):
            rag.read_book(self.write("broken.pdf", b"not a pdf"))


class TestChunks(unittest.TestCase):
    def test_chunk_sizes_and_overlap(self):
        sentences = [f"Предложение номер {i} о философии и знании." for i in range(200)]
        chunks = rag.chunk_text(" ".join(sentences), size=500, overlap=100)
        self.assertGreater(len(chunks), 5)
        self.assertTrue(all(len(c) <= 500 for c in chunks))
        for a, b in zip(chunks, chunks[1:]):
            self.assertIn(a.splitlines()[-1], b[:200])          # перекрытие — целые предложения
        joined = " ".join(chunks)
        for s in sentences:
            self.assertIn(s, joined)

    def test_huge_word_cut(self):
        chunks = rag.chunk_text("x" * 3000, size=1000, overlap=0)
        self.assertEqual("".join(chunks), "x" * 3000)


class KbCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.books = os.path.join(self.tmp.name, "RAG")
        os.makedirs(self.books)
        self.index = os.path.join(self.tmp.name, "rag_index.db")
        self.kb = None

    def tearDown(self):
        if self.kb:
            self.kb.close()
        self.tmp.cleanup()

    def put(self, name, data):
        path = os.path.join(self.books, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data if isinstance(data, bytes) else data.encode("utf-8"))
        return path

    def open(self, **kw):
        if self.kb:
            self.kb.close()
        self.kb = rag.KnowledgeBase(self.books, self.index, **kw)
        return self.kb


class TestKnowledgeBase(KbCase):
    def test_all_formats_indexed(self):
        for name in ("sokrat.doc", "sokrat.docx", "sokrat.pdf", "sokrat.epub"):
            shutil.copy(os.path.join(FIXTURES, name), self.books)
        self.put("sokrat.txt", SOKRAT.encode("cp1251"))
        self.put("подпапка/d.fb2", FB2)
        zpath = os.path.join(self.books, "d.fb2.zip")
        with zipfile.ZipFile(zpath, "w") as z:
            z.writestr("d.fb2", FB2.encode("utf-8"))
        self.put("~$temp.docx", b"lock")            # служебный файл Word
        self.put("заметки.md", "не поддерживается")
        kb = self.open()
        self.assertEqual(kb.sync(), (7, 0))
        st = kb.stats()
        self.assertEqual((st["files"], st["ok"], st["errors"]), (7, 7, []))
        paths = {h.path for h in kb.search("добродетель есть знание", k=20)}
        self.assertEqual(paths, {"sokrat.doc", "sokrat.docx", "sokrat.pdf", "sokrat.epub", "sokrat.txt",
                                 "подпапка/d.fb2", "d.fb2.zip"})

    def test_lemmas_find_other_word_forms(self):
        self.put("s.txt", SOKRAT)
        kb = self.open()
        kb.sync()
        self.assertEqual(kb.lemmatizer.name, "pymorphy3")
        for q in ("Чему учил Сократ?", "учения Платона", "что такое добродетели", "кто ученики Сократа"):
            hits = kb.search(q)
            self.assertTrue(hits, q)
        hit = kb.search("учение об идеях")[0]
        self.assertEqual(hit.label, "Глава 2. Платон")
        self.assertEqual(hit.source(), "«s», Глава 2. Платон")

    def test_nothing_found(self):
        self.put("s.txt", SOKRAT)
        kb = self.open()
        kb.sync()
        self.assertEqual(kb.search("квантовая хромодинамика"), [])
        self.assertEqual(kb.search("что это такое?"), [])      # одни служебные слова
        # одно совпавшее слово из трёх — меньше RAG_MIN_COVERAGE, фрагмент не подходит
        self.assertEqual(kb.search("Сократ квантовая хромодинамика"), [])
        self.assertTrue(kb.search("Сократ квантовая"))           # половина слов — подходит

    def test_sync_changes_and_removal(self):
        self.put("a.txt", "Первая книга про логику.")
        kb = self.open()
        self.assertEqual(kb.sync(), (1, 0))
        self.assertEqual(kb.sync(), (0, 0))                   # без изменений — не переиндексирует
        path = self.put("a.txt", "Изменённая книга про этику и мораль.")
        os.utime(path, (1, 1))
        self.assertEqual(kb.sync(), (1, 0))
        self.assertEqual(kb.search("логика"), [])
        self.assertTrue(kb.search("этика"))
        os.remove(path)
        self.assertEqual(kb.sync(), (0, 1))
        self.assertEqual(kb.search("этика"), [])
        self.assertEqual(kb.stats()["files"], 0)

    def test_index_persists_and_rebuilds_on_settings_change(self):
        self.put("a.txt", "Книга про логику.")
        self.open().sync()
        kb = self.open()
        self.assertEqual(kb.sync(), (0, 0))                   # индекс с диска
        self.assertTrue(kb.search("логика"))
        kb = self.open(chunk_chars=500)
        self.assertEqual(kb.sync(), (1, 0))                   # другие настройки — индекс заново

    def test_index_file_and_errors(self):
        kb = self.open()
        self.put("Рефлексия.txt", "Первая версия: память о споре.")
        self.assertEqual(kb.index_file("Рефлексия.txt"), 1)
        self.assertTrue(kb.search("память"))
        self.put("Рефлексия.txt", "Вторая версия: о дружбе.")
        kb.index_file("Рефлексия.txt")
        self.assertEqual(kb.search("память"), [])
        self.assertTrue(kb.search("дружба"))
        self.put("broken.pdf", b"not a pdf")
        self.put("empty.txt", b"   ")
        kb.sync()
        errors = dict(kb.stats()["errors"])
        self.assertIn("broken.pdf", errors)
        self.assertEqual(errors["empty.txt"], "текста не найдено")

    def test_fallback_lemmatizers(self):
        for kind in ("snowball", "lower"):
            lem = rag.Lemmatizer(kind)
            self.assertEqual(lem.name, kind)
        self.put("s.txt", SOKRAT)
        kb = self.open(lemmatizer=rag.Lemmatizer("snowball"))
        kb.sync()
        self.assertTrue(kb.search("учения Платона"))


class FakeEmbedder:
    """Детерминированные «векторы»: есть ли в тексте слова из каждой группы-«смысла»."""
    name = "fake"
    GROUPS = (("мудрость", "мудрец", "знание", "знаю", "добродетель"), ("идея", "идеях", "платон"),
              ("пещера", "тень"))

    def __call__(self, texts, is_query=False):
        out = []
        for t in texts:
            low = t.lower()
            v = [1.0 if any(w in low for w in g) else 0.0 for g in self.GROUPS]
            norm = sum(x * x for x in v) ** 0.5 or 1.0
            out.append([x / norm for x in v])
        return out


class TestDenseSearch(KbCase):
    def test_vectors_find_by_meaning(self):
        self.put("s.txt", SOKRAT)
        kb = self.open(embedder=FakeEmbedder(), dense_min_sim=0.9)
        kb.sync()
        self.assertEqual(kb.stats()["embeddings"], "fake")
        # слов запроса в книге нет, но «смысл» совпадает
        hits = kb.search("мудрость")
        self.assertTrue(hits)
        self.assertIn("добродетель", hits[0].text)
        self.assertEqual(kb.search("тень"), [])               # ни слов, ни смысла


class TestPromptAndAnswer(unittest.TestCase):
    def hits(self):
        return [rag.Hit(1, "a.txt", "Диалоги", "Глава 1", "Сократ учил…", 1.0),
                rag.Hit(2, "b.pdf", "Государство", "", "Платон писал…", 0.5)]

    def test_prompt_is_strict_and_numbered(self):
        system, user = rag.build_prompt("Чему учил Сократ?", self.hits())
        self.assertIn(rag.NO_ANSWER, system)
        self.assertIn("[1] «Диалоги», Глава 1\nСократ учил…", user)
        self.assertIn("[2] «Государство»\nПлатон писал…", user)
        self.assertTrue(user.endswith("ВОПРОС: Чему учил Сократ?"))
        _system, short = rag.build_prompt("q", self.hits(), max_chars=30)
        self.assertNotIn("[2]", short)                         # лишнее не влезло, первый — всегда

    def test_parse_answer(self):
        hits = self.hits()
        self.assertIsNone(rag.parse_answer("НЕТ_ОТВЕТА", hits))
        self.assertIsNone(rag.parse_answer("**НЕТ_ОТВЕТА**", hits))
        self.assertIsNone(rag.parse_answer("", hits))
        self.assertEqual(rag.parse_answer("Добродетель — знание [2].", hits),
                         "Добродетель — знание [2].\n\nИсточники:\n[2] «Государство»")
        self.assertEqual(rag.parse_answer("Так [1, 2], и [7].", hits).split("Источники:\n")[1],
                         "[1] «Диалоги», Глава 1\n[2] «Государство»")
        self.assertTrue(rag.parse_answer("Без ссылок.", hits).endswith(
            "Источники:\n«Диалоги», Глава 1\n«Государство»"))


class TestCli(KbCase):
    def test_main(self):
        self.put("s.txt", SOKRAT)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(rag.main([self.books, "добродетель"]), 0)
            self.assertEqual(rag.main([]), 2)
        self.assertIn("добродетель есть знание", out.getvalue())
        self.assertIn('"chunks": 2', out.getvalue())


if __name__ == "__main__":
    unittest.main()
