# filosof.py — философские размышления для bot5 и bot6 (перенесено из проекта ai_filosof)
#
# Источник: https://github.com/PavelChurkin/ai_filosof (filosof_bot.py, ThoughtGenerator,
# коммит b48af94). Списки слов nouns.json, verbs.json, adject.json скопированы оттуда же
# в эту папку без изменений.
#
# Мысль рождается в 3 шага, как в ai_filosof:
#   0. случайные слова: большая случайная выборка, топ-3 частых и 2 «редких» слова
#      из каждой части речи (без LLM);
#   1. слова → образ и роль;
#   2. образ → вопрос;
#   3. вопрос → ответ (это и есть мысль).
# Каждый шаг — отдельный запрос к LLM; в запрос идёт только результат предыдущего шага.
#
# Модуль не знает ни про ВК, ни про OpenAI: функцию запроса к LLM передаёт бот.
import os
import json
import random
import logging

log = logging.getLogger("gran.filosof")

WORDS_DIR = os.path.dirname(os.path.abspath(__file__))
WORD_FILES = {"nouns": "nouns.json", "verbs": "verbs.json", "adjectives": "adject.json"}

SAMPLE_MIN = 100                   # размер случайной выборки слов (как в ai_filosof)
SAMPLE_MAX = 20000
MAX_ITERATIONS = 20000             # предел перебора в optimized_choice_lst

SYSTEM_PROMPT = "Ты размышляющий бот"
PROMPT_IMAGE = ("Даны следующие слова:\n{words}\n\n"
                "Выбери из них только слова. По этим словам сформируй образ, "
                "не больше 100 слов, с определением своей роли")
PROMPT_QUESTION = "{image}\n\nСформируй вопрос"
PROMPT_ANSWER = "{question}\n\nОтветь на вопрос, не больше 100 слов"


def optimized_choice_lst(lst, max_iterations=MAX_ITERATIONS, rng=random):
    """Случайно выбирает элементы, пока не встретятся все уникальные (или не кончится
    лимит). Возвращает (выборка, до 2 «редких» — так и не выпавших — элементов)."""
    if not lst:
        return [], []
    unique = set(lst)
    chosen, found = [], set()
    for _ in range(max_iterations):
        if len(found) == len(unique):
            break
        c = rng.choice(lst)
        chosen.append(c)
        found.add(c)
    missing = sorted(unique - found)
    return chosen, rng.sample(missing, min(2, len(missing)))


def create_dct(sampled):
    """Топ-3 самых частых слов выборки: [(слово, частота), ...]."""
    freq = {}
    for w in sampled:
        freq[w] = freq.get(w, 0) + 1
    return sorted(freq.items(), key=lambda x: x[1], reverse=True)[:3]


class ThoughtGenerator:
    def __init__(self, words_dir=WORDS_DIR, rng=None):
        self.words_dir = words_dir
        self.rng = rng or random.Random()
        self._words = None

    @property
    def words(self):
        """Словари загружаются при первой мысли: ~2,4 МБ не нужны, пока о мысли не просили."""
        if self._words is None:
            words = {}
            for key, name in WORD_FILES.items():
                with open(os.path.join(self.words_dir, name), encoding="utf-8") as fh:
                    words[key] = json.load(fh)
            log.info("Словари философа: существительных %d, глаголов %d, прилагательных %d",
                     len(words["nouns"]), len(words["verbs"]), len(words["adjectives"]))
            self._words = words
        return self._words

    def random_words(self):
        """Шаг 0: строка со случайными словами — ровно в формате ai_filosof."""
        size = self.rng.randint(SAMPLE_MIN, SAMPLE_MAX)
        parts = {}
        for key in ("nouns", "verbs", "adjectives"):
            pool = self.words[key]
            sample = [self.rng.choice(pool) for _ in range(size)]
            chosen, rare = optimized_choice_lst(sample, rng=self.rng)
            parts[key] = (create_dct(chosen), rare)
        return (f"Существительные: {parts['nouns'][0]} / {parts['nouns'][1]}\n"
                f"Глаголы: {parts['verbs'][0]} / {parts['verbs'][1]}\n"
                f"Прилагательные: {parts['adjectives'][0]} / {parts['adjectives'][1]}")

    def generate(self, ask):
        """
        Генерирует мысль. ask(system, prompt) -> str — запрос к LLM (его даёт бот).
        Возвращает словарь со всеми шагами: words, image, question, answer.
        Если какой-то шаг вернул пустоту, следующие не выполняются (answer = "").
        """
        steps = {"words": self.random_words(), "image": "", "question": "", "answer": ""}
        steps["image"] = ask(SYSTEM_PROMPT, PROMPT_IMAGE.format(words=steps["words"]))
        if steps["image"]:
            steps["question"] = ask(SYSTEM_PROMPT, PROMPT_QUESTION.format(image=steps["image"]))
        if steps["question"]:
            steps["answer"] = ask(SYSTEM_PROMPT, PROMPT_ANSWER.format(question=steps["question"]))
        return steps


def format_thought(steps, detailed=False):
    """Текст для чата: только мысль или (detailed) вся последовательность генерации."""
    if not detailed:
        return f"🧠 Философская мысль:\n\n{steps['answer']}"
    return ("🔍 Полный процесс генерации мысли:\n\n"
            f"📝 Шаг 1 — исходные слова:\n{steps['words']}\n\n"
            f"🎨 Шаг 2 — образ и роль:\n{steps['image']}\n\n"
            f"❓ Шаг 3 — вопрос:\n{steps['question']}\n\n"
            f"💭 Шаг 4 — ответ:\n{steps['answer']}")
