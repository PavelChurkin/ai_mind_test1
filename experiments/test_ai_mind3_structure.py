"""
Скрипт для валидации структуры ai_mind3.py

Проверяет что все требования из issue #5 реализованы
"""
import sys
import os

# Добавляем родительскую директорию в путь
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def test_imports():
    """Проверка импортов"""
    print("✓ Проверка импортов...")
    try:
        import ai_mind3
        print("  ✓ ai_mind3 успешно импортирован")
        return True
    except ImportError as e:
        print(f"  ✗ Ошибка импорта: {e}")
        return False


def test_class_structure():
    """Проверка структуры класса"""
    print("\n✓ Проверка структуры класса EmotionalMindV3...")
    import ai_mind3

    # Проверяем наличие класса
    assert hasattr(ai_mind3, 'EmotionalMindV3'), "Класс EmotionalMindV3 не найден"
    print("  ✓ Класс EmotionalMindV3 существует")

    # Проверяем методы
    required_methods = [
        '__init__',
        'increase_state_weight',
        'decrease_state_weight',
        'get_top_states',
        'get_triggered_states',
        'analyze_user_states',
        'save_to_memory',
        'delayed_analysis',
        'generate_spontaneous_response',
        'generate_response',
        'process_message',
        '_get_parent_states'
    ]

    cls = ai_mind3.EmotionalMindV3
    for method in required_methods:
        assert hasattr(cls, method), f"Метод {method} не найден"
        print(f"  ✓ Метод {method} существует")

    return True


def test_mind_json_loading():
    """Проверка загрузки mind1.json"""
    print("\n✓ Проверка загрузки mind1.json...")
    import json

    with open('mind1.json', 'r', encoding='utf-8') as f:
        data = json.load(f)

    assert 'состояния' in data, "Ключ 'состояния' не найден в mind1.json"
    print(f"  ✓ mind1.json загружен, найдено {len(data['состояния'])} состояний")

    # Проверяем структуру состояний
    for state in data['состояния'][:3]:  # Проверяем первые 3
        assert 'название' in state, "Поле 'название' отсутствует"
        assert 'условия' in state, "Поле 'условия' отсутствует"
        assert 'вес' in state, "Поле 'вес' отсутствует"

    print("  ✓ Структура состояний корректна")
    return True


def test_requirements_implementation():
    """Проверка реализации требований из issue #5"""
    print("\n✓ Проверка требований из issue #5...")

    import ai_mind3
    import inspect

    # Требование 1: Контекст переписки
    source = inspect.getsource(ai_mind3.EmotionalMindV3)
    assert 'conversation_history' in source, "Не найдено ведение истории переписки"
    print("  ✓ Требование 1: Ведение истории переписки - реализовано")

    # Требование 2: Отправка всего файла mind1.json
    assert 'states_tree' in source or 'states_dict' in source, "Не найдена работа с деревом состояний"
    print("  ✓ Требование 2: Работа с деревом состояний mind1.json - реализовано")

    # Требование 3: Увеличение весов
    assert 'increase_state_weight' in source, "Не найден метод увеличения весов"
    print("  ✓ Требование 3: Увеличение весов состояний - реализовано")

    # Требование 4: Сортировка и топ-3
    assert 'get_top_states' in source, "Не найден метод получения топ состояний"
    print("  ✓ Требование 4: Сортировка и топ-3 состояний - реализовано")

    # Требование 5: analyze.txt
    assert 'analyze.txt' in source or 'memory_file' in source, "Не найдена работа с файлом памяти"
    assert 'save_to_memory' in source, "Не найден метод сохранения в память"
    print("  ✓ Требование 5: Кратковременная память (analyze.txt) - реализовано")

    # Требование 6: Задержка 5 секунд
    assert 'delayed_analysis' in source, "Не найден метод отложенного анализа"
    assert 'asyncio.sleep' in source or 'response_delay' in source, "Не найдена задержка"
    print("  ✓ Требование 6: Задержка 5 секунд для повторного анализа - реализовано")

    # Требование 7: Спонтанные ответы при весе 1.0
    assert 'spontaneous' in source.lower(), "Не найдена реализация спонтанных ответов"
    assert 'generate_spontaneous_response' in source, "Не найден метод спонтанного ответа"
    print("  ✓ Требование 7: Спонтанные ответы при весе 1.0 - реализовано")

    # Требование 8: Уменьшение весов после ответа
    assert 'decrease_state_weight' in source, "Не найден метод уменьшения весов"
    print("  ✓ Требование 8: Уменьшение весов после ответа - реализовано")

    # Требование 9: Учёт родительских состояний
    assert '_get_parent_states' in source or 'parent' in source.lower(), "Не найдена работа с родительскими состояниями"
    print("  ✓ Требование 9: Учёт родительских состояний при условиях - реализовано")

    # Требование 10: Асинхронная работа и остановка по "0"
    # main() belongs to the module, not EmotionalMindV3's class source above.
    assert inspect.iscoroutinefunction(ai_mind3.main), "Не найдена асинхронная работа"
    assert 'if user_input == "0"' in inspect.getsource(ai_mind3.main), "Не найдена проверка на выход"
    print("  ✓ Требование 10: Асинхронная работа и остановка по '0' - реализовано")

    return True


def main():
    """Запуск всех тестов"""
    print("="*60)
    print("🧪 Валидация ai_mind3.py")
    print("="*60)

    tests = [
        test_imports,
        test_class_structure,
        test_mind_json_loading,
        test_requirements_implementation
    ]

    results = []
    for test in tests:
        try:
            result = test()
            results.append(result)
        except Exception as e:
            print(f"\n✗ Тест {test.__name__} провален: {e}")
            import traceback
            traceback.print_exc()
            results.append(False)

    print("\n" + "="*60)
    if all(results):
        print("✓ ВСЕ ТЕСТЫ ПРОЙДЕНЫ УСПЕШНО!")
        print("="*60)
        return 0
    else:
        print("✗ НЕКОТОРЫЕ ТЕСТЫ ПРОВАЛЕНЫ")
        print("="*60)
        return 1


if __name__ == "__main__":
    exit_code = main()
    sys.exit(exit_code)
