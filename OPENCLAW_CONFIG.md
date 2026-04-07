# Варианты настройки OpenClaw

[OpenClaw](https://openclaw.ai/) — бесплатный open-source персональный ИИ-ассистент, который работает локально на вашем устройстве и подключает LLM (Claude, GPT, Gemini и другие) к вашему компьютеру, файлам, браузеру и внешним сервисам. Управление происходит через мессенджеры, которые вы уже используете.

---

## Установка

| Способ | Команда |
|---|---|
| npm (глобальная установка) | `npm i -g openclaw` |
| Bash-скрипт | `curl -fsSL https://openclaw.ai/install.sh \| bash` |
| Из исходников (для разработки) | `git clone` + сборка |
| macOS приложение (меню-бар) | Скачать Beta-версию с сайта |
| Docker / Kubernetes / облако | Azure, GCP, AWS |
| Nix / Podman | Поддерживается |

---

## Первоначальная настройка

```bash
openclaw onboard                       # Интерактивный мастер настройки
openclaw onboard --install-daemon      # Установить как системный сервис
openclaw configure                     # Перенастроить после установки
openclaw configure --section web       # Настроить веб-поиск
openclaw dashboard                     # Открыть панель управления в браузере
openclaw doctor                        # Проверить и исправить конфигурацию
```

---

## Файл конфигурации (`openclaw.json`)

По умолчанию расположен в `~/.openclaw/openclaw.json`. Формат: JSON/JSON5.

### Переопределение через переменные окружения

| Переменная | Описание | По умолчанию |
|---|---|---|
| `OPENCLAW_HOME` | Переопределить домашнюю директорию | — |
| `OPENCLAW_STATE_DIR` | Переопределить директорию состояния | `~/.openclaw` |
| `OPENCLAW_CONFIG_PATH` | Переопределить путь к файлу конфигурации | `~/.openclaw/openclaw.json` |
| `OPENCLAW_LOAD_SHELL_ENV` | Импортировать переменные из login-оболочки (значение `1`) | отключено |
| `OPENCLAW_SHELL_ENV_TIMEOUT_MS` | Таймаут импорта переменных оболочки (мс) | `15000` |
| `OPENCLAW_THEME` | Принудительно задать цветовую схему UI | auto |
| `OPENCLAW_LOG_LEVEL` | Уровень логирования (`debug`, `trace` и др.) | — |
| `OPENROUTER_API_KEY` | API-ключ для OpenRouter | — |
| `GROQ_API_KEY` | API-ключ для Groq | — |
| `VERCEL_GATEWAY_API_KEY` | API-ключ для Vercel Gateway | — |

### Блок `env` (встроенные ключи и переменные)

```json
{
  "env": {
    "ANTHROPIC_API_KEY": "sk-ant-...",
    "OPENAI_API_KEY": "sk-...",
    "MY_VAR": "${ENV_VAR_FROM_SYSTEM}",
    "shellEnv": {
      "enabled": true,
      "timeoutMs": 15000
    }
  }
}
```

### Блок `models.providers` (провайдеры ИИ-моделей)

OpenClaw поддерживает 40+ провайдеров. Примеры настройки API-ключей:

```json
{
  "models": {
    "providers": {
      "anthropic": { "apiKey": "sk-ant-..." },
      "openai":    { "apiKey": "sk-..." },
      "google":    { "apiKey": "AIza..." },
      "ollama":    { "baseUrl": "http://localhost:11434" }
    }
  }
}
```

**Поддерживаемые провайдеры:** Anthropic Claude, OpenAI (GPT-4/5, o1), Google Gemini, xAI Grok, Mistral, DeepSeek, MiniMax, GLM, Perplexity, OpenRouter, Vercel AI Gateway, Groq, Hugging Face, Ollama (локально), LM Studio (локально), а также специализированные провайдеры.

### Блок `tools` (инструменты)

```json
{
  "tools": {
    "profile": "coding"
  }
}
```

| Значение `profile` | Описание |
|---|---|
| `coding` | Профиль для разработки (расширенный набор инструментов) |
| `default` | Стандартный набор инструментов |

### Блок `session` (сессии)

```json
{
  "session": {
    "dmScope": "per-channel-peer"
  }
}
```

| Значение `dmScope` | Описание |
|---|---|
| `per-channel-peer` | Изолировать личные сообщения по каналу и пользователю |
| `global` | Общая сессия для всех |

### Блок `cron` (автоматические задачи)

```json
{
  "cron": {
    "webhookToken": "your-secret-token"
  }
}
```

---

## Настройка каналов (мессенджеров)

### Встроенные каналы

| Канал | Конфигурация |
|---|---|
| **WhatsApp** | `~/.openclaw/credentials/whatsapp/` (JSON аккаунта + QR-спаривание) |
| **Telegram** | Токен через конфиг/env или `channels.telegram.tokenFile` |
| **Discord** | Токен через конфиг/env или SecretRef-провайдеры |
| **Slack** | Блок `channels.slack.*` в конфиге |
| **Signal** | Встроенная поддержка |
| **iMessage** | Через BlueBubbles |
| **Google Chat** | Встроенная поддержка |
| **IRC** | Встроенная поддержка |
| **WebChat** | Браузерный UI (встроен) |

### Плагины каналов (устанавливаются отдельно)

Microsoft Teams, Matrix, Mattermost, LINE, Feishu, Nostr, Nextcloud Talk, Twitch, Zalo и другие.

### Список доступа (allowlist)

Управляется через JSON-файлы для каждого канала. Настраивается в Dashboard.

---

## Настройка шлюза (Gateway)

| Параметр | По умолчанию | Описание |
|---|---|---|
| Порт WebSocket | `18789` | Изменяется в конфигурации |
| Bind-адрес | configurable | Адрес прослушивания |
| Режим аутентификации | Token / Password | Выбирается при настройке |
| Tailscale | отключено | Включить для доступа через Tailscale-сеть |

---

## Настройка веб-поиска

```bash
openclaw configure --section web
```

Доступные провайдеры поиска: Perplexity, Brave, Gemini, Grok, Kimi.

---

## Управление агентами и навыками

```bash
openclaw agents add <name>    # Добавить дополнительного агента
```

| Путь | Описание |
|---|---|
| `~/.openclaw/workspace/` | Директория навыков, промптов и памяти |
| `~/.openclaw/agents/<agentId>/` | Директория конкретного агента |
| `~/.openclaw/agents/<agentId>/agent/auth-profiles.json` | Профили аутентификации агента |
| `~/.openclaw/secrets.json` | Хранилище секретов (опционально) |

- Навыки можно создавать прямо в чате.
- Изменения в промптах применяются без перезапуска (hot-reload).
- Маркетплейс сообщества: [clawhub.ai](https://clawhub.ai).

---

## Панель управления (Dashboard)

Запускается командой `openclaw dashboard` или через macOS-приложение.

| Раздел | Что настраивается |
|---|---|
| **Язык** | `en`, `zh-CN`, `zh-TW`, `pt-BR`, `de`, `es` |
| **Сессии** | Список и управление активными сессиями; переопределение модели, режима мышления, verbose |
| **Каналы** | Мониторинг и конфигурация WhatsApp, Telegram, Discord, Slack; QR-вход для плагинов |
| **Cron-задачи** | Создание/редактирование/запуск/включение; режимы доставки (summary, webhook, none); модель и мышление для каждой задачи |
| **Навыки** | Включение/отключение, установка новых, обновление API-ключей |
| **Системная конфигурация** | Просмотр и редактирование `openclaw.json` в браузере с валидацией |
| **Расширенные** | Разрешения exec, allowlist, лог шлюза в реальном времени, ручные RPC-вызовы |

---

## CLI — полезные флаги

| Флаг / Команда | Описание |
|---|---|
| `--non-interactive` | Режим автоматизации / скриптов |
| `--json` | Вывод в формате JSON |
| `--reset` | Сбросить конфиг, учётные данные и сессии |
| `--reset-scope full` | Включить workspace в сброс |
| `--workspace <путь>` | Указать кастомную директорию workspace |
| `--model <модель>` | Указать модель по умолчанию |
| `--agent-dir <путь>` | Кастомная директория агентов |
| `--bind` | Настройка маршрутизации сообщений |
| `--gateway-token-ref-env <ENV>` | Ссылка на токен для неинтерактивной настройки |
| `--secret-input-mode ref` | Учётные данные через переменные окружения |

---

## Поддерживаемые интеграции

| Категория | Сервисы |
|---|---|
| Продуктивность | Apple Notes, Apple Reminders, Things 3, Notion, Obsidian, Bear Notes, Trello, GitHub |
| Музыка | Spotify, Sonos, Shazam |
| Умный дом | Philips Hue, 8Sleep, Home Assistant |
| Автоматизация | Управление браузером, cron-задачи, вебхуки, Gmail (+ PubSub), 1Password |
| Социальные | Twitter/X, Email |
| Медиа / Творчество | Генерация изображений, поиск GIF, захват экрана (Peekaboo), камера |

---

## Поддерживаемые платформы

macOS, Windows (рекомендуется WSL2), Linux, Raspberry Pi, iOS, Android.

Развёртывание на сервере: Docker, Kubernetes, Azure, GCP, AWS, systemd user service.

---

## Ссылки

- [Официальный сайт OpenClaw](https://openclaw.ai/)
- [Документация](https://docs.openclaw.ai/getting-started)
- [Маркетплейс навыков — ClaWHub](https://clawhub.ai)
