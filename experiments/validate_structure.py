"""
Валидация структуры ai_mind3.py без импорта модулей

Проверяет наличие всех требуемых функций из issue #5
"""
import json


def check_file_exists():
    """Проверка существования файла"""
    import os
    assert os.path.exists('ai_mind3.py'), "Файл ai_mind3.py не найден"
    print("✓ Файл ai_mind3.py существует")
    return True


def check_mind_json():
    """Проверка mind1.json"""
    with open('mind1.json', 'r', encoding='utf-8') as f:
        data = json.load(f)

    assert 'состояния' in data, "Ключ 'состояния' не найден"
    states_count = len(data['состояния'])
    print(f"✓ mind1.json: найдено {states_count} состояний")

    # Проверяем наличие родительских связей
    has_parents = False
    for state in data['состояния']:
        if state.get('условия') and len(state['условия']) > 0:
            has_parents = True
            break

    assert has_parents, "Не найдены родительские связи в состояниях"
    print("✓ В дереве состояний есть родительские связи (условия)")
    return True


def check_code_structure():
    """Проверка структуры кода"""
    with open('ai_mind3.py', 'r', encoding='utf-8') as f:
        code = f.read()

    checks = {
        "Класс EmotionalMindV3": "class EmotionalMindV3",
        "Контекст переписки (conversation_history)": "conversation_history",
        "Файл памяти (analyze.txt)": "analyze.txt",
        "Метод увеличения весов": "def increase_state_weight",
        "Метод уменьшения весов": "def decrease_state_weight",
        "Метод получения топ-N": "def get_top_states",
        "Метод получения родительских состояний": "def _get_parent_states",
        "Метод анализа состояний пользователя": "def analyze_user_states",
        "Метод сохранения в память": "def save_to_memory",
        "Метод отложенного анализа": "def delayed_analysis",
        "Метод спонтанного ответа": "def generate_spontaneous_response",
        "Метод получения триггерных состояний": "def get_triggered_states",
        "Асинхронный main": "async def main",
        "Проверка на выход '0'": 'user_input == "0"',
        "Задержка asyncio.sleep": "asyncio.sleep",
        "Порог spontaneous_threshold": "spontaneous_threshold",
        "Работа с весами состояний": "state_weights",
        "Загрузка mind1.json": 'mind1.json',
    }

    results = []
    for description, pattern in checks.items():
        if pattern in code:
            print(f"✓ {description}")
            results.append(True)
        else:
            print(f"✗ {description} - НЕ НАЙДЕНО")
            results.append(False)

    return all(results)


def check_requirements():
    """Проверка списка требований из issue #5"""
    with open('ai_mind3.py', 'r', encoding='utf-8') as f:
        code = f.read()

    print("\n" + "="*60)
    print("ПРОВЕРКА ТРЕБОВАНИЙ ИЗ ISSUE #5")
    print("="*60)

    requirements = [
        ("1. Контекст переписки", "conversation_history"),
        ("2. Отправка всего дерева mind1.json", "states_tree"),
        ("3. Увеличение весов при обнаружении", "increase_state_weight"),
        ("4. Сортировка и топ-3 для промпта", "get_top_states"),
        ("5. Кратковременная память analyze.txt", "save_to_memory"),
        ("6. Задержка 5 секунд", "response_delay"),
        ("7. Спонтанный ответ при весе 1.0", "spontaneous"),
        ("8. Уменьшение весов после ответа", "decrease_state_weight"),
        ("9. Учёт родительских состояний", "_get_parent_states"),
        ("10. Асинхронная работа и выход по '0'", "async def main"),
    ]

    results = []
    for req_desc, pattern in requirements:
        found = pattern.lower() in code.lower()
        status = "✓" if found else "✗"
        print(f"{status} {req_desc}")
        results.append(found)

    return all(results)


def main():
    print("="*60)
    print("🧪 ВАЛИДАЦИЯ AI_MIND3.PY")
    print("="*60)
    print()

    tests = [
        ("Проверка файла", check_file_exists),
        ("Проверка mind1.json", check_mind_json),
        ("Проверка структуры кода", check_code_structure),
        ("Проверка требований", check_requirements),
    ]

    all_passed = True
    for name, test_func in tests:
        print(f"\n📋 {name}:")
        print("-" * 60)
        try:
            result = test_func()
            if not result:
                all_passed = False
        except Exception as e:
            print(f"✗ ОШИБКА: {e}")
            import traceback
            traceback.print_exc()
            all_passed = False

    print("\n" + "="*60)
    if all_passed:
        print("✅ ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ УСПЕШНО!")
        print("="*60)
        return 0
    else:
        print("❌ НЕКОТОРЫЕ ПРОВЕРКИ НЕ ПРОЙДЕНЫ")
        print("="*60)
        return 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
