"""
Эксперимент: Тестирование распространения активации по дереву состояний
"""
import sys
import json

# Имитируем структуру дерева состояний
test_tree = [
    {"название": "Пустота", "условия": [], "вес": 0.0},
    {"название": "Тревога", "условия": ["Пустота"], "вес": 0.0},
    {"название": "Гнев", "условия": ["Тревога"], "вес": 0.0},
    {"название": "Объективизация", "условия": ["Гнев"], "вес": 0.0},
    {"название": "Сомнение", "условия": ["Тревога"], "вес": 0.0},
]

class StateTreeTest:
    def __init__(self, tree):
        self.states_tree = tree
        self.active_nodes = {}
        self.nodes_map = {node["название"]: node for node in self.states_tree}

    def find_children(self, parent_name: str):
        """Найти все состояния, у которых parent_name в условиях"""
        children = []
        for node in self.states_tree:
            if parent_name in node.get("условия", []):
                children.append(node["название"])
        return children

    def propagate_activation(self, iterations: int = 3):
        """Распространение активации по дереву"""
        print(f"\n{'='*60}")
        print(f"ТЕСТ РАСПРОСТРАНЕНИЯ АКТИВАЦИИ")
        print(f"{'='*60}")

        for iteration in range(iterations):
            print(f"\nИтерация {iteration + 1}:")
            print(f"Состояние перед итерацией: {self.active_nodes}")

            new_activations = {}

            for state_name, activation in list(self.active_nodes.items()):
                if activation < 0.1:
                    continue

                children = self.find_children(state_name)
                print(f"  {state_name} ({activation:.2f}) -> дети: {children}")

                for child in children:
                    influence = activation * 0.5
                    if child in new_activations:
                        new_activations[child] = min(1.0, new_activations[child] + influence)
                    else:
                        new_activations[child] = influence

            # Обновляем активации
            for state, value in new_activations.items():
                if state in self.active_nodes:
                    self.active_nodes[state] = min(1.0, self.active_nodes[state] + value)
                else:
                    if value > 0.2:
                        self.active_nodes[state] = value

            print(f"  Новые активации: {new_activations}")
            print(f"  Состояние после итерации: {self.active_nodes}")

    def test_scenario(self, initial_states: dict):
        """Тест сценария"""
        print(f"\n{'='*60}")
        print(f"СЦЕНАРИЙ ТЕСТА")
        print(f"{'='*60}")
        print(f"Начальные состояния: {initial_states}")

        self.active_nodes = initial_states.copy()
        self.propagate_activation(iterations=3)

        print(f"\n{'='*60}")
        print(f"ИТОГОВЫЙ РЕЗУЛЬТАТ")
        print(f"{'='*60}")
        for state, activation in sorted(self.active_nodes.items(), key=lambda x: x[1], reverse=True):
            print(f"  {state}: {activation:.3f}")


if __name__ == "__main__":
    print("Тестирование алгоритма распространения активации\n")

    tester = StateTreeTest(test_tree)

    # Тест 1: Активация Тревоги
    print("\n" + "="*60)
    print("ТЕСТ 1: Реплика 'Ты опять ошибся' -> активирует Тревогу")
    print("="*60)
    tester.test_scenario({"Тревога": 0.8})

    # Тест 2: Активация нескольких состояний
    tester2 = StateTreeTest(test_tree)
    print("\n" + "="*60)
    print("ТЕСТ 2: Множественная активация")
    print("="*60)
    tester2.test_scenario({"Тревога": 0.7, "Гнев": 0.6})

    print("\n✓ Все тесты завершены")
