"""
Эксперимент: Тестирование системы весов и давления на ответ
"""

class WeightSystemTest:
    def __init__(self):
        self.active_nodes = {}
        self.urgency_weights = {
            'Гнев': 0.9,
            'Уточнение': 0.7,
            'Вывод': 0.8,
            'Признание': 0.7,
            'Сомнение': 0.3,
            'О грёзах': 0.1,
            'Тревога': 0.6,
        }

    def calculate_pressure(self) -> float:
        """Расчет давления для ответа"""
        pressure = 0.0
        print("\nРасчет давления:")
        for state, activation in self.active_nodes.items():
            weight = self.urgency_weights.get(state, 0.5)
            contribution = activation * weight
            pressure += contribution
            print(f"  {state}: активация={activation:.2f} * вес={weight:.1f} = {contribution:.2f}")

        pressure = min(pressure, 1.0)
        print(f"\nИТОГО давление: {pressure:.2f}")
        return pressure

    def should_respond(self, threshold: float = 0.7) -> bool:
        """Нужно ли отвечать?"""
        pressure = self.calculate_pressure()
        should = pressure >= threshold
        print(f"\nПорог для ответа: {threshold:.2f}")
        print(f"Отвечать: {'ДА' if should else 'НЕТ'}")
        return should

    def test_scenario(self, states: dict, threshold: float = 0.7):
        """Тест сценария"""
        print(f"\n{'='*60}")
        print(f"ТЕСТ СЦЕНАРИЯ")
        print(f"{'='*60}")
        print(f"Активные состояния: {states}")

        self.active_nodes = states
        result = self.should_respond(threshold)

        return result


if __name__ == "__main__":
    print("Тестирование системы весов и давления\n")

    tester = WeightSystemTest()

    # Тест 1: Высокая активация Гнева
    print("\n" + "="*60)
    print("ТЕСТ 1: Реплика вызвала Гнев (должен быстро ответить)")
    print("="*60)
    tester.test_scenario({"Гнев": 0.8})

    # Тест 2: Сомнение
    print("\n" + "="*60)
    print("ТЕСТ 2: Активировано Сомнение (низкий приоритет, долго обдумывает)")
    print("="*60)
    tester.test_scenario({"Сомнение": 0.9})

    # Тест 3: Комбинация состояний
    print("\n" + "="*60)
    print("ТЕСТ 3: Уточнение + Тревога")
    print("="*60)
    tester.test_scenario({"Уточнение": 0.5, "Тревога": 0.4})

    # Тест 4: Мечтательность
    print("\n" + "="*60)
    print("ТЕСТ 4: О грёзах (очень низкий приоритет)")
    print("="*60)
    tester.test_scenario({"О грёзах": 0.95})

    print("\n✓ Все тесты завершены")
