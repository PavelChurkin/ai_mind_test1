"""
Симуляция к отчёту «Архитектура мышления/Отчёт 2026-10-14 …»:
сколько инициативных сообщений бот отправит одному собеседнику за 3 суток
при наивном правиле (как в ai_mind3.py: «вес выше порога — пишем») и при
предложенном фильтре: Рамка (ночь, «не писать») + очередь хода + Лень.

Запуск: python3 experiments/simulate_initiative_gate.py
Модель упрощённая и детерминированная (seed), цифры — порядок величин,
а не предсказание поведения реального бота.
"""
import math
import random

MINUTES = 3 * 24 * 60
THOUGHT_EVERY_MIN = 30          # случайная мысль (ai_filosof) раз в 30 минут
NAIVE_THRESHOLD = 0.7           # «вес мысли выше порога — писать»

# Предложенный фильтр (значения — те же, что в отчёте)
NIGHT = (23, 9)                 # с 23:00 до 9:00 инициативы нет
INITIATIVE_THRESHOLD = 0.5      # желание − Лень ≥ 0.5
LAZY_BASE = 0.2                 # базовый уровень Лени (темперамент)
LAZY_TAU_MIN = 120              # Лень спадает к базе с постоянной 2 часа
COST_INITIATIVE = 0.15          # сколько Лени добавляет своё сообщение
COST_UNANSWERED = 0.20          # … и безответное сообщение (к этому собеседнику)
BACKOFF_BASE_MIN = 24 * 60      # после безответного — пауза 1 сутки, затем 2, 4
MAX_UNANSWERED = 3              # после трёх безответных — не писать, пока не ответит


def is_night(minute):
    hour = (minute // 60) % 24
    start, end = NIGHT
    return hour >= start or hour < end


def simulate(policy, reply_prob, seed=1):
    rnd = random.Random(seed)
    sent = []
    unanswered = 0
    max_unanswered = 0
    lazy = LAZY_BASE
    lazy_contact = 0.0
    last_sent = None
    pending_reply_at = None
    for minute in range(MINUTES):
        # затухание Лени к базовому уровню
        decay = math.exp(-1 / LAZY_TAU_MIN)
        lazy = LAZY_BASE + (lazy - LAZY_BASE) * decay
        # собеседник отвечает
        if pending_reply_at is not None and minute >= pending_reply_at:
            pending_reply_at = None
            unanswered = 0
            lazy_contact = max(0.0, lazy_contact - 0.3)
        if minute % THOUGHT_EVERY_MIN:
            continue
        desire = rnd.random()   # насколько мысль «цепляет» интересы и карточку
        if policy == "naive":
            ok = desire >= NAIVE_THRESHOLD
        else:
            ok = not is_night(minute)
            if ok and unanswered >= MAX_UNANSWERED:
                ok = False
            if ok and unanswered and last_sent is not None:
                ok = minute - last_sent >= BACKOFF_BASE_MIN * 2 ** (unanswered - 1)
            if ok:
                ok = desire - max(lazy, lazy_contact) >= INITIATIVE_THRESHOLD
        if not ok:
            continue
        sent.append(minute)
        last_sent = minute
        unanswered += 1
        max_unanswered = max(max_unanswered, unanswered)
        lazy += COST_INITIATIVE * (1 - lazy)
        if rnd.random() < reply_prob:
            pending_reply_at = minute + rnd.randint(5, 180)
        else:
            lazy_contact += COST_UNANSWERED * (1 - lazy_contact)
    night = sum(1 for m in sent if is_night(m))
    return len(sent), night, max_unanswered


def main():
    print(f"{'собеседник':<28}{'правило':<12}{'сообщений за 3 сут':>20}"
          f"{'ночью':>8}{'макс. подряд без ответа':>26}")
    for title, prob in (("молчит (не отвечает)", 0.0),
                        ("отвечает в 80% случаев", 0.8)):
        for policy in ("naive", "gate"):
            total, night, streak = simulate(policy, prob)
            print(f"{title:<28}{policy:<12}{total:>20}{night:>8}{streak:>26}")


if __name__ == "__main__":
    main()
