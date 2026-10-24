"""
Тесты Прочее/bot8/rag.py (задача #17): весь набор experiments/test_rag.py (он же — для bot6)
плюс новое в bot8 — format_fragments и CHAT_RULES для режима «/раг вкл».

Запуск: python3 -m unittest experiments/test_rag_bot8.py -v
"""
import os
import sys
import unittest
import importlib.util

HERE = os.path.dirname(os.path.abspath(__file__))
_prev = os.environ.get("RAG_BOT")
os.environ["RAG_BOT"] = "bot8"
try:
    _spec = importlib.util.spec_from_file_location("test_rag_for_bot8", os.path.join(HERE, "test_rag.py"))
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules[_spec.name] = _mod
    _spec.loader.exec_module(_mod)
finally:
    if _prev is None:
        os.environ.pop("RAG_BOT")
    else:
        os.environ["RAG_BOT"] = _prev

rag = _mod.rag
assert os.path.samefile(rag.__file__, os.path.join(HERE, "..", "Прочее", "bot8", "rag.py"))

# все тесты test_rag.py — для rag.py из bot8
for _name in dir(_mod):
    _obj = getattr(_mod, _name)
    if isinstance(_obj, type) and issubclass(_obj, unittest.TestCase) and _obj.__module__ == _spec.name:
        globals()[_name] = _obj


class TestChatFragments(unittest.TestCase):
    def hits(self):
        return [rag.Hit(1, "a.txt", "Диалоги", "Глава 1", "Сократ учил…", 1.0),
                rag.Hit(2, "b.pdf", "Государство", "", "Платон писал…", 0.5)]

    def test_format_fragments(self):
        text, used = rag.format_fragments(self.hits())
        self.assertEqual(used, 2)
        self.assertEqual(text, "[1] «Диалоги», Глава 1\nСократ учил…\n\n[2] «Государство»\nПлатон писал…")
        text, used = rag.format_fragments(self.hits(), max_chars=30)
        self.assertEqual(used, 1)                                # первый — всегда
        self.assertNotIn("[2]", text)

    def test_build_prompt_uses_same_fragments(self):
        _system, user = rag.build_prompt("q", self.hits())
        self.assertIn(rag.format_fragments(self.hits())[0], user)

    def test_chat_rules_strict(self):
        self.assertIn(rag.NO_ANSWER, rag.CHAT_RULES)
        self.assertIn("Ничего не добавляй от себя", rag.CHAT_RULES)
        self.assertIn("[2]", rag.CHAT_RULES)


if __name__ == "__main__":
    unittest.main()
