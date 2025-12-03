import asyncio
import json
import os
from openai import AsyncOpenAI
from typing import Dict, List, Optional, Set
from dotenv import load_dotenv
import traceback
import sys
from datetime import datetime

load_dotenv()


class EmotionalMindV3:
    """
    Версия 0.1 - Бот с контекстом переписки и деревом состояний

    Функционал:
    - Ведение истории переписки (контекст)
    - Анализ состояний собеседника по файлу mind1.json
    - Увеличение весов состояний при их обнаружении
    - Сортировка состояний по весам и использование топ-3 в промпте
    - Кратковременная память в analyze.txt
    - Задержка 5 секунд для повторного анализа
    - Спонтанные ответы при достижении весом 1.0
    - Уменьшение весов после ответа
    - Учёт родительских состояний при условиях
    """

    def __init__(self, mind_json_path: str = "mind1.json", memory_file: str = "analyze.txt"):
        self.client = AsyncOpenAI(
            api_key=os.getenv("OPENAI_API_KEY"),
            base_url="https://api.proxyapi.ru/openai/v1"
        )

        # Загружаем дерево состояний из mind1.json
        with open(mind_json_path, 'r', encoding='utf-8') as f:
            mind_data = json.load(f)

        # Исправляем ключ: используем "состояния" вместо "технологии"
        self.states_tree = mind_data["состояния"]

        # Создаём словарь состояний для быстрого доступа
        self.states_dict = {state["название"]: state for state in self.states_tree}

        # Инициализация весов состояний (по умолчанию все 0.0)
        self.state_weights: Dict[str, float] = {
            state["название"]: state.get("вес", 0.0)
            for state in self.states_tree
        }

        # История переписки (контекст)
        self.conversation_history: List[Dict[str, str]] = []

        # Файл кратковременной памяти
        self.memory_file = memory_file

        # Порог для спонтанного ответа
        self.spontaneous_threshold = 1.0

        # Задержка между ответами (секунды)
        self.response_delay = 5

        # Флаг для асинхронной работы
        self.running = True

    def _get_parent_states(self, state_name: str) -> Set[str]:
        """Получить все родительские состояния для данного состояния"""
        parents = set()

        if state_name not in self.states_dict:
            return parents

        conditions = self.states_dict[state_name].get("условия", [])
        for condition in conditions:
            if condition in self.states_dict:
                parents.add(condition)
                # Рекурсивно добавляем родителей родителей
                parents.update(self._get_parent_states(condition))

        return parents

    def increase_state_weight(self, state_name: str, amount: float = 0.2):
        """
        Увеличить вес состояния и его родительских состояний

        Если состояние имеет условия (родителей), увеличиваем веса всех родителей
        """
        if state_name not in self.state_weights:
            return

        # Увеличиваем вес самого состояния
        self.state_weights[state_name] = min(1.0, self.state_weights[state_name] + amount)

        # Получаем родительские состояния и увеличиваем их веса
        parent_states = self._get_parent_states(state_name)
        for parent in parent_states:
            # Родительские состояния увеличиваем на половину amount
            self.state_weights[parent] = min(1.0, self.state_weights[parent] + amount * 0.5)

    def decrease_state_weight(self, state_name: str, amount: float = 0.3):
        """Уменьшить вес состояния"""
        if state_name not in self.state_weights:
            return

        self.state_weights[state_name] = max(0.0, self.state_weights[state_name] - amount)

    def get_top_states(self, n: int = 3) -> List[tuple]:
        """Получить топ-N состояний по весам"""
        sorted_states = sorted(
            self.state_weights.items(),
            key=lambda x: x[1],
            reverse=True
        )
        return [(name, weight) for name, weight in sorted_states[:n] if weight > 0.1]

    def get_triggered_states(self) -> List[str]:
        """Получить состояния с весом >= 1.0 (триггеры для спонтанного ответа)"""
        return [name for name, weight in self.state_weights.items() if weight >= self.spontaneous_threshold]

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

            if json_mode:
                result = json.loads(response.choices[0].message.content)
                print(f'API Response: {result}')
                return result
            else:
                return {"text": response.choices[0].message.content.strip()}

        except Exception as e:
            print(f"Ошибка API: {e}")
            print(f"Тип ошибки: {type(e).__name__}")
            traceback.print_exc()
            return {}

    async def analyze_user_states(self, user_input: str) -> List[str]:
        """
        Анализ состояний собеседника по реплике с учётом всего дерева состояний
        """
        all_states = list(self.states_dict.keys())

        prompt = f"""
        Проанализируй психологическое состояние собеседника по реплике:
        "{user_input}"

        Доступные состояния из дерева mind1.json: {all_states[:50]}... (всего {len(all_states)} состояний)

        Определи какие состояния присутствуют в реплике.

        Верни JSON в формате:
        {{
            "states": ["состояние1", "состояние2", "состояние3"],
            "confidence": 0.8,
            "reasoning": "краткое объяснение"
        }}
        """

        system_prompt = """Ты психологический аналитик.
        Определяй эмоциональные и мыслительные состояния точно и лаконично.
        Выбирай наиболее подходящие состояния из предоставленного списка."""

        result = await self.bam_api_call(prompt, system_prompt)
        detected_states = result.get("states", [])

        print(f"   Обнаружены состояния: {detected_states}")
        print(f"   Уверенность: {result.get('confidence', 0)}")
        print(f"   Обоснование: {result.get('reasoning', '')}")

        return detected_states

    def save_to_memory(self, user_input: str, bot_response: str):
        """Сохранить реплику собеседника и ответ бота в analyze.txt"""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        with open(self.memory_file, 'a', encoding='utf-8') as f:
            f.write(f"\n{'='*60}\n")
            f.write(f"[{timestamp}]\n")
            f.write(f"Пользователь: {user_input}\n")
            f.write(f"Бот: {bot_response}\n")

            # Сохраняем топ-3 состояния
            top_states = self.get_top_states(3)
            f.write(f"Топ-3 состояния: {top_states}\n")
            f.write(f"{'='*60}\n")

    async def delayed_analysis(self, user_input: str, bot_response: str):
        """
        Задержка 5 секунд для повторного анализа весов состояний
        Дополнительный запрос к БЯМ для корректировки весов
        """
        print(f"\n⏱  Задержка {self.response_delay} секунд для повторного анализа...")
        await asyncio.sleep(self.response_delay)

        print("🔄 Повторный анализ весов состояний...")

        # Читаем последние записи из analyze.txt
        context = ""
        try:
            with open(self.memory_file, 'r', encoding='utf-8') as f:
                lines = f.readlines()
                context = "".join(lines[-20:])  # Последние 20 строк
        except FileNotFoundError:
            context = "Нет предыдущего контекста"

        prompt = f"""
        Проанализируй диалог и определи, нужно ли скорректировать веса состояний:

        Последняя реплика пользователя: "{user_input}"
        Ответ бота: "{bot_response}"

        Контекст из памяти:
        {context}

        Текущие топ-3 состояния: {self.get_top_states(3)}

        Определи какие состояния нужно усилить или ослабить.

        Верни JSON в формате:
        {{
            "increase": ["состояние1", "состояние2"],
            "decrease": ["состояние3"],
            "reasoning": "объяснение корректировки"
        }}
        """

        system_prompt = "Ты аналитик состояний. Корректируй веса состояний на основе контекста диалога."

        result = await self.bam_api_call(prompt, system_prompt)

        # Применяем корректировки
        for state in result.get("increase", []):
            self.increase_state_weight(state, 0.15)

        for state in result.get("decrease", []):
            self.decrease_state_weight(state, 0.15)

        print(f"   Корректировка: {result.get('reasoning', '')}")

        # Проверяем триггеры для спонтанного ответа
        triggered = self.get_triggered_states()
        if triggered:
            print(f"\n🔔 Триггер спонтанного ответа! Состояния с весом 1.0: {triggered}")
            await self.generate_spontaneous_response()

    async def generate_spontaneous_response(self):
        """
        Спонтанный ответ при достижении весом 1.0
        Использует топ-3 состояния и последние 2 сообщения
        """
        triggered_states = self.get_triggered_states()
        top_states = self.get_top_states(3)

        # Последние 2 сообщения из истории
        recent_messages = self.conversation_history[-2:] if len(self.conversation_history) >= 2 else self.conversation_history

        context = "\n".join([f"{msg['role']}: {msg['content']}" for msg in recent_messages])

        prompt = f"""
        Сгенерируй спонтанный ответ на основе накопленных состояний:

        Триггерные состояния (вес 1.0): {triggered_states}
        Топ-3 текущих состояния: {top_states}

        Последние сообщения:
        {context}

        Создай естественный спонтанный ответ (1-2 предложения), который отражает эти состояния.
        """

        system_prompt = "Ты чуткий собеседник. Генерируй естественные спонтанные реплики."

        response_data = await self.bam_api_call(prompt, system_prompt, json_mode=False)
        spontaneous_text = response_data.get("text", "...")

        print(f"\n💬 Спонтанный ответ: {spontaneous_text}")

        # Сохраняем в историю
        self.conversation_history.append({
            "role": "бот (спонтанно)",
            "content": spontaneous_text
        })

        # Уменьшаем веса триггерных состояний
        for state in triggered_states:
            self.decrease_state_weight(state, 0.4)

        print(f"   Веса триггерных состояний уменьшены")

    async def generate_response(self, user_input: str) -> str:
        """
        Генерация ответа с учётом топ-3 состояний
        """
        top_states = self.get_top_states(3)
        top_states_str = ", ".join([f"{name} ({weight:.2f})" for name, weight in top_states])

        # Формируем контекст из последних сообщений
        context = ""
        if len(self.conversation_history) > 0:
            recent = self.conversation_history[-5:]  # Последние 5 сообщений
            context = "\n".join([f"{msg['role']}: {msg['content']}" for msg in recent])

        prompt = f"""
        Сгенерируй естественный ответ на реплику: "{user_input}"

        Контекст предыдущих сообщений:
        {context}

        Топ-3 текущих эмоциональных состояния: {top_states_str}

        Создай искренний, эмоционально согласованный ответ (1-2 предложения).
        Учитывай активные состояния в формулировке ответа.

        Верни JSON в формате:
        {{
            "response": "текст ответа"
        }}
        """

        result = await self.bam_api_call(prompt, json_mode=True)
        return result.get("response", "Я понимаю тебя...")

    async def process_message(self, user_input: str) -> Optional[str]:
        """
        Основной метод обработки сообщения

        Шаги:
        1. Добавить сообщение пользователя в историю
        2. Отправить весь контекст + mind1.json для анализа состояний
        3. Увеличить веса обнаруженных состояний (+ родительские)
        4. Подумать перед ответом - отсортировать состояния, взять топ-3
        5. Сгенерировать ответ с топ-3 в промпте
        6. Сохранить в analyze.txt
        7. Запустить delayed_analysis (5 сек)
        """
        print(f"\n{'='*60}")
        print(f"📨 Обработка сообщения: '{user_input}'")
        print(f"{'='*60}")

        # 1. Добавляем в историю
        self.conversation_history.append({
            "role": "пользователь",
            "content": user_input
        })

        # 2. Анализируем состояния собеседника
        print("\n🔍 Шаг 1: Анализ состояний собеседника...")
        detected_states = await self.analyze_user_states(user_input)

        # 3. Увеличиваем веса обнаруженных состояний
        print("\n📈 Шаг 2: Увеличение весов состояний...")
        for state in detected_states:
            if state in self.state_weights:
                print(f"   Увеличиваем вес: {state}")
                self.increase_state_weight(state, 0.2)

        # 4. Подумать перед ответом - получить топ-3
        print("\n🤔 Шаг 3: Сортировка состояний по весам...")
        top_states = self.get_top_states(3)
        print(f"   Топ-3 состояния для промпта: {top_states}")

        # 5. Генерируем ответ
        print("\n✍️  Шаг 4: Генерация ответа...")
        bot_response = await self.generate_response(user_input)
        print(f"   Ответ бота: {bot_response}")

        # Добавляем ответ в историю
        self.conversation_history.append({
            "role": "бот",
            "content": bot_response
        })

        # 6. Сохраняем в analyze.txt
        print("\n💾 Шаг 5: Сохранение в кратковременную память (analyze.txt)...")
        self.save_to_memory(user_input, bot_response)

        # 7. Запускаем отложенный анализ (не блокируем основной поток)
        asyncio.create_task(self.delayed_analysis(user_input, bot_response))

        return bot_response

    def get_status(self) -> dict:
        """Получить текущий статус системы"""
        return {
            "top_3_states": self.get_top_states(3),
            "triggered_states": self.get_triggered_states(),
            "conversation_length": len(self.conversation_history),
            "total_states": len(self.state_weights)
        }


async def main():
    """
    Основной цикл работы бота

    Бот работает асинхронно - всегда ждёт ответ от пользователя.
    Если ответ 0 - программа должна остановиться.
    """
    print("="*60)
    print("🤖 Эмоциональный бот v0.1 запущен")
    print("="*60)
    print("Введите сообщение (или '0' для выхода)")
    print("="*60)

    mind = EmotionalMindV3("mind1.json", "analyze.txt")

    while True:
        try:
            # Асинхронное ожидание ввода пользователя
            user_input = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: input("\n👤 Вы: ").strip()
            )

            # Проверка на команду выхода
            if user_input == "0":
                print("\n👋 Завершение работы...")
                break

            if not user_input:
                continue

            # Обработка сообщения
            response = await mind.process_message(user_input)

            # Вывод ответа
            print(f"\n🤖 Бот: {response}")

            # Статус системы
            status = mind.get_status()
            print(f"\n📊 Статус: {status}")

        except KeyboardInterrupt:
            print("\n\n👋 Завершение работы...")
            break
        except Exception as e:
            print(f"\n❌ Ошибка: {e}")
            traceback.print_exc()


if __name__ == "__main__":
    # Запуск асинхронного main
    asyncio.run(main())
