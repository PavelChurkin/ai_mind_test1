import asyncio
import json
import os
from openai import AsyncOpenAI
from typing import Dict, List, Optional
from dotenv import load_dotenv

import traceback
import sys

load_dotenv()

class EmotionalMind:
    def __init__(self, mind_json_path: str = "mind.json"):
        self.client = AsyncOpenAI(
            api_key=os.getenv("OPENAI_API_KEY"),
            base_url="https://api.proxyapi.ru/openai/v1"
        )

        # Загружаем дерево состояний
        with open(mind_json_path, 'r', encoding='utf-8') as f:
            mind_data = json.load(f)
        self.states_tree = mind_data["технологии"]

        # Инициализация состояния
        self.active_nodes = {}  # {node_name: activation_level (0-1)}
        self.conversation_history = []
        self.short_term_memory = []  # последние 5 состояний
        self.empathy_target = None  # состояния собеседника

        # Триггерные узлы для ответа
        self.trigger_nodes = ['Уточнение', 'Вывод', 'Признание', 'Гнев', 'Сомнение']

        # Карта узлов для быстрого доступа
        self.nodes_map = {node["название"]: node for node in self.states_tree}

    async def bam_api_call(self, prompt: str, system_prompt: str = None, json_mode: bool = True) -> dict:
        """Универсальный метод для вызовов к OpenAI API"""
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        try:
            response = await self.client.chat.completions.create(
                model="gpt-3.5-turbo",  # или "gpt-4"
                messages=messages,
                temperature=0.7,
                response_format={"type": "json_object"} if json_mode else None
            )
            print(f'answer ========= \n{json.loads(response.choices[0].message.content)}')
            return json.loads(response.choices[0].message.content)

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
        root_nodes = [node["название"] for node in self.states_tree if "Пустота" in node.get("условия", [])]

        prompt = f"""
        Проанализируй психологическое состояние собеседника по реплике:
        "{user_input}"

        Доступные состояния: {root_nodes}

        Верни JSON в формате:
        {{
            "states": ["состояние1", "состояние2"],
            "confidence": 0.8,
            "reasoning": "краткое объяснение"
        }}
        """

        system_prompt = "Ты психологический аналитик. Определяй эмоциональные состояния точно и лаконично."

        return await self.bam_api_call(prompt, system_prompt)

    async def activate_empathy_states(self, user_states: List[str]) -> dict:
        """Запрос 2: Активация собственных состояний (эмпатия)"""
        current_active = list(self.active_nodes.keys())

        prompt = f"""
        Текущие мои активные состояния: {current_active}
        Состояния собеседника: {user_states}

        Дерево состояний доступно. Активируй состояния для эмпатии.

        Верни JSON в формате:
        {{
            "activate": ["состояние1", "состояние2"],
            "deactivate": ["состояние3"],
            "empathy_level": 0.8,
            "reasoning": "обоснование выбора"
        }}
        """

        system_prompt = "Ты эмоциональный координатор. Подбирай состояния для искренней эмпатии."

        return await self.bam_api_call(prompt, system_prompt)

    async def evaluate_response_need(self, user_input: str) -> dict:
        """Запрос 3: Оценка необходимости ответа"""
        pressure = self.calculate_pressure()

        prompt = f"""
        Активные состояния: {self.get_high_activation_nodes()}
        Давление ответа: {pressure:.2f}
        Последняя реплика собеседника: "{user_input}"
        История: {self.conversation_history[-2:] if len(self.conversation_history) > 1 else "Нет истории"}

        Нужно ли отвечать сейчас? Оцени срочность и сформулируй темы.

        Верни JSON в формате:
        {{
            "respond": true/false,
            "themes": ["тема1", "тема2"],
            "urgency": 0.7,
            "response_style": "эмпатия/совет/поддержка/вопрос"
        }}
        """

        system_prompt = "Ты диалоговый координатор. Определяй необходимость и стиль ответа."

        return await self.bam_api_call(prompt, system_prompt)

    async def generate_response(self, user_input: str, response_themes: List[str]) -> str:
        """Запрос 4: Генерация ответа"""
        active_states_str = ", ".join(self.get_high_activation_nodes())

        prompt = f"""
        Сгенерируй естественный ответ на реплику: "{user_input}"

        Ключевые темы для ответа: {response_themes}
        Мои активные эмоциональные состояния: {active_states_str}

        Создай искренний, эмоционально согласованный ответ (1-2 предложения).
        Верни только текст ответа без форматирования.
        """

        try:
            response = await self.client.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7
            )
            return response.choices[0].message.content.strip()
        except Exception as e:
            print(f"Ошибка генерации ответа: {e}")
            return "Я понимаю тебя..."

    def calculate_pressure(self) -> float:
        """Расчет давления для ответа"""
        pressure = 0.0
        for node in self.trigger_nodes:
            pressure += self.active_nodes.get(node, 0.0)
        return min(pressure, 1.0)

    def get_high_activation_nodes(self, threshold: float = 0.3) -> List[str]:
        """Получить узлы с высокой активацией"""
        return [node for node, activation in self.active_nodes.items() if activation > threshold]

    def update_activation(self, states_to_activate: List[str], states_to_deactivate: List[str]):
        """Обновление активации состояний"""
        # Уменьшаем активацию для деактивируемых состояний
        for state in states_to_deactivate:
            if state in self.active_nodes:
                self.active_nodes[state] *= 0.3

        # Увеличиваем активацию для новых состояний
        for state in states_to_activate:
            if state in self.active_nodes:
                self.active_nodes[state] = min(1.0, self.active_nodes[state] + 0.4)
            else:
                self.active_nodes[state] = 0.7  # базовая активация

        # Постепенное затухание всех состояний
        for state in list(self.active_nodes.keys()):
            self.active_nodes[state] *= 0.95
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

    async def process_message(self, user_input: str) -> Optional[str]:
        """Основной метод обработки сообщения"""
        print(f"\n=== Обработка: '{user_input}' ===")

        # Запрос 1: Анализ состояния пользователя
        print("1. Анализ состояния пользователя...")
        user_analysis = await self.analyze_user_state(user_input)
        user_states = user_analysis.get("states", [])
        self.empathy_target = user_states
        print(f"   Состояния пользователя: {user_states}")

        # Запрос 2: Активация эмпатических состояний
        print("2. Активация эмпатии...")
        empathy_response = await self.activate_empathy_states(user_states)
        states_to_activate = empathy_response.get("activate", [])
        states_to_deactivate = empathy_response.get("deactivate", [])

        self.update_activation(states_to_activate, states_to_deactivate)
        print(f"   Активированы: {states_to_activate}")
        print(f"   Деактивированы: {states_to_deactivate}")

        # Запрос 3: Оценка необходимости ответа
        print("3. Оценка необходимости ответа...")
        response_decision = await self.evaluate_response_need(user_input)

        should_respond = response_decision.get("respond", False)
        response_themes = response_decision.get("themes", [])

        print(f"   Нужно отвечать: {should_respond}")
        print(f"   Темы ответа: {response_themes}")
        print(f"   Давление: {self.calculate_pressure():.2f}")
        print(f"   Активные узлы: {self.get_high_activation_nodes()}")

        # Сохраняем в историю
        self.conversation_history.append(("user", user_input))

        # Запрос 4: Генерация ответа (если нужно)
        if should_respond and response_themes:
            print("4. Генерация ответа...")
            response = await self.generate_response(user_input, response_themes)

            # Архивируем состояние и сохраняем ответ
            self.archive_current_state()
            self.conversation_history.append(("bot", response))

            # Сбрасываем давление (катарсис)
            for trigger in ['Уточнение', 'Вывод']:
                if trigger in self.active_nodes:
                    self.active_nodes[trigger] *= 0.4

            print(f"   Ответ: {response}")
            return response
        else:
            print("4. Ответ не требуется")
            return None

    def get_status(self) -> dict:
        """Получить текущий статус системы"""
        return {
            "active_nodes": self.get_high_activation_nodes(),
            "pressure": self.calculate_pressure(),
            "empathy_target": self.empathy_target,
            "memory_entries": len(self.short_term_memory),
            "conversation_length": len(self.conversation_history)
        }


# Пример использования
async def main():
    mind = EmotionalMind("mind.json")

    print("Эмоциональный бот запущен. Введите сообщение (или '0' для выхода):")

    while True:
        try:
            user_input = input("\nВы: ").strip()

            if user_input == "0":
                print("Завершение работы...")
                break

            if not user_input:
                continue

            response = await mind.process_message(user_input)
            if response:
                print(f"Бот: {response}")
            status = mind.get_status()
            print(f"\nСтатус: {status}")

        except KeyboardInterrupt:
            print("\nЗавершение работы...")
            break
        except Exception as e:
            print(f"Ошибка: {e}")


if __name__ == "__main__":
    # Запуск асинхронного main
    asyncio.run(main())