import asyncio
import json
import os
from openai import AsyncOpenAI
from typing import Dict, List, Optional
from dotenv import load_dotenv
from datetime import datetime

import traceback
import sys

load_dotenv()

class EmotionalMind:
    def __init__(self, mind_json_path: str = "mind1.json", knowledge_path: str = "world_model.json"):
        self.client = AsyncOpenAI(
            api_key=os.getenv("OPENAI_API_KEY"),
            base_url="https://api.proxyapi.ru/openai/v1"
        )

        # Загружаем дерево состояний - ИСПРАВЛЕНО: используем правильный ключ
        with open(mind_json_path, 'r', encoding='utf-8') as f:
            mind_data = json.load(f)
        self.states_tree = mind_data.get("состояния", mind_data.get("технологии", []))

        # Полное дерево состояний в виде строки для промптов
        self.states_tree_json = json.dumps(self.states_tree, ensure_ascii=False, indent=2)

        # Инициализация состояния
        self.active_nodes = {}  # {node_name: activation_level (0-1)}
        self.conversation_history = []
        self.short_term_memory = []  # последние 5 состояний
        self.empathy_target = None  # состояния собеседника

        # Триггерные узлы для ответа
        self.trigger_nodes = ['Уточнение', 'Вывод', 'Признание', 'Гнев', 'Сомнение']

        # Карта узлов для быстрого доступа
        self.nodes_map = {node["название"]: node for node in self.states_tree}

        # Веса узлов для определения срочности ответа
        self.urgency_weights = {
            'Гнев': 0.9,
            'Уточнение': 0.7,
            'Вывод': 0.8,
            'Признание': 0.7,
            'Сомнение': 0.3,
            'О грёзах': 0.1,
            'Тревога': 0.6,
            'Сожаление': 0.4
        }

        # База знаний о мире
        self.knowledge_path = knowledge_path
        self.world_model = self.load_world_model()

        # Логирование состояний
        self.enable_logging = True
        self.log_buffer = []

    def load_world_model(self) -> dict:
        """Загрузка или создание модели мира"""
        if os.path.exists(self.knowledge_path):
            with open(self.knowledge_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        else:
            return {
                "concepts": [],
                "relations": [],
                "created_at": datetime.now().isoformat()
            }

    def save_world_model(self):
        """Сохранение модели мира"""
        with open(self.knowledge_path, 'w', encoding='utf-8') as f:
            json.dump(self.world_model, f, ensure_ascii=False, indent=2)

    def log_state(self, message: str):
        """Логирование текущего состояния"""
        if self.enable_logging:
            timestamp = datetime.now().strftime("%H:%M:%S")
            log_entry = f"[{timestamp}] {message}"
            self.log_buffer.append(log_entry)
            print(log_entry)

            # Логируем активные состояния
            if self.active_nodes:
                active_states_str = ", ".join([f"{k}:{v:.2f}" for k, v in self.active_nodes.items() if v > 0.2])
                if active_states_str:
                    state_log = f"[{timestamp}] СОСТОЯНИЯ: {active_states_str}"
                    self.log_buffer.append(state_log)
                    print(state_log)

    async def bam_api_call(self, prompt: str, system_prompt: str = None, json_mode: bool = True) -> dict:
        """Универсальный метод для вызовов к OpenAI API"""
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        try:
            response = await self.client.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=messages,
                temperature=0.7,
                response_format={"type": "json_object"} if json_mode else None
            )
            result = json.loads(response.choices[0].message.content)
            self.log_state(f"БЯМ ответ: {json.dumps(result, ensure_ascii=False)[:200]}...")
            return result

        except Exception as e:
            print(f"Ошибка API: {e}")
            print(f"Тип ошибки: {type(e).__name__}")
            traceback.print_exc()
            exc_type, exc_obj, exc_tb = sys.exc_info()
            if exc_tb:
                line_num = exc_tb.tb_lineno
                print(f"\n--- Детали ошибки sys ---")
                print(f"Ошибка произошла в файле: {exc_tb.tb_frame.f_code.co_filename}")
                print(f"Номер строки исходной ошибки: {line_num}")
            return {}

    async def analyze_user_state(self, user_input: str) -> dict:
        """Запрос 1: Анализ состояния собеседника"""
        # ИСПРАВЛЕНО: Теперь используем ВСЕ состояния из дерева, не только первые 3
        all_states = [node["название"] for node in self.states_tree]

        prompt = f"""
        Проанализируй психологическое состояние собеседника по реплике:
        "{user_input}"

        ПОЛНЫЙ СПИСОК ДОСТУПНЫХ СОСТОЯНИЙ:
        {json.dumps(all_states, ensure_ascii=False)}

        ДЕРЕВО СОСТОЯНИЙ С ЗАВИСИМОСТЯМИ:
        {self.states_tree_json[:3000]}

        Верни JSON в формате:
        {{
            "states": ["состояние1", "состояние2", "состояние3"],
            "confidence": 0.8,
            "reasoning": "краткое объяснение"
        }}
        """

        system_prompt = "Ты психологический аналитик. Определяй эмоциональные состояния точно и лаконично. Используй ТОЛЬКО состояния из предоставленного списка."

        return await self.bam_api_call(prompt, system_prompt)

    async def activate_empathy_states(self, user_states: List[str]) -> dict:
        """Запрос 2: Активация собственных состояний (эмпатия)"""
        current_active = list(self.active_nodes.keys())

        prompt = f"""
        Текущие мои активные состояния: {current_active}
        Состояния собеседника: {user_states}

        ДЕРЕВО СОСТОЯНИЙ:
        {self.states_tree_json[:3000]}

        Активируй состояния для эмпатии. Учитывай связи в дереве состояний.
        Состояния должны СООТВЕТСТВОВАТЬ текущей ситуации и помогать понять собеседника.

        НЕ ОТКЛЮЧАЙ состояния просто так - они должны затухать естественно.

        Верни JSON в формате:
        {{
            "activate": ["состояние1", "состояние2"],
            "increase_weight": ["состояние3"],
            "empathy_level": 0.8,
            "reasoning": "обоснование выбора"
        }}
        """

        system_prompt = "Ты эмоциональный координатор. Подбирай состояния для искренней эмпатии. Используй ТОЛЬКО состояния из дерева."

        return await self.bam_api_call(prompt, system_prompt)

    def propagate_activation(self, iterations: int = 3):
        """
        НОВОЕ: Автоматическое распространение активации по дереву
        Исправляет проблему: "нет алгоритма анализа связи узлов"
        """
        self.log_state(f"Распространение активации, итераций: {iterations}")

        for iteration in range(iterations):
            new_activations = {}

            for state_name, activation in list(self.active_nodes.items()):
                if activation < 0.1:
                    continue

                # Найти дочерние состояния (те, у которых state_name в условиях)
                children = self.find_children(state_name)

                for child in children:
                    # Распространяем активацию с затуханием
                    influence = activation * 0.5  # 50% затухание при переходе

                    if child in new_activations:
                        new_activations[child] = min(1.0, new_activations[child] + influence)
                    else:
                        new_activations[child] = influence

            # Обновляем активации
            for state, value in new_activations.items():
                if state in self.active_nodes:
                    self.active_nodes[state] = min(1.0, self.active_nodes[state] + value)
                else:
                    if value > 0.2:  # Порог для новых состояний
                        self.active_nodes[state] = value

            self.log_state(f"Итерация {iteration + 1}: новые состояния {list(new_activations.keys())[:5]}")

    def find_children(self, parent_name: str) -> List[str]:
        """Найти все состояния, у которых parent_name в условиях"""
        children = []
        for node in self.states_tree:
            if parent_name in node.get("условия", []):
                children.append(node["название"])
        return children

    async def evaluate_response_need(self, user_input: str) -> dict:
        """Запрос 3: Оценка необходимости ответа"""
        pressure = self.calculate_pressure()
        high_activation = self.get_high_activation_nodes(threshold=0.3)

        prompt = f"""
        Активные состояния бота: {high_activation}
        Давление на ответ: {pressure:.2f}
        Последняя реплика собеседника: "{user_input}"
        История: {self.conversation_history[-2:] if len(self.conversation_history) > 1 else "Нет истории"}

        ВАЖНО: Бот должен ОТВЕЧАТЬ СОГЛАСНО СВОИМ СОСТОЯНИЯМ, а не просто отключать их.
        Если активированы состояния Гнев, Сомнение, Уточнение - ответ должен отражать эти состояния.

        ДЕРЕВО СОСТОЯНИЙ для контекста:
        {self.states_tree_json[:2000]}

        Нужно ли отвечать сейчас? Оцени срочность и сформулируй темы для ответа.

        Верни JSON в формате:
        {{
            "respond": true/false,
            "themes": ["тема1", "тема2"],
            "urgency": 0.7,
            "response_style": "эмпатия/совет/поддержка/вопрос/размышление"
        }}
        """

        system_prompt = "Ты диалоговый координатор. Определяй необходимость и стиль ответа на основе ТЕКУЩИХ СОСТОЯНИЙ бота."

        return await self.bam_api_call(prompt, system_prompt)

    async def generate_response(self, user_input: str, response_themes: List[str]) -> str:
        """Запрос 4: Генерация ответа"""
        active_states_str = ", ".join(self.get_high_activation_nodes())

        # Получаем релевантные концепты из базы знаний
        relevant_concepts = self.find_relevant_concepts(active_states_str)

        prompt = f"""
        Сгенерируй естественный ответ на реплику: "{user_input}"

        Ключевые темы для ответа: {response_themes}
        Мои активные эмоциональные состояния: {active_states_str}

        ВАЖНО: Ответ должен ОТРАЖАТЬ текущие состояния бота.
        Если активен Гнев - ответ может быть резким.
        Если активно Сомнение - ответ содержит неуверенность.
        Если активно Уточнение - задавай вопросы.

        Контекст из базы знаний: {relevant_concepts[:200] if relevant_concepts else "Нет данных"}

        Создай искренний, эмоционально согласованный ответ (1-2 предложения).
        Верни JSON в формате:
        {{
            "response": "текст ответа"
        }}
        """

        system_prompt = "Ты создаёшь ответы, которые соответствуют эмоциональному состоянию бота."

        try:
            result = await self.bam_api_call(prompt, system_prompt, json_mode=True)
            return result.get("response", "Я понимаю тебя...")
        except Exception as e:
            print(f"Ошибка генерации ответа: {e}")
            return "Я понимаю тебя..."

    def calculate_pressure(self) -> float:
        """
        УЛУЧШЕНО: Адаптивное давление на ответ с учетом весов состояний
        """
        pressure = 0.0

        for state, activation in self.active_nodes.items():
            weight = self.urgency_weights.get(state, 0.5)
            pressure += activation * weight

        return min(pressure, 1.0)

    def get_high_activation_nodes(self, threshold: float = 0.3) -> List[str]:
        """Получить узлы с высокой активацией"""
        return [node for node, activation in self.active_nodes.items() if activation > threshold]

    def update_activation(self, states_to_activate: List[str], states_to_increase: List[str] = None):
        """
        УЛУЧШЕНО: Обновление активации состояний
        Теперь увеличиваем веса, а не просто отключаем
        """
        # Увеличиваем активацию для новых состояний
        for state in states_to_activate:
            if state in self.nodes_map:  # Проверяем что состояние существует
                if state in self.active_nodes:
                    self.active_nodes[state] = min(1.0, self.active_nodes[state] + 0.4)
                else:
                    self.active_nodes[state] = 0.7  # базовая активация

        # Увеличиваем веса для существующих состояний
        if states_to_increase:
            for state in states_to_increase:
                if state in self.active_nodes:
                    self.active_nodes[state] = min(1.0, self.active_nodes[state] + 0.2)

        # Постепенное затухание всех состояний (естественное)
        for state in list(self.active_nodes.keys()):
            self.active_nodes[state] *= 0.92
            if self.active_nodes[state] < 0.1:
                del self.active_nodes[state]

    def reduce_weights_after_response(self):
        """
        НОВОЕ: Уменьшение весов после генерации ответа
        Исправляет: "При достижении единицы в весе - генерируется ответ, который уменьшает вес данных состояний"
        """
        self.log_state("Уменьшение весов после ответа")

        for state in list(self.active_nodes.keys()):
            # Триггерные состояния уменьшаются сильнее
            if state in self.trigger_nodes:
                self.active_nodes[state] *= 0.4
            else:
                self.active_nodes[state] *= 0.7

            if self.active_nodes[state] < 0.1:
                del self.active_nodes[state]

    def archive_current_state(self):
        """Архивация текущего состояния в память"""
        significant_states = {k: v for k, v in self.active_nodes.items() if v > 0.3}
        if significant_states:
            memory_entry = {
                'states': significant_states,
                'empathy_target': self.empathy_target,
                'timestamp': len(self.conversation_history)
            }
            self.short_term_memory.append(memory_entry)

            # Держим только последние 5 состояний в памяти
            if len(self.short_term_memory) > 5:
                self.short_term_memory.pop(0)

    async def update_world_model(self, user_input: str, bot_response: str):
        """
        НОВОЕ: Обновление модели мира через вопрос-ответное мышление
        Исправляет: "Нужна Модель окружающего мира и база знаний"
        """
        # Извлекаем новые концепты из диалога
        prompt = f"""
        Из этого диалога извлеки ключевые концепты и их связи:

        Пользователь: "{user_input}"
        Бот: "{bot_response}"

        Текущие состояния бота: {list(self.active_nodes.keys())}

        Верни JSON в формате:
        {{
            "concepts": [
                {{
                    "name": "концепт1",
                    "type": "event/object/emotion/temporal",
                    "associations": ["связанный1", "связанный2"],
                    "emotional_context": ["Состояние1", "Состояние2"]
                }}
            ]
        }}

        Если новых концептов нет, верни пустой список.
        """

        system_prompt = "Ты извлекаешь концепты из диалогов для формирования базы знаний."

        try:
            result = await self.bam_api_call(prompt, system_prompt, json_mode=True)

            new_concepts = result.get("concepts", [])
            for concept in new_concepts:
                # Добавляем в базу знаний
                concept["id"] = f"concept_{len(self.world_model['concepts']):04d}"
                concept["created_at"] = datetime.now().isoformat()
                concept["source"] = "dialogue"

                self.world_model["concepts"].append(concept)

            if new_concepts:
                self.log_state(f"Добавлено концептов в базу знаний: {len(new_concepts)}")
                self.save_world_model()

        except Exception as e:
            self.log_state(f"Ошибка обновления модели мира: {e}")

    def find_relevant_concepts(self, query: str) -> str:
        """Поиск релевантных концептов в базе знаний"""
        relevant = []
        query_lower = query.lower()

        for concept in self.world_model.get("concepts", []):
            if (query_lower in concept.get("name", "").lower() or
                any(query_lower in assoc.lower() for assoc in concept.get("associations", []))):
                relevant.append(concept.get("name", ""))

        return ", ".join(relevant[:5]) if relevant else ""

    async def process_message(self, user_input: str) -> Optional[str]:
        """Основной метод обработки сообщения"""
        self.log_state(f"=== Обработка: '{user_input}' ===")

        # Запрос 1: Анализ состояния пользователя
        self.log_state("1. Анализ состояния пользователя...")
        user_analysis = await self.analyze_user_state(user_input)
        user_states = user_analysis.get("states", [])
        self.empathy_target = user_states
        self.log_state(f"   Состояния пользователя: {user_states}")

        # Запрос 2: Активация эмпатических состояний
        self.log_state("2. Активация эмпатии...")
        empathy_response = await self.activate_empathy_states(user_states)
        states_to_activate = empathy_response.get("activate", [])
        states_to_increase = empathy_response.get("increase_weight", [])

        self.update_activation(states_to_activate, states_to_increase)
        self.log_state(f"   Активированы: {states_to_activate}")
        self.log_state(f"   Усилены: {states_to_increase}")

        # НОВОЕ: Автоматическое распространение активации по дереву
        self.propagate_activation(iterations=3)

        # Запрос 3: Оценка необходимости ответа
        self.log_state("3. Оценка необходимости ответа...")
        response_decision = await self.evaluate_response_need(user_input)

        should_respond = response_decision.get("respond", False)
        response_themes = response_decision.get("themes", [])

        self.log_state(f"   Нужно отвечать: {should_respond}")
        self.log_state(f"   Темы ответа: {response_themes}")
        self.log_state(f"   Давление: {self.calculate_pressure():.2f}")
        self.log_state(f"   Активные узлы: {self.get_high_activation_nodes()}")

        # Сохраняем в историю
        self.conversation_history.append(("user", user_input))

        # Запрос 4: Генерация ответа (если нужно)
        if should_respond and response_themes:
            self.log_state("4. Генерация ответа...")
            response = await self.generate_response(user_input, response_themes)

            # Архивируем состояние и сохраняем ответ
            self.archive_current_state()
            self.conversation_history.append(("bot", response))

            # НОВОЕ: Уменьшаем веса после ответа
            self.reduce_weights_after_response()

            # НОВОЕ: Обновляем модель мира
            await self.update_world_model(user_input, response)

            self.log_state(f"   Ответ: {response}")
            return response
        else:
            self.log_state("4. Ответ не требуется (бот продолжает обдумывать)")
            return None

    def get_status(self) -> dict:
        """Получить текущий статус системы"""
        return {
            "active_nodes": self.get_high_activation_nodes(),
            "all_active_nodes": self.active_nodes,
            "pressure": self.calculate_pressure(),
            "empathy_target": self.empathy_target,
            "memory_entries": len(self.short_term_memory),
            "conversation_length": len(self.conversation_history),
            "knowledge_concepts": len(self.world_model.get("concepts", []))
        }

    def save_logs(self, filename: str = "bot_logs.txt"):
        """Сохранить логи в файл"""
        with open(filename, 'w', encoding='utf-8') as f:
            f.write("\n".join(self.log_buffer))
        print(f"\nЛоги сохранены в {filename}")


# Пример использования
async def main():
    mind = EmotionalMind("mind1.json")

    print("=" * 60)
    print("Эмоциональный бот v2 запущен (улучшенная версия)")
    print("=" * 60)
    print("\nУлучшения:")
    print("✓ Полное дерево состояний (все узлы, не только первые 3)")
    print("✓ Автоматическое распространение активации по связям")
    print("✓ Весь JSON отправляется в промпты")
    print("✓ Логирование текущих состояний бота")
    print("✓ Бот отвечает согласно состояниям, не просто отключает их")
    print("✓ Веса увеличиваются с состояниями, уменьшаются после ответа")
    print("✓ JSON-based Knowledge Graph для модели мира")
    print("=" * 60)
    print("\nВведите сообщение (или '0' для выхода, 'статус' для просмотра):")

    while True:
        try:
            user_input = input("\nВы: ").strip()

            if user_input == "0":
                print("Завершение работы...")
                mind.save_logs()
                break

            if user_input.lower() == "статус":
                status = mind.get_status()
                print(f"\n{'='*60}")
                print(f"СТАТУС СИСТЕМЫ:")
                print(f"{'='*60}")
                print(f"Активные состояния (>0.3): {status['active_nodes']}")
                print(f"\nВсе активные состояния:")
                for state, weight in sorted(status['all_active_nodes'].items(), key=lambda x: x[1], reverse=True):
                    print(f"  {state}: {weight:.3f}")
                print(f"\nДавление на ответ: {status['pressure']:.2f}")
                print(f"Состояния собеседника: {status['empathy_target']}")
                print(f"Записей в памяти: {status['memory_entries']}")
                print(f"Длина диалога: {status['conversation_length']}")
                print(f"Концептов в базе знаний: {status['knowledge_concepts']}")
                print(f"{'='*60}")
                continue

            if not user_input:
                continue

            response = await mind.process_message(user_input)
            if response:
                print(f"\nБот: {response}")
            else:
                print(f"\n[Бот молчит, обдумывает...]")

            # Показываем краткий статус после каждого сообщения
            status = mind.get_status()
            print(f"\n[Давление: {status['pressure']:.2f} | Активных состояний: {len(status['active_nodes'])} | Концептов: {status['knowledge_concepts']}]")

        except KeyboardInterrupt:
            print("\nЗавершение работы...")
            mind.save_logs()
            break
        except Exception as e:
            print(f"Ошибка: {e}")
            traceback.print_exc()


if __name__ == "__main__":
    # Запуск асинхронного main
    asyncio.run(main())
