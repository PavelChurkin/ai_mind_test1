# bot8.py — бот «Грань» для чата сообщества ВКонтакте (следующая версия bot6, задача #17)
#
# Что изменилось по сравнению с bot6 (подробно — в README.md этой папки):
#   * база знаний — режим беседы «/раг вкл» / «/раг выкл»: во включённом режиме перед каждым
#     ответом — поиск по базе знаний, ответ строго по найденным фрагментам; не нашлось —
#     ответ начинается с «Поиск по базе знаний не дал результата.»; «РАГ+» убран;
#     «/лимит чанков N» — сколько фрагментов передавать модели;
#   * «/рассуждения вкл» / «/рассуждения выкл»: рассуждение модели включено и показывается
#     в начале ответа между маркерами #рассуждения# (по умолчанию выключено);
#   * /мысль — без повтора и без увеличения лимита токенов; в промптах философа вместо
#     «не больше 100 слов» — «пиши кратко»;
#   * размер контекста: «/контекст N», «/автоконтекст» (по пересланному сообщению или
#     reply), «/автоконтекст 2 ч», «/сброс контекста [N]»; рост контекста в споре убран;
#   * остановка: «0» + Enter в терминале; «Грань, стоп машина» — только админ;
#   * ansible/ — установка бота на чистый Linux-сервер.
import os
import re
import ssl
import sys
import json
import math
import time
import base64
import signal
import threading
import sqlite3
import argparse
import functools
import logging
import logging.handlers
import urllib.error
import urllib.request
from collections import OrderedDict
from datetime import datetime, timedelta

BOT_DIR = os.path.dirname(os.path.abspath(__file__))
if BOT_DIR not in sys.path:
    sys.path.insert(0, BOT_DIR)
import filosof  # noqa: E402  (лежит рядом с ботом)
import rag      # noqa: E402

# ============ НАСТРОЙКИ ============
BOT_NAME = "Грань разумного"
TRIGGER_RE = re.compile(r"\bгрань\b", re.IGNORECASE)
STOP_PHRASE = "грань, стоп машина"   # остановка из чата — только админ (ADMIN_IDS или админ беседы)
TERMINAL_STOP = True               # «0» + Enter в терминале, где запущен бот, — остановить бота

# --- личные сообщения и права ---
ALLOW_PRIVATE_MESSAGES = False     # можно ли боту отвечать в личных сообщениях (по умолчанию — нет)
ADMIN_CACHE_MINUTES = 10           # сколько минут помнить список админов беседы
# ADMIN_IDS в .env — id пользователей, которые админы бота в любой беседе (через запятую)

# --- контекст ---
CONTEXT_SIZE = 30                  # стандартный размер контекста (последних сообщений)
CONTEXT_SIZE_MAX = 500             # больше этого /контекст и /автоконтекст не поставят
CONTEXT_MIN_MESSAGES = 10          # столько последних сообщений берём всегда, даже если они старые
CONTEXT_MEMORY_HOURS = 12          # более старые сообщения — только в пределах CONTEXT_MIN_MESSAGES (0 — без ограничения);
                                   # при размере, заданном /контекст или /автоконтекст, не действует
MAX_CONTEXT_CHARS = 12000          # стандартный лимит символов истории в запросе (меняется /лимит символов)
MAX_CONTEXT_CHARS_CAP = 300000     # больше этого /лимит символов не поставит
CONTEXT_SUMMARY_CHARS = 2000       # размер сжатого пересказа старой части истории
CONTEXT_KEEP_SHARE = 0.6           # после сжатия свежая часть занимает не больше этой доли остатка

# --- ответы ---
MAX_TOKENS_DEFAULT = 800           # стандартный лимит выходных токенов (меняется /лимит токенов)
MAX_TOKENS_CAP = 64000             # больше этого /лимит токенов не поставит
TOKEN_LIMIT_RESET_MINUTES = 60     # через столько минут /лимит токенов сам вернётся к стандарту (0 — не вернётся)
EMPTY_ANSWER_RETRY_TOKENS = 3000   # пустой ответ из-за лимита (всё ушло на рассуждение) — один повтор с таким лимитом
                                   # (0 — без повтора); у /мысль повтора нет
LLM_EXTRA_BODY = None              # доп. параметры каждого запроса к модели, например {"top_k": 20}

# --- рассуждение модели (/рассуждения вкл|выкл) ---
REASONING_DEFAULT = False          # рассуждения в новой беседе: False — выключены
REASONING_ON_BODY = {"thinking": {"type": "enabled"}}    # что добавить к запросу, когда рассуждения включены
REASONING_OFF_BODY = {"thinking": {"type": "disabled"}}  # …и когда выключены (None — ничего: решает модель)
REASONING_MARK = "#рассуждения#"   # рассуждение в начале ответа — между двумя такими строками
REASONING_MAX_CHARS = 6000         # длиннее — рассуждение показывается с сокращением в середине
BATCH_WAIT_SEC = 8                 # ждём столько секунд без новых обращений и отвечаем на все разом
BATCH_MAX_WAIT_SEC = 60            # но не дольше этого с первого обращения
PAUSE_DEFAULT_MINUTES = 1          # /пауза без числа
PAUSE_MAX_MINUTES = 60
FRESHNESS_MINUTES = 10             # если обращение старше — не отвечаем

# --- длинные сообщения, которые ВК разбил на части ---
SPLIT_MIN_PART_CHARS = 3000        # часть такой длины (и длиннее) может продолжиться следующим сообщением
SPLIT_JOIN_SEC = 10                # следующая часть приходит не позже стольких секунд (по дате сообщений)
SPLIT_MAX_PARTS = 20               # больше частей в одно обращение не склеиваем
ROLE_JOIN_SEC = 15                 # /роль без закрывающей скобки ждёт продолжения столько секунд

# --- роль ---
ROLE_FILE = "Роль.txt"             # роль по умолчанию — файл рядом с ботом (нет файла — DEFAULT_ROLE)

# --- база знаний (RAG) ---
RAG_DIR = os.path.join(BOT_DIR, "RAG")             # книги: txt, doc, docx, pdf, epub, fb2
RAG_INDEX_PATH = os.path.join(BOT_DIR, "rag_index.db")
RAG_TOP_K = 6                      # сколько найденных фрагментов передавать модели (меняется /лимит чанков)
RAG_TOP_K_MAX = 30                 # больше этого /лимит чанков не поставит
RAG_MIN_COVERAGE = 0.5             # фрагмент подходит, если в нём есть эта доля значимых слов запроса
RAG_MAX_CONTEXT_CHARS = 40000      # предохранитель: больше стольких символов фрагментов модели не уходит
RAG_MAX_TOKENS = 1500              # лимит токенов ответа по базе знаний (берётся больший из этого и /лимит токенов)
RAG_EMBEDDINGS = False             # True — ещё и векторный поиск (pip install sentence-transformers)
RAG_EMBEDDING_MODEL = "intfloat/multilingual-e5-small"
RAG_NOTHING_FOUND = "В базе знаний ничего не нашлось."                 # «/раг вопрос» без находок
RAG_MODE_DEFAULT = False           # режим «/раг вкл» в новой беседе
RAG_NO_RESULT_PREFIX = "Поиск по базе знаний не дал результата."     # режим «/раг вкл»: начало ответа без находок
REMEMBER_FILE = "Рефлексия.txt"    # «Грань, запомни» перезаписывает этот файл в RAG_DIR
REMEMBER_ADMIN_ONLY = True         # «Грань, запомни» — только админ беседы (False — все)

# --- дневной лимит ---
DAILY_MESSAGE_LIMIT = 100          # ответов бота в сутки (0 — без лимита)
DAILY_LIMIT_PER_CHAT = True        # True — свой лимит у каждой беседы, False — один на все беседы
LIMIT_NOTIFY_PERCENTS = (25, 50, 90, 100)
LIMIT_RESET_HOUR = 0               # в этот час (по времени сервера) начинаются новые сутки

# --- пересказ ---
SUMMARY_MAX_INPUT = 5000           # больше сообщений не пересказываем
SUMMARY_FREE_MAX = 100             # пересказ больше стольких сообщений — только админ
SUMMARY_DEFAULT_COUNT = 100        # /пересказ без числа
SUMMARY_MAX_TOKENS = 1500
HISTORY_LOAD_MAX = 20000           # сколько сообщений максимум догружать из ВК для пересказа с даты

# --- история и вложения ---
INITIAL_LOAD = 1000                # сколько сообщений загрузить при первом старте
SYNC_ON_START = 200                # при каждом следующем старте — догрузить пропущенное
FWD_MAX_DEPTH = 4                  # глубина вложенности пересланных сообщений
QUOTE_MAX_CHARS = 300              # цитата сообщения, на которое ответили reply (0 — целиком)

MAX_IMAGES_PER_REQUEST = 4         # картинок в одном запросе к LLM
IMAGE_MODE = "base64"              # "base64" — скачать и передать данными, "url" — передать ссылку
IMAGE_MAX_BYTES = 5 * 1024 * 1024
IMAGE_MAX_SIDE = 1280              # предпочитаем размер фото не больше этой стороны
IMAGE_DOWNLOAD_TIMEOUT = 15
IMAGE_CA_FILE = None               # доп. корневой сертификат (.pem/.crt) для скачивания картинок, путь от папки бота
IMAGE_SSL_INSECURE_FALLBACK = False  # True — если сертификат не проверился, скачать без проверки (небезопасно)
IMAGE_URL_FALLBACK = False         # True — не скачалась картинка в base64-режиме — передать модели ссылку (как в bot5)
IMAGE_CACHE_SIZE = 16              # столько последних скачанных картинок держать в памяти

LLM_MIN_INTERVAL_SEC = 3           # не чаще одного запроса к LLM за столько секунд
VK_MESSAGE_LIMIT = 4000            # ВК режет сообщения длиннее 4096 символов
LONGPOLL_WAIT_SEC = 1              # Long Poll возвращается не реже — чтобы вовремя отвечать пакетам

DB_PATH = os.path.join(BOT_DIR, "gran7.db")
OLD_DB_NAME = "gran6.db"           # БД bot6: ищется рядом с ботом, в текущей папке и в ../bot6
OLD_DB_PATH = None                 # явный путь к БД bot6 (если задан — ищется только он)

# --- лог и перезапуск ---
LOG_MAX_MB = 50                    # файл лога (--log-file) больше этого — ротация
LOG_BACKUPS = 1                    # сколько старых файлов лога хранить
RESTART_EXIT_CODE = 75             # с этим кодом бот выходит для планового перезапуска (--restart-after-hours)

PROXYAPI_BASE_URL = "https://api.proxyapi.ru/v1"
MODEL_NAME = "deepseek/deepseek-v4-flash-vision-exp"
# ===================================

CHAT_PEER_OFFSET = 2000000000      # peer_id бесед начинается с 2e9
SYSTEM_MARK = "⚙️"                 # служебные сообщения бота; в контекст LLM не попадают

log = logging.getLogger("gran")
DEBUG_MODE = False                 # --debug: подробный лог основных функций (setup_logging)

DEFAULT_ROLE = (
    "Ты — Грань, участник беседы. Общайся живо, по-человечески, "
    "не будь слишком формальной, подстраивайся под тон беседы."
)
LIMIT_REACHED_TEXT = "Извините, сегодня лимит сообщений достигнут."

STOP_REQUESTED = False
EXIT_CODE = 0
LAST_LLM_ERROR = ""                # почему последний запрос к LLM не дал ответа (для сообщения в чат)
LLM_REASONINGS = []                # рассуждения модели за текущий ответ (может быть несколько запросов)
REASONING_ON = False               # рассуждения включены для текущего ответа (ставит with_reasoning)
REASONING_PARAM_OK = True          # False — API отверг REASONING_*_BODY, больше не передаём

# Заполняются в init() — так модуль можно импортировать в тестах без токенов и сети.
DB = None
vk = None
llm_client = None
BOT_GROUP_ID = 0                   # отрицательный id сообщества (from_id сообщений бота)
VK_USER_TOKEN = None               # необязательный ключ пользователя/сервисный — для wall.getById
ADMIN_IDS = set()
_last_llm_call = 0.0
_last_limit_check = 0.0
_admin_cache = {}                  # peer_id -> (когда загружен, {id админов})
PEERS = {}                         # peer_id -> PeerState: очередь обращений, пауза, недописанные команды
FILOSOF = filosof.ThoughtGenerator()
KB = None                          # база знаний rag.KnowledgeBase (get_kb)
_image_cache = OrderedDict()       # url -> часть запроса с картинкой
_ssl_contexts = None
_role_file_cache = (None, None)    # (mtime, текст) файла роли


def now():
    """Текущее время; тесты подменяют эту функцию."""
    return time.time()


# ============ Лог ============
NOISY_LOGGERS = ("httpx", "httpx2", "httpcore", "openai", "urllib3", "pymorphy3")


def setup_logging(debug=False, log_file=None, console=True):
    """Обычный режим — INFO без строк HTTP-библиотек; debug — подробный лог основных функций
    (вход, выход, время, промпты и ответы модели). log_file — ещё и в файл (с ротацией)."""
    global DEBUG_MODE
    DEBUG_MODE = bool(debug)
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(funcName)s: %(message)s" if debug
                            else "%(asctime)s %(levelname)s %(message)s")
    handlers = []
    if console:
        stream = sys.stderr
        try:   # консоль Windows (cp866/cp1251) не умеет эмодзи — не падаем на них
            stream.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError):
            pass
        handlers.append(logging.StreamHandler(stream))
    if log_file:
        os.makedirs(os.path.dirname(os.path.abspath(log_file)), exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(
            log_file, maxBytes=LOG_MAX_MB * 1024 * 1024, backupCount=LOG_BACKUPS, encoding="utf-8"))
    for h in handlers:
        h.setFormatter(fmt)
        root.addHandler(h)
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    if debug:
        logging.getLogger("httpx").setLevel(logging.INFO)     # строка на каждый HTTP-запрос
        logging.getLogger("httpx2").setLevel(logging.INFO)


def brief(value, limit=200):
    """Короткое представление значения для подробного лога."""
    if isinstance(value, dict) and "peer_id" in value:      # сообщение ВК
        value = {k: value.get(k) for k in ("peer_id", "from_id", "conversation_message_id", "text")}
    text = repr(value)
    return text if len(text) <= limit else text[:limit] + f"…(+{len(text) - limit})"


def traced(fn):
    """В режиме --debug пишет в лог вход в функцию (аргументы), выход (результат) и время."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if not DEBUG_MODE:
            return fn(*args, **kwargs)
        shown = ", ".join([brief(a, 120) for a in args] + [f"{k}={brief(v, 120)}" for k, v in kwargs.items()])
        log.debug(">> %s(%s)", fn.__name__, shown)
        t0 = time.time()
        try:
            result = fn(*args, **kwargs)
        except Exception as e:
            log.debug("<< %s: исключение %r за %.2f с", fn.__name__, e, time.time() - t0)
            raise
        log.debug("<< %s = %s за %.2f с", fn.__name__, brief(result), time.time() - t0)
        return result
    return wrapper


# ============ БД ============
def db_init(path=DB_PATH):
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            peer_id INTEGER NOT NULL,
            cmid INTEGER,                  -- conversation_message_id: уникален в пределах беседы
            vk_msg_id INTEGER,             -- глобальный id (в беседах у бота он = 0)
            from_id INTEGER NOT NULL,
            is_bot INTEGER NOT NULL DEFAULT 0,
            text TEXT,                     -- текст вместе с пересланными, постами и пометками вложений
            images TEXT,                   -- JSON-список ссылок на картинки
            ts INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_peer_ts ON messages(peer_id, ts);
        CREATE UNIQUE INDEX IF NOT EXISTS uq_peer_cmid
            ON messages(peer_id, cmid) WHERE cmid > 0;
        CREATE UNIQUE INDEX IF NOT EXISTS uq_peer_msg
            ON messages(peer_id, vk_msg_id) WHERE vk_msg_id > 0;
        CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS roles (peer_id INTEGER PRIMARY KEY, role TEXT);
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,   -- у сообществ отрицательный
            first_name TEXT,
            last_name TEXT,
            updated_at INTEGER
        );
    """)
    con.commit()
    return con


def db_add_message(peer_id, from_id, is_bot, text, ts, cmid=None, vk_msg_id=None, images=()):
    """Сохраняет сообщение, дубли (по cmid или id) игнорирует. Возвращает rowid."""
    cmid = cmid or None
    vk_msg_id = vk_msg_id or None
    images_json = json.dumps(list(images), ensure_ascii=False) if images else None
    try:
        if is_bot and cmid:
            # ответ, сохранённый при отправке без cmid, получает cmid из события
            row = DB.execute(
                "SELECT id FROM messages WHERE peer_id=? AND is_bot=1 AND cmid IS NULL "
                "AND text=? AND ABS(ts-?)<=120 ORDER BY id DESC LIMIT 1",
                (peer_id, text, ts),
            ).fetchone()
            if row:
                DB.execute("UPDATE messages SET cmid=? WHERE id=?", (cmid, row[0]))
                DB.commit()
                return row[0]
        cur = DB.execute(
            "INSERT OR IGNORE INTO messages(peer_id,cmid,vk_msg_id,from_id,is_bot,text,images,ts) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (peer_id, cmid, vk_msg_id, from_id, int(bool(is_bot)), text, images_json, ts),
        )
        DB.commit()
        if cur.rowcount:
            return cur.lastrowid
        row = DB.execute(
            "SELECT id FROM messages WHERE peer_id=? AND (cmid=? OR vk_msg_id=?)",
            (peer_id, cmid, vk_msg_id),
        ).fetchone()
        return row[0] if row else None
    except Exception as e:
        log.warning("db_add_message: %s", e)
        return None


def _rows(rows):
    """Строки контекста: (id, from_id, is_bot, text, ts, images[])."""
    return [(r[0], r[1], r[2], r[3], r[4], json.loads(r[5]) if r[5] else []) for r in rows]


NOT_SYSTEM_SQL = "NOT (is_bot=1 AND COALESCE(text,'') LIKE ?)"   # параметр: SYSTEM_MARK + "%"


def db_get_context(peer_id, limit, since_ts=None, min_rows=0, exclude_ids=(), after_ts=None):
    """
    Последние `limit` сообщений беседы в порядке старые → новые.
    since_ts  — мягкая граница: старше неё берём не больше `min_rows` последних сообщений;
    after_ts  — жёсткая граница (команда /забудь): старше неё не берём ничего;
    exclude_ids — не включать эти сообщения (те, на которые сейчас отвечаем).
    Служебные сообщения бота (SYSTEM_MARK) не считаются: «/контекст 50» — 50 настоящих сообщений.
    """
    exclude_ids = list(exclude_ids) or [-1]
    marks = ",".join("?" * len(exclude_ids))
    rows = DB.execute(
        f"SELECT id,from_id,is_bot,text,ts,images FROM messages "
        f"WHERE peer_id=? AND id NOT IN ({marks}) AND ts>=? AND {NOT_SYSTEM_SQL} "
        f"ORDER BY ts DESC, id DESC LIMIT ?",
        (peer_id, *exclude_ids, after_ts or 0, SYSTEM_MARK + "%", limit),
    ).fetchall()
    rows.reverse()
    if since_ts is not None:
        recent = [r for r in rows if r[4] >= since_ts]
        if len(recent) < min_rows:
            recent = rows[-min_rows:] if min_rows else []
        rows = recent
    return _rows(rows)


def db_get_range(peer_id, start_ts, limit, exclude_ids=()):
    """Первые `limit` сообщений беседы, начиная с start_ts (старые → новые)."""
    exclude_ids = list(exclude_ids) or [-1]
    marks = ",".join("?" * len(exclude_ids))
    rows = DB.execute(
        f"SELECT id,from_id,is_bot,text,ts,images FROM messages "
        f"WHERE peer_id=? AND id NOT IN ({marks}) AND ts>=? ORDER BY ts ASC, id ASC LIMIT ?",
        (peer_id, *exclude_ids, start_ts, limit),
    ).fetchall()
    return _rows(rows)


def role_file_path():
    return ROLE_FILE if os.path.isabs(ROLE_FILE) else os.path.join(BOT_DIR, ROLE_FILE)


def read_text_file(path):
    with open(path, "rb") as f:
        data = f.read()
    for enc in ("utf-8-sig", "cp1251"):     # Блокнот Windows может сохранить в ANSI
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def default_role():
    """Роль по умолчанию: текст файла Роль.txt рядом с ботом (перечитывается, если файл
    изменился), а если файла нет или он пуст — DEFAULT_ROLE."""
    global _role_file_cache
    path = role_file_path()
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return DEFAULT_ROLE
    if _role_file_cache[0] != (path, mtime):
        try:
            text = read_text_file(path).strip()
        except OSError as e:
            log.warning("%s: %s", path, e)
            text = ""
        _role_file_cache = ((path, mtime), text)
        log.info("Роль по умолчанию: %s (%d символов)", path, len(text))
    return _role_file_cache[1] or DEFAULT_ROLE


def db_get_role(peer_id):
    r = DB.execute("SELECT role FROM roles WHERE peer_id=?", (peer_id,)).fetchone()
    return r[0] if r else default_role()


def db_del_role(peer_id):
    """Удаляет роль беседы: дальше действует роль по умолчанию (Роль.txt)."""
    deleted = DB.execute("DELETE FROM roles WHERE peer_id=?", (peer_id,)).rowcount
    DB.commit()
    return bool(deleted)


def db_set_role(peer_id, role):
    DB.execute(
        "INSERT INTO roles(peer_id, role) VALUES(?,?) "
        "ON CONFLICT(peer_id) DO UPDATE SET role=excluded.role",
        (peer_id, role),
    )
    DB.commit()


def db_state_get(key, default=None):
    r = DB.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    return r[0] if r else default


def db_state_set(key, value):
    DB.execute(
        "INSERT INTO state(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )
    DB.commit()


def db_state_del(key):
    DB.execute("DELETE FROM state WHERE key=?", (key,))
    DB.commit()


def db_get_user_name(user_id):
    r = DB.execute("SELECT first_name, last_name FROM users WHERE user_id=?", (user_id,)).fetchone()
    if not r:
        return None
    return " ".join(x for x in r if x).strip() or None


def db_upsert_user(user_id, first_name, last_name=""):
    DB.execute(
        "INSERT INTO users(user_id, first_name, last_name, updated_at) VALUES(?,?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET first_name=excluded.first_name, "
        "last_name=excluded.last_name, updated_at=excluded.updated_at",
        (user_id, first_name or "", last_name or "", int(time.time())),
    )
    DB.commit()


def find_old_db():
    """БД bot6: OLD_DB_PATH, иначе gran6.db рядом с ботом, в текущей папке или в ../bot6."""
    if OLD_DB_PATH:
        return OLD_DB_PATH if os.path.exists(OLD_DB_PATH) else None
    for folder in (BOT_DIR, os.getcwd(), os.path.join(BOT_DIR, "..", "bot6")):
        path = os.path.join(folder, OLD_DB_NAME)
        if os.path.exists(path):
            return path
    return None


def migrate_from_bot6(old_path=None):
    """Переносит из БД bot6 сообщения, роли, имена и настройки бесед (/забудь, /лимит символов,
    начало истории чата): схема та же, загружать историю из ВК заново не нужно."""
    if db_state_get("migrated_bot6") == "1":
        return
    old_path = old_path or find_old_db()
    if not old_path:
        return
    try:
        old = sqlite3.connect(old_path)
        n = 0
        for row in old.execute(
                "SELECT peer_id,cmid,vk_msg_id,from_id,is_bot,text,images,ts FROM messages ORDER BY id"):
            n += DB.execute(
                "INSERT OR IGNORE INTO messages(peer_id,cmid,vk_msg_id,from_id,is_bot,text,images,ts) "
                "VALUES(?,?,?,?,?,?,?,?)", row).rowcount
        for peer_id, role in old.execute("SELECT peer_id, role FROM roles"):
            DB.execute("INSERT OR IGNORE INTO roles(peer_id, role) VALUES(?,?)", (peer_id, role))
        for row in old.execute("SELECT user_id, first_name, last_name, updated_at FROM users"):
            DB.execute("INSERT OR IGNORE INTO users VALUES(?,?,?,?)", row)
        for key, value in old.execute(
                "SELECT key, value FROM state WHERE key LIKE 'forget_%' OR key LIKE 'chars_%' "
                "OR key LIKE 'history_complete_%'"):
            DB.execute("INSERT OR IGNORE INTO state(key, value) VALUES(?,?)", (key, value))
        old.close()
        DB.commit()
        if n:
            db_state_set("initial_load_done", "1")
        db_state_set("migrated_bot6", "1")
        log.info("Из %s перенесено %d сообщений, роли, имена и настройки бесед", old_path, n)
    except Exception as e:
        log.warning("migrate_from_bot6 (%s): %s", old_path, e)


# ============ Имена ============
def vk_fetch_names(ids):
    """Пакетно подтягивает имена пользователей (id > 0) и сообществ (id < 0)."""
    users = [str(i) for i in ids if i and i > 0]
    groups = [str(-i) for i in ids if i and i < 0]
    for i in range(0, len(users), 900):
        try:
            for u in vk.users.get(user_ids=",".join(users[i:i + 900])):
                db_upsert_user(u["id"], u.get("first_name", ""), u.get("last_name", ""))
        except Exception as e:
            log.warning("users.get: %s", e)
    for i in range(0, len(groups), 450):
        try:
            res = vk.groups.getById(group_ids=",".join(groups[i:i + 450]))
            # новые версии API отдают {"groups": [...]}, старые — список
            for g in (res.get("groups", []) if isinstance(res, dict) else res):
                db_upsert_user(-g["id"], g.get("name", ""), "")
        except Exception as e:
            log.warning("groups.getById: %s", e)


def ensure_names(ids):
    """Дозагрузка имён, которых нет в БД или которые устарели (>24ч)."""
    t = int(time.time())
    missing = []
    for i in set(ids):
        if not i or i == BOT_GROUP_ID:
            continue
        r = DB.execute("SELECT updated_at FROM users WHERE user_id=?", (i,)).fetchone()
        if not r or t - r[0] >= 86400:
            missing.append(i)
    if missing:
        vk_fetch_names(missing)


def display_name(from_id):
    if from_id == BOT_GROUP_ID:
        return BOT_NAME
    name = db_get_user_name(from_id)
    if name:
        return name
    return f"club{-from_id}" if from_id < 0 else f"id{from_id}"


def collect_author_ids(obj, acc=None, depth=0):
    """Все from_id/owner_id внутри сообщения: пересланные, reply, посты."""
    acc = set() if acc is None else acc
    if not isinstance(obj, dict) or depth > FWD_MAX_DEPTH + 2:
        return acc
    for key in ("from_id", "owner_id"):
        if obj.get(key):
            acc.add(obj[key])
    for f in obj.get("fwd_messages") or []:
        collect_author_ids(f, acc, depth + 1)
    if obj.get("reply_message"):
        collect_author_ids(obj["reply_message"], acc, depth + 1)
    for a in obj.get("attachments") or []:
        if a.get("type") in ("wall", "wall_reply"):
            collect_author_ids(a.get(a["type"]), acc, depth + 1)
    for c in obj.get("copy_history") or []:
        collect_author_ids(c, acc, depth + 1)
    return acc


# ============ Права ============
def is_private(peer_id):
    return 0 < peer_id < CHAT_PEER_OFFSET


def chat_admins(peer_id):
    """Админы и создатель беседы (messages.getConversationMembers; боту нужны права
    админа в беседе). Список кэшируется на ADMIN_CACHE_MINUTES."""
    cached = _admin_cache.get(peer_id)
    if cached and now() - cached[0] < ADMIN_CACHE_MINUTES * 60:
        return cached[1]
    admins = set()
    try:
        res = vk.messages.getConversationMembers(peer_id=peer_id)
        for m in res.get("items", []):
            if m.get("is_admin") or m.get("is_owner"):
                admins.add(m.get("member_id"))
    except Exception as e:
        log.warning("getConversationMembers peer=%s: %s — админы только из ADMIN_IDS", peer_id, e)
    _admin_cache[peer_id] = (now(), admins)
    return admins


def is_admin(peer_id, user_id):
    """В личке собеседник — админ своего чата; в беседе — ADMIN_IDS и админы беседы."""
    if is_private(peer_id):
        return True
    return user_id in ADMIN_IDS or user_id in chat_admins(peer_id)


# ============ Разбор сообщений: пересланные, картинки, посты ============
def photo_url(photo):
    """Ссылка на фото подходящего размера: самое большое не больше IMAGE_MAX_SIDE."""
    sizes = [s for s in (photo or {}).get("sizes") or [] if s.get("url")]
    if not sizes:
        return None
    area = lambda s: (s.get("width") or 0) * (s.get("height") or 0)
    fit = [s for s in sizes if max(s.get("width") or 0, s.get("height") or 0) <= IMAGE_MAX_SIDE]
    return max(fit or sizes, key=area)["url"]


def shorten(text, limit):
    """Обрезает текст до limit символов; limit <= 0 — не обрезает."""
    text = (text or "").strip()
    return text if limit <= 0 or len(text) <= limit else text[:limit].rstrip() + "…"


def fetch_wall_post(owner_id, post_id):
    """Полный текст поста, если во вложении его нет. Нужен ключ пользователя
    или сервисный ключ (VK_USER_TOKEN): ключ сообщества wall.getById не вызывает."""
    if not VK_USER_TOKEN or not owner_id or not post_id:
        return None
    try:
        res = vk.wall.getById(posts=f"{owner_id}_{post_id}", access_token=VK_USER_TOKEN)
        items = res.get("items", []) if isinstance(res, dict) else res
        return items[0] if items else None
    except Exception as e:
        log.warning("wall.getById: %s", e)
        return None


def render_attachments(attachments, images, depth):
    """Пометки о вложениях + текст постов. Ссылки на картинки складываются в images."""
    lines = []
    for a in attachments or []:
        t = a.get("type")
        obj = a.get(t) or {}
        if t == "photo":
            url = photo_url(obj)
            if url:
                images.append(url)
            lines.append("[фото]" + (f" {obj['text']}" if obj.get("text") else ""))
        elif t == "wall":
            lines.extend(render_wall(obj, images, depth))
        elif t == "wall_reply":
            lines.append(f"[комментарий {display_name(obj.get('from_id', 0))}: "
                         f"«{(obj.get('text') or '').strip()}»]")
        elif t == "sticker":
            lines.append("[стикер]")
        elif t == "audio_message":
            tr = obj.get("transcript")
            lines.append(f"[голосовое: «{tr}»]" if tr else "[голосовое сообщение]")
        elif t == "video":
            lines.append(f"[видео: {obj.get('title') or 'без названия'}]")
        elif t == "audio":
            lines.append(f"[аудио: {obj.get('artist', '')} — {obj.get('title', '')}]")
        elif t == "doc":
            if obj.get("type") == 4 or (obj.get("ext") or "").lower() in ("jpg", "jpeg", "png", "webp"):
                url = obj.get("url")
                if url:
                    images.append(url)
            lines.append(f"[файл: {obj.get('title') or obj.get('ext') or 'документ'}]")
        elif t == "link":
            lines.append(f"[ссылка: {obj.get('title') or ''} {obj.get('url') or ''}]".replace("  ", " "))
        elif t == "poll":
            answers = ", ".join(x.get("text", "") for x in obj.get("answers") or [])
            lines.append(f"[опрос: {obj.get('question', '')} ({answers})]")
        elif t:
            lines.append(f"[{t}]")
    return lines


def render_wall(post, images, depth):
    """Текст поста (записи), которым поделились, — целиком, с репостами и картинками."""
    if post.get("is_deleted") or post.get("deleted"):
        return ["[запись удалена или недоступна]"]
    if not (post.get("text") or post.get("attachments") or post.get("copy_history")):
        post = fetch_wall_post(post.get("owner_id") or post.get("to_id"), post.get("id")) or post
    author = display_name(post.get("from_id") or post.get("owner_id") or 0)
    text = (post.get("text") or "").strip()
    lines = [f"[запись {author}" + (f": «{text}»]" if text else "]")]
    if depth < FWD_MAX_DEPTH:
        lines.extend("  " + l for l in render_attachments(post.get("attachments"), images, depth + 1))
        for orig in post.get("copy_history") or []:
            lines.append("  репост:")
            lines.extend("  " + l for l in render_wall(orig, images, depth + 1))
    if not text and len(lines) == 1:
        lines[0] = f"[запись {author} — текст недоступен боту]"
    return lines


def render_message(msg, images=None, depth=0):
    """
    Превращает объект сообщения ВК в текст для истории:
    текст + вложения + посты + пересланные сообщения (с вложенностью).
    Возвращает (text, images).
    """
    images = [] if images is None else images
    lines = []
    if msg.get("text"):
        lines.append(msg["text"])
    lines.extend(render_attachments(msg.get("attachments"), images, depth))
    if depth < FWD_MAX_DEPTH:
        for f in msg.get("fwd_messages") or []:
            when = datetime.fromtimestamp(f.get("date", 0)).strftime("%d.%m %H:%M")
            lines.append(f"[переслано от {display_name(f.get('from_id', 0))}, {when}]:")
            inner, _ = render_message(f, images, depth + 1)
            lines.extend("> " + l for l in (inner.splitlines() or [""]))
    elif msg.get("fwd_messages"):
        lines.append("[ещё пересланные сообщения]")
    return "\n".join(lines), images


def render_for_history(msg):
    """Как render_message, но с пометкой, на что это ответ. Само сообщение, на которое
    ответили, обычно уже есть в истории, поэтому в пометке — короткая цитата."""
    text, images = render_message(msg)
    rp = msg.get("reply_message")
    if rp:
        quote, _ = render_message(rp, [])
        text = (f"(в ответ {display_name(rp.get('from_id', 0))}: "
                f"«{shorten(quote, QUOTE_MAX_CHARS)}»)\n{text}")
    return text, images


def store_vk_message(peer_id, msg):
    """Сохраняет сообщение из ВК в историю. Возвращает (rowid, text, images)."""
    from_id = msg.get("from_id", 0)
    text, images = render_for_history(msg)
    rowid = db_add_message(
        peer_id, from_id, from_id == BOT_GROUP_ID, text,
        msg.get("date") or int(now()),
        cmid=msg.get("conversation_message_id"), vk_msg_id=msg.get("id"), images=images,
    )
    return rowid, text, images


# ============ VK: отправка ============
def split_message(text, limit=VK_MESSAGE_LIMIT):
    """Режет длинный текст на части не длиннее limit: по абзацу, иначе по пробелу, иначе жёстко."""
    parts = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        parts.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        parts.append(text)
    return parts


def vk_send(peer_id, text, reply_to_msg=None):
    """
    Отправляет ответ и сразу сохраняет его в историю. Длинный текст уходит
    несколькими сообщениями (split_message). В беседах reply делается через
    параметр forward с conversation_message_ids: reply_to там не работает, потому что id = 0.
    """
    for i, part in enumerate(split_message(text)):
        params = dict(peer_ids=str(peer_id), message=part,
                      random_id=int(time.time() * 1000) % (2 ** 31) + i)
        if reply_to_msg and i == 0:
            cmid = reply_to_msg.get("conversation_message_id")
            if cmid:
                params["forward"] = json.dumps({
                    "peer_id": peer_id, "conversation_message_ids": [cmid], "is_reply": 1,
                })
            elif reply_to_msg.get("id"):
                params["reply_to"] = reply_to_msg["id"]
        sent_cmid = sent_id = None
        try:
            res = vk.messages.send(**params)
            if isinstance(res, list) and res:
                sent_cmid = res[0].get("conversation_message_id")
                sent_id = res[0].get("message_id")
        except Exception as e:
            log.error("vk_send: %s", e)
            continue
        db_add_message(peer_id, BOT_GROUP_ID, True, part, int(now()),
                       cmid=sent_cmid, vk_msg_id=sent_id)


def send_system(peer_id, text, reply_to_msg=None):
    """Служебное сообщение (помощь, лимиты, пауза): с пометкой SYSTEM_MARK, в контекст не идёт."""
    vk_send(peer_id, f"{SYSTEM_MARK} {text}", reply_to_msg=reply_to_msg)


# ============ Загрузка истории ============
@traced
def fetch_history(peer_id, count, offset=0, stop_before_ts=None):
    """
    Загружает из ВК до `count` сообщений диалога (от новых к старым, начиная с offset)
    и сохраняет их. stop_before_ts — остановиться, как только дошли до сообщений старше.
    Возвращает (сколько загружено, дошли ли до начала диалога).
    """
    collected, reached_start = [], False
    while len(collected) < count:
        want = min(200, count - len(collected))
        try:
            items = vk.messages.getHistory(peer_id=peer_id, count=want, offset=offset)["items"]
        except Exception as e:
            log.warning("getHistory peer=%s: %s", peer_id, e)
            break
        collected.extend(items)
        offset += len(items)
        if len(items) < want:
            reached_start = True
            break
        if stop_before_ts is not None and min(m.get("date", 0) for m in items) <= stop_before_ts:
            break

    # ВК отдаёт от новых к старым — разворачиваем
    collected.reverse()
    ids = set()
    for m in collected:
        collect_author_ids(m, ids)
    ensure_names(ids)
    for m in collected:
        store_vk_message(peer_id, m)
    return len(collected), reached_start


def load_history():
    """
    Первый старт — до INITIAL_LOAD сообщений на диалог, следующие старты —
    SYNC_ON_START последних (догрузить то, что пришло, пока бот был выключен).
    Дубли отсекаются уникальными индексами.
    """
    first = db_state_get("initial_load_done") != "1"
    per_peer = INITIAL_LOAD if first else SYNC_ON_START
    if per_peer <= 0:
        return
    log.info("Загрузка истории (до %d сообщений на диалог)...", per_peer)
    try:
        dialogs = vk.messages.getConversations(count=200)["items"]
    except Exception as e:
        log.error("getConversations: %s", e)
        return

    total = 0
    for d in dialogs:
        peer_id = d["conversation"]["peer"]["id"]
        n, reached_start = fetch_history(peer_id, per_peer)
        if reached_start:
            db_state_set(f"history_complete_{peer_id}", 1)
        total += n
        log.info("peer=%s загружено %d", peer_id, n)

    db_state_set("initial_load_done", "1")
    log.info("Загрузка истории завершена, всего %d сообщений.", total)


def ensure_history_since(peer_id, start_ts):
    """
    Пересказ с даты: если в БД нет сообщений не позже start_ts, догружает
    историю из ВК (от новых к старым), пока не дойдёт до start_ts или до начала
    чата. Начало чата запоминается — второй раз грузить не будем.
    Возвращает число загруженных сообщений.
    """
    first_ts, stored = DB.execute(
        "SELECT MIN(ts), COUNT(*) FROM messages WHERE peer_id=?", (peer_id,)).fetchone()
    if first_ts is not None and first_ts <= start_ts:
        return 0
    if db_state_get(f"history_complete_{peer_id}") == "1":
        return 0  # раньше start_ts чата не существовало — догружать нечего
    # сообщения в БД — это последние сообщения диалога, их можно пропустить;
    # 200 — запас на сообщения, удалённые в ВК, но оставшиеся в БД
    offset = max(0, stored - 200)
    n, reached_start = fetch_history(peer_id, HISTORY_LOAD_MAX, offset=offset,
                                     stop_before_ts=start_ts)
    if reached_start:
        db_state_set(f"history_complete_{peer_id}", 1)
    log.info("peer=%s: для пересказа с %s догружено %d сообщений%s", peer_id,
             fmt_ts(start_ts), n, " (дошли до начала чата)" if reached_start else "")
    return n


# ============ Настройки беседы: токены, символы, дневной лимит ============
def _limit_get(key, default):
    """Настройка беседы в state: 'значение|когда_задано'."""
    raw = db_state_get(key)
    if not raw:
        return default, None
    value, set_ts = raw.split("|")
    return int(value), int(set_ts)


def tokens_reset_at(peer_id):
    """Когда /лимит токенов беседы вернётся к стандарту (timestamp) или None."""
    _value, set_ts = _limit_get(f"tokens_{peer_id}", MAX_TOKENS_DEFAULT)
    if not set_ts or not TOKEN_LIMIT_RESET_MINUTES:
        return None
    return set_ts + TOKEN_LIMIT_RESET_MINUTES * 60


def tokens_limit(peer_id):
    """Лимит выходных токенов беседы; через TOKEN_LIMIT_RESET_MINUTES — снова стандарт
    (и об этом — служебное сообщение в чат, чтобы возврат не был незаметным)."""
    value, set_ts = _limit_get(f"tokens_{peer_id}", MAX_TOKENS_DEFAULT)
    reset_at = tokens_reset_at(peer_id)
    if reset_at and now() >= reset_at:
        db_state_del(f"tokens_{peer_id}")
        log.info("peer=%s: лимит токенов %d (задан %s) вернулся к стандарту %d", peer_id, value,
                 fmt_ts(set_ts), MAX_TOKENS_DEFAULT)
        try:
            send_system(peer_id, f"Прошло {TOKEN_LIMIT_RESET_MINUTES} мин с «/лимит токенов {value}» "
                                 f"(задан в {datetime.fromtimestamp(set_ts):%H:%M}) — лимит токенов "
                                 f"вернулся к стандарту {MAX_TOKENS_DEFAULT}.")
        except Exception as e:
            log.warning("сообщение о возврате лимита: %s", e)
        return MAX_TOKENS_DEFAULT
    return value


def check_token_limits(t=None):
    """Возвращает к стандарту истёкшие лимиты токенов всех бесед (сообщение — вовремя,
    а не при следующем обращении). Вызывается из process_pending не чаще раза в 30 с."""
    global _last_limit_check
    t = now() if t is None else t
    if t - _last_limit_check < 30:
        return
    _last_limit_check = t
    for (key,) in DB.execute("SELECT key FROM state WHERE key LIKE 'tokens_%'").fetchall():
        try:
            tokens_limit(int(key[len("tokens_"):]))
        except ValueError:
            continue


def parse_limit_value(arg):
    """Число из «/лимит токенов 3000», «: 3000», «30 000», «30к», «2k», «3 тыс». None — не число."""
    if arg is None:
        return None
    m = re.fullmatch(r"(\d(?:[\d\s]*\d)?)\s*(к|k|тыс\.?)?", arg.strip().lower())
    if not m:
        return None
    value = int(re.sub(r"\s", "", m.group(1)))
    return value * 1000 if m.group(2) else value


def chars_limit(peer_id):
    """Лимит символов истории в запросе к LLM для беседы."""
    return _limit_get(f"chars_{peer_id}", MAX_CONTEXT_CHARS)[0]


def day_key(t=None):
    """Сутки для дневного лимита: начинаются в LIMIT_RESET_HOUR."""
    t = now() if t is None else t
    return (datetime.fromtimestamp(t) - timedelta(hours=LIMIT_RESET_HOUR)).strftime("%Y-%m-%d")


def next_reset_dt():
    d = datetime.fromtimestamp(now()) - timedelta(hours=LIMIT_RESET_HOUR)
    return datetime(d.year, d.month, d.day) + timedelta(days=1, hours=LIMIT_RESET_HOUR)


def daily_key(peer_id):
    return f"daily_{peer_id}" if DAILY_LIMIT_PER_CHAT else "daily_all"


def daily_state(peer_id):
    raw = db_state_get(daily_key(peer_id))
    st = json.loads(raw) if raw else {}
    if st.get("day") != day_key():
        st = {"day": day_key(), "count": 0, "notified": []}   # новые сутки — лимит сброшен
    return st


def daily_limit_reached(peer_id):
    return DAILY_MESSAGE_LIMIT > 0 and daily_state(peer_id)["count"] >= DAILY_MESSAGE_LIMIT


def count_answer(peer_id):
    """Учитывает ответ бота в дневном лимите и сообщает о достижении 25/50/90/100 %."""
    if DAILY_MESSAGE_LIMIT <= 0:
        return
    st = daily_state(peer_id)
    st["count"] += 1
    crossed = [p for p in LIMIT_NOTIFY_PERCENTS
               if p not in st["notified"] and st["count"] >= math.ceil(DAILY_MESSAGE_LIMIT * p / 100)]
    st["notified"].extend(crossed)
    db_state_set(daily_key(peer_id), json.dumps(st))
    if not crossed:
        return
    p = max(crossed)  # при маленьком лимите пороги могут совпасть — сообщаем о старшем
    if p >= 100:
        send_system(peer_id, f"Дневной лимит сообщений исчерпан: {st['count']} из "
                             f"{DAILY_MESSAGE_LIMIT}. До {next_reset_dt():%H:%M %d.%m} "
                             "бот не будет отвечать. Админ может сбросить лимит командой "
                             "«/сбросить лимит сообщений».")
    else:
        send_system(peer_id, f"Использовано {st['count']} из {DAILY_MESSAGE_LIMIT} "
                             f"сообщений на сегодня ({p}% дневного лимита).")


# ============ Состояние беседы: очередь, пауза, размер контекста ============
class PeerState:
    def __init__(self):
        self.pending = []              # обращения, ждущие ответа: [Request]
        self.first_pending_ts = 0.0
        self.last_pending_ts = 0.0
        self.paused_until = 0.0
        self.chains = {}               # from_id -> [часть]: длинные сообщения без обращения подряд
        self.buffers = {}              # from_id -> {...}: /роль или «запомни», ждущие продолжения


def peer_state(peer_id):
    if peer_id not in PEERS:
        PEERS[peer_id] = PeerState()
    return PEERS[peer_id]


# ============ Размер контекста: /контекст, /автоконтекст, /сброс контекста ============
# ctx_<peer>     — размер, заданный «/контекст N» (или «/сброс контекста N»);
# autoctx_<peer> — размер, посчитанный «/автоконтекст» (действует поверх ctx_ до «/сброс контекста»).
def context_setting(peer_id):
    """(размер, откуда): 'авто', 'задан' или 'стандарт'."""
    for key, source in (("autoctx", "авто"), ("ctx", "задан")):
        value = _limit_get(f"{key}_{peer_id}", None)[0]
        if value:
            return value, source
    return CONTEXT_SIZE, "стандарт"


def context_size(peer_id):
    return context_setting(peer_id)[0]


def context_text(peer_id):
    """«30 сообщений (стандарт)» / «120 сообщений (автоконтекст; без него — 50)»."""
    size, source = context_setting(peer_id)
    if source == "стандарт":
        return f"{size} сообщений (стандарт)"
    base = _limit_get(f"ctx_{peer_id}", None)[0] or CONTEXT_SIZE
    if source == "авто":
        return f"{size} сообщений (автоконтекст; после «/сброс контекста» — {base})"
    return f"{size} сообщений (задан; стандарт {CONTEXT_SIZE})"


def set_context(peer_id, key, value):
    value = min(max(int(value), 1), CONTEXT_SIZE_MAX)
    db_state_set(f"{key}_{peer_id}", f"{value}|{int(now())}")
    return value


def db_count_since(peer_id, start_ts, start_id=0, exclude_ids=()):
    """Сколько сообщений беседы с start_ts (а при равном времени — с id ≥ start_id) до сейчас.
    Служебные сообщения бота не считаются — как и в db_get_context."""
    exclude_ids = list(exclude_ids) or [-1]
    marks = ",".join("?" * len(exclude_ids))
    return DB.execute(
        f"SELECT COUNT(*) FROM messages WHERE peer_id=? AND id NOT IN ({marks}) "
        f"AND (ts>? OR (ts=? AND id>=?)) AND {NOT_SYSTEM_SQL}",
        (peer_id, *exclude_ids, start_ts, start_ts, start_id, SYSTEM_MARK + "%")).fetchone()[0]


def find_stored_message(peer_id, msg):
    """Пересланное сообщение или reply → (id, ts) этого сообщения в истории беседы или None.
    Ищется по conversation_message_id (если оно из этой беседы), затем по автору и времени,
    затем по автору и тексту."""
    cmid, ts = msg.get("conversation_message_id"), msg.get("date")
    from_id, text = msg.get("from_id"), (msg.get("text") or "").strip()
    if cmid and msg.get("peer_id", peer_id) == peer_id:
        row = DB.execute("SELECT id, ts FROM messages WHERE peer_id=? AND cmid=?",
                         (peer_id, cmid)).fetchone()
        if row and (not ts or abs(row[1] - ts) <= 60):
            return row
    if from_id and ts:
        rows = DB.execute("SELECT id, ts, text FROM messages WHERE peer_id=? AND from_id=? AND ts=? "
                          "ORDER BY id", (peer_id, from_id, ts)).fetchall()
        for rid, rts, rtext in rows:
            if not text or text in (rtext or ""):
                return rid, rts
        if rows:
            return rows[0][0], rows[0][1]
    if from_id and text:
        row = DB.execute("SELECT id, ts FROM messages WHERE peer_id=? AND from_id=? AND text LIKE ? "
                         "ORDER BY ts DESC, id DESC LIMIT 1",
                         (peer_id, from_id, "%" + text[:200] + "%")).fetchone()
        if row:
            return row
    return None


AUTOCTX_UNITS = (("м", 60), ("ч", 3600), ("д", 86400), ("сут", 86400), ("нед", 7 * 86400))


def parse_period(arg):
    """«2 ч», «2ч», «30 мин», «1 день», «2,5 часа», «2» (часы) → секунды или None."""
    m = re.fullmatch(r"(\d+(?:[.,]\d+)?)\s*([а-яё]*)\.?", (arg or "").strip().lower())
    if not m:
        return None
    number, unit = float(m.group(1).replace(",", ".")), m.group(2)
    if not unit:
        return int(number * 3600) or None
    if unit.startswith("мес"):
        return None
    for prefix, sec in AUTOCTX_UNITS:
        if unit.startswith(prefix):
            return int(number * sec) or None
    return None


def fmt_period(sec):
    if sec % 86400 == 0:
        return f"{sec // 86400} сут"
    if sec % 3600 == 0:
        return f"{sec // 3600} ч"
    if sec >= 3600:
        return f"{sec / 3600:.1f} ч".replace(".", ",")
    return f"{max(sec // 60, 1)} мин"


def is_paused(peer_id):
    return peer_state(peer_id).paused_until > now()


class Request:
    """Обращение к боту: сообщение ВК + то, что о нём уже известно."""

    def __init__(self, message, rowid=None, text="", images=(), command=None):
        self.message = message
        self.peer_id = message["peer_id"]
        self.from_id = message.get("from_id", 0)
        self.ts = message.get("date") or int(now())
        self.rowid = rowid
        self.text = text
        self.images = list(images)
        self.command = command         # (имя, аргумент) — для команд, отложенных паузой
        self.rowids = [rowid] if rowid else []
        self.parts = 1                 # из скольких сообщений ВК собрано (длинное разрезано)
        self.last_part_ts = self.ts
        self.last_part_len = len(message.get("text") or "")
        self.note = ""                 # пояснение к ответу на команду

    def add_part(self, part):
        """Следующая часть длинного сообщения, которое ВК разрезал на несколько."""
        if part["rowid"]:
            self.rowids.append(part["rowid"])
        self.text = f"{self.text}\n{part['text']}" if self.text else part["text"]
        self.images.extend(part["images"])
        self.parts += 1
        self.last_part_ts, self.last_part_len = part["ts"], part["len"]

    def prepend(self, parts):
        """Предыдущие части: обращение («Грань, …») оказалось в последней части."""
        self.rowids = [p["rowid"] for p in parts if p["rowid"]] + self.rowids
        self.text = "\n".join([p["text"] for p in parts] + [self.text])
        self.images = [u for p in parts for u in p["images"]] + self.images
        self.parts += len(parts)
        self.ts = parts[0]["ts"]

    def __repr__(self):
        return (f"Request(peer={self.peer_id}, from={self.from_id}, parts={self.parts}, "
                f"cmd={self.command!r}, text={shorten(self.text, 60)!r})")


def enqueue(req):
    st = peer_state(req.peer_id)
    t = now()
    if not st.pending:
        st.first_pending_ts = t
    st.last_pending_ts = t
    st.pending.append(req)


def process_pending(t=None):
    """
    Отвечает на накопленные обращения: когда BATCH_WAIT_SEC не было новых обращений
    (или с первого прошло BATCH_MAX_WAIT_SEC) и беседа не на паузе. Вызывается из
    главного цикла после каждой проверки Long Poll.
    """
    t = now() if t is None else t
    if DB is not None:
        check_token_limits(t)
    for peer_id, st in list(PEERS.items()):
        for from_id, buf in list(st.buffers.items()):
            if t - buf["t"] >= ROLE_JOIN_SEC:
                finish_buffer(peer_id, from_id)       # продолжение так и не пришло
        for from_id, chain in list(st.chains.items()):
            if t - chain[-1]["added"] > SPLIT_JOIN_SEC * 3:
                del st.chains[from_id]
        if st.paused_until > t:
            continue
        resumed = bool(st.paused_until)     # пауза только что кончилась — отвечаем сразу
        st.paused_until = 0.0
        wait = BATCH_WAIT_SEC
        if any(r.last_part_len >= SPLIT_MIN_PART_CHARS and not r.command for r in st.pending):
            wait = max(wait, SPLIT_JOIN_SEC)  # длинная часть — может прийти продолжение
        if st.pending and (resumed
                           or t - st.last_pending_ts >= wait
                           or t - st.first_pending_ts >= BATCH_MAX_WAIT_SEC):
            flush(peer_id)


def is_idle():
    """Нет ожидающих ответа обращений, недописанных команд и пауз — можно перезапускаться."""
    t = now()
    return all(not st.pending and not st.buffers and st.paused_until <= t for st in PEERS.values())


# ---- длинные сообщения, которые ВК разрезал на части ----
def find_continuation(st, from_id, ts):
    """Ожидающее ответа обращение того же автора, продолжением которого может быть сообщение
    с датой ts: прошлая часть длинная (≥ SPLIT_MIN_PART_CHARS) и пришла не раньше SPLIT_JOIN_SEC."""
    for req in reversed(st.pending):
        if req.from_id != from_id or req.command:
            continue
        if (req.last_part_len >= SPLIT_MIN_PART_CHARS and 0 <= ts - req.last_part_ts <= SPLIT_JOIN_SEC
                and req.parts < SPLIT_MAX_PARTS):
            return req
        return None
    return None


def chain_continues(chain, ts):
    return (chain and chain[-1]["len"] >= SPLIT_MIN_PART_CHARS
            and 0 <= ts - chain[-1]["ts"] <= SPLIT_JOIN_SEC and len(chain) < SPLIT_MAX_PARTS)


def note_unaddressed_part(st, from_id, part):
    """Длинное сообщение без обращения может оказаться началом обращения, у которого «Грань»
    в последней части: запоминаем цепочку таких сообщений."""
    chain = st.chains.get(from_id)
    if chain_continues(chain, part["ts"]):
        if part["len"] >= SPLIT_MIN_PART_CHARS:
            chain.append(part)
        else:
            del st.chains[from_id]            # сообщение кончилось без обращения к боту
    elif part["len"] >= SPLIT_MIN_PART_CHARS:
        st.chains[from_id] = [part]
    else:
        st.chains.pop(from_id, None)


def take_chain(st, from_id, ts):
    chain = st.chains.pop(from_id, None)
    return chain if chain_continues(chain, ts) else None


def buffer_may_continue(cmd, text, message):
    """/роль или «запомни» с текстом, у которого, похоже, есть продолжение в следующих сообщениях."""
    if cmd[0] == "роль":
        role = extract_role(text)
        return bool(role) and (role[1] is False or (role[1] is None and len(text) >= SPLIT_MIN_PART_CHARS))
    if cmd[0] == "запомни":
        return (not message.get("fwd_messages") and not message.get("reply_message")
                and len(text) >= SPLIT_MIN_PART_CHARS)
    return False


def start_buffer(st, req, text):
    st.buffers[req.from_id] = {"req": req, "text": text, "t": now(), "ts": req.ts,
                               "len": len(text), "parts": 1}
    log.info("peer=%s: команда %s от %s ждёт продолжения (%d символов)", req.peer_id,
             req.command[0], req.from_id, len(text))


def continue_buffer(peer_id, from_id, buf, text, part):
    """Добавляет часть к недописанной команде. True — сообщение поглощено."""
    if not 0 <= part["ts"] - buf["ts"] <= SPLIT_JOIN_SEC:
        finish_buffer(peer_id, from_id)
        return False
    req = buf["req"]
    buf["text"] += "\n" + text
    buf.update(t=now(), ts=part["ts"], len=part["len"], parts=buf["parts"] + 1)
    if part["rowid"]:
        req.rowids.append(part["rowid"])
    if req.command[0] == "роль":
        closed = extract_role(buf["text"])[1]
        done = closed is True or (closed is None and part["len"] < SPLIT_MIN_PART_CHARS)
    else:
        done = part["len"] < SPLIT_MIN_PART_CHARS
    log.info("peer=%s: часть %d команды %s (%d символов)%s", peer_id, buf["parts"],
             req.command[0], part["len"], " — команда собрана" if done else "")
    if done or buf["parts"] >= SPLIT_MAX_PARTS:
        finish_buffer(peer_id, from_id)
    return True


@traced
def finish_buffer(peer_id, from_id):
    """Выполняет /роль или «запомни», собранную из нескольких сообщений."""
    buf = peer_state(peer_id).buffers.pop(from_id, None)
    if not buf:
        return
    req, text = buf["req"], buf["text"]
    if req.command[0] == "роль":
        role, closed = extract_role(text)
        req.command = ("роль", role or None)
        if buf["parts"] > 1:
            req.note += f" Роль собрана из {buf['parts']} сообщений ({len(role)} символов)."
        if closed is False:
            req.note += " Закрывающая скобка так и не пришла — роль взята целиком до конца сообщения."
    else:
        m = REMEMBER_RE.search(text)
        req.command = ("запомни", text[m.end():].strip() if m else text)
    try:
        run_command(req)
    except Exception as e:
        log.exception("команда %s: %s", req.command[0], e)


@traced
def flush(peer_id):
    st = peer_state(peer_id)
    items, st.pending = st.pending, []
    for req in items:
        if req.command:
            try:
                run_llm_command(req)
            except Exception as e:
                log.exception("команда %s: %s", req.command[0], e)
    msgs = [r for r in items if not r.command]
    if msgs:
        answer_batch(peer_id, msgs)


# ============ LLM ============
def build_system_prompt(role):
    t = datetime.fromtimestamp(now()).strftime("%Y-%m-%d %H:%M")
    return (
        f"{role}\n\n"
        f"Сейчас: {t}.\n"
        "Ты пишешь в чате ВКонтакте от лица сообщества. Отвечай кратко и по делу, "
        "если не просят длинного ответа. Используй живой язык. "
        "Пересланные сообщения помечены «[переслано от …]», посты — «[запись …]», "
        "картинки — «[фото]»; сами картинки приложены к запросу, если они есть. "
        "Не подписывайся своим именем в начале ответа."
    )


def fmt_ts(ts):
    return datetime.fromtimestamp(ts).strftime("%H:%M %d.%m.%Y")


def format_line(from_id, is_bot, text, ts):
    who = BOT_NAME if is_bot or from_id == BOT_GROUP_ID else display_name(from_id)
    t = datetime.fromtimestamp(ts).strftime("%d.%m %H:%M")
    return f"[{t}] {who}: {text}"


def context_lines(rows):
    """[(id, строка), ...] без пустых и служебных сообщений бота; из ответов бота убраны
    рассуждения и приставка «Поиск по базе знаний не дал результата»."""
    out = []
    for rid, from_id, is_bot, text, ts, _images in rows:
        if not text or (is_bot and text.startswith(SYSTEM_MARK)):
            continue
        if is_bot or from_id == BOT_GROUP_ID:
            text = strip_bot_extras(text)
            if not text:
                continue
        out.append((rid, format_line(from_id, is_bot, text, ts)))
    return out


def _size(lines):
    return sum(len(l) + 1 for _, l in lines)


def format_context(rows, max_chars=MAX_CONTEXT_CHARS):
    """История без сжатия: если не влезает в max_chars — выбрасываем самые старые строки."""
    lines = context_lines(rows)
    while lines and _size(lines) > max_chars:
        lines.pop(0)
    return "\n".join(l for _, l in lines)


def compress_lines(previous_summary, lines):
    """Сжимает старую часть истории (и прежний пересказ) в пересказ ≤ CONTEXT_SUMMARY_CHARS."""
    old = "\n".join(l for _, l in lines)
    prompt = (
        f"Сожми начало переписки в чате в краткий пересказ не длиннее {CONTEXT_SUMMARY_CHARS} "
        "символов: кто что утверждал, о чём спорили, к чему пришли, открытые вопросы. "
        "Сохрани имена. Пиши только пересказ.\n\n"
        + (f"ПРЕЖНИЙ ПЕРЕСКАЗ:\n{previous_summary}\n\n" if previous_summary else "")
        + f"СООБЩЕНИЯ:\n{old}"
    )
    with quiet_llm():
        summary = llm_call("Ты сжимаешь историю чата для памяти собеседника.", prompt,
                           max_tokens=max(300, CONTEXT_SUMMARY_CHARS // 2))
    return shorten(summary, CONTEXT_SUMMARY_CHARS)


@traced
def build_history(peer_id, rows, max_chars):
    """
    История для запроса. Влезает в max_chars — как есть. Не влезает (большой /контекст
    или /автоконтекст) — старая часть сжимается в пересказ, а свежие сообщения идут
    дословно. Пересказ хранится в БД (ctxsum_<peer>) и дополняется, только когда
    свежая часть снова перестала помещаться, — LLM не вызывается на каждом ответе.
    """
    lines = context_lines(rows)
    if _size(lines) <= max_chars:
        return "\n".join(l for _, l in lines)

    ids = [rid for rid, _ in lines]
    raw = db_state_get(f"ctxsum_{peer_id}")
    cache = json.loads(raw) if raw else None
    avail = max(max_chars - CONTEXT_SUMMARY_CHARS, 0)
    base, candidates, first_id = "", lines, ids[0]
    if cache and cache["first_id"] <= ids[0] and cache["last_id"] in ids:
        fresh = lines[ids.index(cache["last_id"]) + 1:]
        if _size(fresh) <= avail:
            return summary_block(cache["text"], fresh)
        base, candidates, first_id = cache["text"], fresh, cache["first_id"]

    # свежая часть — с запасом, чтобы следующие сообщения тоже поместились без сжатия
    keep = []
    for item in reversed(candidates):
        if _size(keep) + len(item[1]) + 1 > avail * CONTEXT_KEEP_SHARE:
            break
        keep.insert(0, item)
    old = candidates[:len(candidates) - len(keep)]
    summary = compress_lines(base, old) if old else base
    if not summary:
        log.warning("peer=%s: не удалось сжать историю — обрезаю старые сообщения", peer_id)
        return format_context(rows, max_chars)
    db_state_set(f"ctxsum_{peer_id}", json.dumps(
        {"first_id": first_id, "last_id": old[-1][0] if old else cache["last_id"],
         "text": summary}, ensure_ascii=False))
    log.info("peer=%s: %d старых сообщений сжато в пересказ (%d символов)",
             peer_id, len(old), len(summary))
    return summary_block(summary, keep)


def summary_block(summary, lines):
    body = "\n".join(l for _, l in lines)
    return f"(Пересказ более ранней части беседы: {summary})\n{body}".rstrip()


def ssl_contexts():
    """Контексты SSL для скачивания картинок, по очереди: системные корневые сертификаты,
    затем certifi (в нём есть HARICA — корень сертификатов *.userapi.com, которого может
    не быть в хранилище Windows), затем IMAGE_CA_FILE, и (если разрешено) без проверки."""
    global _ssl_contexts
    if _ssl_contexts is None:
        contexts = [("системные сертификаты", ssl.create_default_context())]
        try:
            import certifi
            contexts.append(("certifi", ssl.create_default_context(cafile=certifi.where())))
        except Exception as e:      # certifi ставится вместе с httpx/openai, но мало ли
            log.debug("certifi недоступен: %s", e)
        if IMAGE_CA_FILE:
            path = IMAGE_CA_FILE if os.path.isabs(IMAGE_CA_FILE) else os.path.join(BOT_DIR, IMAGE_CA_FILE)
            try:
                contexts.append((path, ssl.create_default_context(cafile=path)))
            except Exception as e:
                log.warning("IMAGE_CA_FILE %s: %s", path, e)
        if IMAGE_SSL_INSECURE_FALLBACK:
            contexts.append(("без проверки сертификата", ssl._create_unverified_context()))
        _ssl_contexts = contexts
    return _ssl_contexts


def is_cert_error(e):
    return isinstance(e, ssl.SSLCertVerificationError) or (
        isinstance(e, urllib.error.URLError) and isinstance(e.reason, ssl.SSLCertVerificationError))


def download_image(url):
    """Скачивает картинку: (данные, mime). При ошибке сертификата пробует следующий
    контекст SSL. Исключение — если скачать не удалось."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    contexts = ssl_contexts() if url.lower().startswith("https:") else [("http", None)]
    for i, (name, ctx) in enumerate(contexts):
        try:
            with urllib.request.urlopen(req, timeout=IMAGE_DOWNLOAD_TIMEOUT, context=ctx) as r:
                data = r.read(IMAGE_MAX_BYTES + 1)
                mime = r.headers.get_content_type() or "image/jpeg"
        except Exception as e:
            if is_cert_error(e) and i + 1 < len(contexts):
                log.info("картинка: сертификат не проверился (%s), пробую: %s",
                         getattr(e, "reason", e), contexts[i + 1][0])
                continue
            raise
        if i:
            log.debug("картинка скачана, сертификаты: %s", name)
        if len(data) > IMAGE_MAX_BYTES:
            raise ValueError(f"картинка больше {IMAGE_MAX_BYTES} байт")
        return data, mime
    raise RuntimeError("нет контекста SSL")


@traced
def _image_part(url):
    """Часть запроса с картинкой или None, если картинку не удалось скачать.
    Ссылку модели передаём только при IMAGE_MODE = "url" или IMAGE_URL_FALLBACK: сервер
    прокси, скачивая картинку с серверов ВК, получал 429 и весь запрос падал с 400."""
    if IMAGE_MODE != "base64":
        return {"type": "image_url", "image_url": {"url": url}}
    if url in _image_cache:
        _image_cache.move_to_end(url)
        return _image_cache[url]
    try:
        data, mime = download_image(url)
    except Exception as e:
        if IMAGE_URL_FALLBACK:
            log.warning("скачивание картинки: %s — передаю ссылкой", e)
            return {"type": "image_url", "image_url": {"url": url}}
        log.warning("скачивание картинки: %s — картинка пропущена", e)
        return None
    part = {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{base64.b64encode(data).decode()}"}}
    _image_cache[url] = part
    while len(_image_cache) > IMAGE_CACHE_SIZE:
        _image_cache.popitem(last=False)
    return part


def _rate_limit():
    """Не чаще одного запроса за LLM_MIN_INTERVAL_SEC: запросы к API платные."""
    global _last_llm_call
    wait = _last_llm_call + LLM_MIN_INTERVAL_SEC - time.time()
    if wait > 0:
        time.sleep(wait)
    _last_llm_call = time.time()


THINK_RE = re.compile(r"^\s*<think>(.*?)</think>\s*", re.DOTALL)


def extract_reasoning(message, answer):
    """(ответ, рассуждение): рассуждение — из reasoning_content / reasoning (DeepSeek, OpenRouter
    и т. п.) или из «<think>…</think>» в начале ответа (тогда из ответа оно убирается)."""
    reasoning = None
    extra = getattr(message, "model_extra", None) or {}
    for name in ("reasoning_content", "reasoning"):
        value = getattr(message, name, None) or extra.get(name)
        if isinstance(value, str) and value.strip():
            reasoning = value.strip()
            break
    m = THINK_RE.match(answer or "")
    if m:
        reasoning = reasoning or m.group(1).strip()
        answer = answer[m.end():]
    return (answer or "").strip(), reasoning


def _llm_request(system, content, max_tokens, extra_body=None):
    """Один запрос к API: (ответ, finish_reason, рассуждение). Пишет в лог строку с расходом токенов."""
    _rate_limit()
    kwargs = {"extra_body": extra_body} if extra_body else {}
    t0 = time.time()
    resp = llm_client.chat.completions.create(
        model=MODEL_NAME,
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": content},
        ],
        **kwargs,
    )
    choice = resp.choices[0]
    answer, reasoning_text = extract_reasoning(choice.message, getattr(choice.message, "content", None))
    finish = getattr(choice, "finish_reason", None)
    usage = getattr(resp, "usage", None)
    details = getattr(usage, "completion_tokens_details", None)
    reasoning = getattr(details, "reasoning_tokens", None)
    log.info("LLM: %.1f с, finish=%s, рассуждения %s, токены: вход %s, выход %s%s (лимит %d), "
             "ответ %d символов%s", time.time() - t0, finish, "вкл" if REASONING_ON else "выкл",
             getattr(usage, "prompt_tokens", "?"), getattr(usage, "completion_tokens", "?"),
             f", из них рассуждение {reasoning}" if reasoning else "", max_tokens, len(answer),
             f", текст рассуждения {len(reasoning_text)} символов" if reasoning_text else "")
    if DEBUG_MODE:
        log.debug("LLM system:\n%s\nLLM user:\n%s\nLLM рассуждение:\n%s\nLLM ответ:\n%s", system,
                  content if isinstance(content, str) else content[0]["text"], reasoning_text, answer)
    return answer, finish, reasoning_text


def reasoning_body():
    """Параметры, которые включают или выключают рассуждение модели (REASONING_ON/OFF_BODY)."""
    if not REASONING_PARAM_OK:
        return None
    return REASONING_ON_BODY if REASONING_ON else REASONING_OFF_BODY


def is_param_error(e):
    """Похоже, что API не понял параметр запроса (400/422 или ошибка про thinking)."""
    return getattr(e, "status_code", None) in (400, 422) or "thinking" in str(e).lower()


def _request(system, content, max_tokens):
    """Запрос с REASONING_*_BODY; если API отверг этот параметр — повтор без него, и до конца
    работы бота он больше не передаётся (рассуждение тогда — как решит модель)."""
    global REASONING_PARAM_OK
    thinking = reasoning_body()
    body = dict(LLM_EXTRA_BODY or {})
    body.update(thinking or {})
    try:
        return _llm_request(system, content, max_tokens, body or None)
    except Exception as e:
        if not thinking or not is_param_error(e):
            raise
        log.warning("API отверг параметр %s (%s) — повтор без него", thinking, shorten(str(e), 200))
        result = _llm_request(system, content, max_tokens, dict(LLM_EXTRA_BODY or {}) or None)
        REASONING_PARAM_OK = False
        log.warning("Параметр рассуждения больше не передаётся: модель рассуждает как решит сама; "
                    "поправьте REASONING_ON_BODY / REASONING_OFF_BODY в настройках")
        return result


@traced
def llm_call(system, user_text, max_tokens=MAX_TOKENS_DEFAULT, images=(), retry_empty=True):
    """
    Запрос к LLM. Возвращает ответ или "" — тогда причина в LAST_LLM_ERROR.
    Рассуждение модели (если она его вернула) добавляется в LLM_REASONINGS.
    Пустой ответ с finish_reason="length" — модель-«рассуждалка» потратила весь лимит
    токенов на рассуждение (токены списаны, а текста нет): если retry_empty — один повтор
    с большим лимитом (у /мысль повтора нет). Ошибка API с картинками — повтор без картинок.
    """
    global LAST_LLM_ERROR
    LAST_LLM_ERROR = ""
    images = list(dict.fromkeys(images))[:MAX_IMAGES_PER_REQUEST]
    content = user_text
    if images:
        loaded = [(u, p) for u, p in ((u, _image_part(u)) for u in images) if p]
        if len(loaded) < len(images):
            user_text += f"\n\n({len(images) - len(loaded)} картинок не удалось скачать.)"
        images = [u for u, _ in loaded]     # для повторов — только скачавшиеся (они в кэше)
        if loaded:
            content = [{"type": "text", "text": user_text}] + [p for _, p in loaded]
        else:
            content = user_text
    try:
        answer, finish, reasoning = _request(system, content, max_tokens)
    except Exception as e:
        log.error("llm_call: %s", e)
        LAST_LLM_ERROR = f"ошибка API: {shorten(str(e), 300)}"
        if images:
            # модель или прокси не приняли картинки — пробуем хотя бы текстом
            answer = llm_call(system, user_text + "\n\n(Картинки не удалось передать.)", max_tokens,
                              retry_empty=retry_empty)
            if not answer:
                LAST_LLM_ERROR = f"ошибка API: {shorten(str(e), 300)}; без картинок: {LAST_LLM_ERROR}"
            return answer
        return ""
    if reasoning:
        LLM_REASONINGS.append(reasoning)
    if answer:
        if finish == "length":
            log.warning("ответ обрезан лимитом %d токенов", max_tokens)
        return answer
    if finish == "length":
        LAST_LLM_ERROR = (f"модель израсходовала весь лимит ({max_tokens} токенов) на рассуждение "
                          "и не успела написать ответ")
        if retry_empty and EMPTY_ANSWER_RETRY_TOKENS and max_tokens < EMPTY_ANSWER_RETRY_TOKENS:
            log.warning("пустой ответ: %s — повтор с лимитом %d", LAST_LLM_ERROR, EMPTY_ANSWER_RETRY_TOKENS)
            answer = llm_call(system, user_text, EMPTY_ANSWER_RETRY_TOKENS, images)
            if not answer and LAST_LLM_ERROR.startswith("модель израсходовала"):
                LAST_LLM_ERROR = (f"модель израсходовала весь лимит на рассуждение даже при "
                                  f"{EMPTY_ANSWER_RETRY_TOKENS} токенах")
            return answer
    else:
        LAST_LLM_ERROR = f"модель вернула пустой ответ (finish_reason={finish})"
    log.warning("пустой ответ: %s", LAST_LLM_ERROR)
    return ""


# ============ Режимы беседы: рассуждения, база знаний ============
def reasoning_enabled(peer_id):
    raw = db_state_get(f"reasoning_{peer_id}")
    return REASONING_DEFAULT if raw is None else raw == "1"


def rag_mode(peer_id):
    raw = db_state_get(f"rag_{peer_id}")
    return RAG_MODE_DEFAULT if raw is None else raw == "1"


def chunks_limit(peer_id):
    """Сколько найденных фрагментов базы знаний передавать модели (/лимит чанков)."""
    return _limit_get(f"chunks_{peer_id}", RAG_TOP_K)[0]


class with_reasoning:
    """Ответ беседы: запросы к LLM внутри — с рассуждением или без (режим беседы),
    LLM_REASONINGS собирает рассуждения всех запросов этого ответа."""

    def __init__(self, peer_id):
        self.on = reasoning_enabled(peer_id)

    def __enter__(self):
        global REASONING_ON
        self.prev = REASONING_ON
        REASONING_ON = self.on
        LLM_REASONINGS.clear()
        return self

    def __exit__(self, *exc):
        global REASONING_ON
        REASONING_ON = self.prev
        return False


class quiet_llm:
    """Вспомогательный запрос внутри ответа (сжатие истории): без рассуждения, и его
    рассуждение (если модель всё же его вернула) в ответ не попадает."""

    def __enter__(self):
        global REASONING_ON
        self.prev, self.n = REASONING_ON, len(LLM_REASONINGS)
        REASONING_ON = False
        return self

    def __exit__(self, *exc):
        global REASONING_ON
        REASONING_ON = self.prev
        del LLM_REASONINGS[self.n:]
        return False


class NoAnswer(Exception):
    """produce() для llm_reply: ответа нет по понятной причине — служебное сообщение с этим текстом."""


def reasoning_block():
    """«#рассуждения#\n…\n#рассуждения#» — рассуждения модели за текущий ответ."""
    text = "\n\n".join(r for r in LLM_REASONINGS if r)
    if not text:
        text = "(модель не вернула рассуждение)"
    elif len(text) > REASONING_MAX_CHARS:
        half = REASONING_MAX_CHARS // 2
        text = f"{text[:half].rstrip()}\n[…сокращено {len(text) - 2 * half} символов…]\n{text[-half:].lstrip()}"
    return f"{REASONING_MARK}\n{text}\n{REASONING_MARK}"


REASONING_BLOCK_RE = re.compile(rf"^\s*{re.escape(REASONING_MARK)}.*?{re.escape(REASONING_MARK)}\s*",
                                re.DOTALL)


def strip_bot_extras(text):
    """Ответ бота для истории: без блока рассуждения и без пометки «Поиск … не дал результата»
    (иначе модель начинает подражать им в следующих ответах)."""
    text = REASONING_BLOCK_RE.sub("", text or "")
    if text.lstrip().startswith(REASONING_MARK):
        return ""                  # первая часть длинного ответа: всё — рассуждение
    if REASONING_MARK in text:     # следующая часть: конец рассуждения и начало ответа
        text = text.split(REASONING_MARK, 1)[1]
    text = text.strip()
    if text.startswith(RAG_NO_RESULT_PREFIX):
        text = text[len(RAG_NO_RESULT_PREFIX):].lstrip()
    return text


def clean_answer(text):
    """Убирает «Грань разумного:» в начале, если модель подписалась."""
    return re.sub(rf"^\s*(\[[^\]]*\]\s*)?({re.escape(BOT_NAME)}|Грань)\s*:\s*", "", text or "").strip()


@traced
def ask_gran(peer_id, requests, context_rows, images=(), fragments=None):
    """Один запрос к LLM на одно или несколько обращений, накопившихся за BATCH_WAIT_SEC.
    fragments — найденные фрагменты базы знаний (режим «/раг вкл»): тогда ответ только по ним
    или rag.NO_ANSWER, если ответа в них нет."""
    system = build_system_prompt(db_get_role(peer_id))
    if fragments:
        system += "\n\n" + rag.CHAT_RULES
    ctx = build_history(peer_id, context_rows, chars_limit(peer_id))
    log.debug("КОНТЕКСТ peer=%s:\n%s", peer_id, ctx)

    history = f"ИСТОРИЯ:\n{ctx}\n\n" if ctx else "Истории нет — это первое сообщение.\n\n"
    new = "\n".join(format_line(r.from_id, False, r.text, r.ts) for r in requests)
    if len(requests) == 1:
        task = f"НОВОЕ СООБЩЕНИЕ:\n{new}\n\n"
    else:
        task = (f"НОВЫЕ СООБЩЕНИЯ (их {len(requests)}, ответь на все одним сообщением, "
                f"обращаясь к авторам по имени):\n{new}\n\n")
    knowledge = f"ФРАГМЕНТЫ БАЗЫ ЗНАНИЙ:\n\n{fragments}\n\n" if fragments else ""
    prompt = (
        "Ниже история чата (старые сообщения сверху, новые снизу)"
        + (", фрагменты базы знаний" if fragments else "") + " и новые "
        "сообщения, на которые надо ответить.\n\n"
        f"{history}{knowledge}{task}"
        + ("К запросу приложены картинки из новых сообщений и последних сообщений истории.\n\n"
           if images else "")
        + ("Ответь как Грань, только по фрагментам базы знаний (с номерами фрагментов), "
           f"или одним словом {rag.NO_ANSWER}." if fragments else "Ответь как Грань.")
    )
    max_tokens = max(RAG_MAX_TOKENS, tokens_limit(peer_id)) if fragments else tokens_limit(peer_id)
    return clean_answer(llm_call(system, prompt, max_tokens=max_tokens, images=images))


@traced
def summarize_rows(peer_id, rows):
    system = build_system_prompt(db_get_role(peer_id))
    ctx = format_context(rows, max_chars=SUMMARY_MAX_INPUT * 200)
    prompt = (
        "Ниже — история чата. Сделай краткий, но содержательный пересказ: "
        "о чём говорили, ключевые темы, кто что утверждал, к чему пришли.\n\n"
        f"{ctx}"
    )
    return llm_call(system, prompt, max_tokens=max(SUMMARY_MAX_TOKENS, tokens_limit(peer_id)))


@traced
def summarize_text(peer_id, text, images=()):
    system = "Ты — полезный помощник, который делает краткое резюме текста."
    prompt = f"Перескажи суть следующего текста кратко и по делу:\n\n{text}"
    return llm_call(system, prompt, max_tokens=max(1200, tokens_limit(peer_id)), images=images)


# ============ Парсинг команд ============
def has_trigger(text):
    return bool(TRIGGER_RE.search(text or ""))


def is_reply_to_bot(message):
    rp = message.get("reply_message")
    return bool(rp) and rp.get("from_id") == BOT_GROUP_ID


def is_addressed_to_bot(message):
    """Обращение к боту: слово «грань», reply на сообщение бота или личка (если разрешена)."""
    return (has_trigger(message.get("text"))
            or is_reply_to_bot(message)
            or (ALLOW_PRIVATE_MESSAGES and is_private(message.get("peer_id", 0))))


def should_respond(message_ts):
    return (int(now()) - message_ts) <= FRESHNESS_MINUTES * 60


ROLE_RE = re.compile(r"/роль(?!\w)", re.IGNORECASE)
ADDRESS_PREFIX_RE = re.compile(r"\s*(?:грань\b[\s,.:!—-]*)?", re.IGNORECASE)


def extract_role(text):
    """
    Роль из «/роль (текст)» или «/роль текст»: (текст, закрыта) или None.
    закрыта: True — скобка закрыта; False — «(» без «)» (ВК разрезал длинную роль на
    несколько сообщений, продолжение — в следующих); None — форма без скобок (она только
    в начале сообщения, можно после «Грань,»). «/роль» без текста — ("", None).
    Скобки внутри роли допустимы: роль кончается последней «)», если она закрывает «(».
    """
    text = text or ""
    m = ROLE_RE.search(text)
    if not m:
        return None
    rest = text[m.end():].lstrip()
    if rest.startswith("("):
        body = rest[1:]
        closed = body.rstrip().endswith(")") or body.count(")") > body.count("(")
        if closed:
            body = body[:body.rindex(")")]
        return body.strip(), closed
    if not ADDRESS_PREFIX_RE.fullmatch(text[:m.start()]):
        return None
    return rest.strip(), None


def is_summary_request(text):
    low = (text or "").lower()
    return ("грань, перескажи" in low) or ("грань, о чём тут" in low) \
        or ("грань, о чем тут" in low)


def is_stop_request(text):
    return STOP_PHRASE in (text or "").lower()


def parse_start_time(s, t=None):
    """
    Дата начала для «/пересказ N от …»: «12:00 16.10.2026», «16.10.2026 12:00»,
    «16.10.2026», «16.10» (этот год), «12:00» (сегодня), год можно двумя цифрами.
    Дата без года или время без даты в будущем — значит, прошлый год / вчера.
    Возвращает timestamp или None.
    """
    cur = datetime.fromtimestamp(now() if t is None else t)
    tm = re.search(r"\b(\d{1,2}):(\d{2})\b", s or "")
    dm = re.search(r"\b(\d{1,2})\.(\d{1,2})(?:\.(\d{4}|\d{2}))?\b", s or "")
    if not tm and not dm:
        return None
    hour, minute = (int(tm.group(1)), int(tm.group(2))) if tm else (0, 0)
    try:
        if dm:
            year = int(dm.group(3)) if dm.group(3) else cur.year
            year += 2000 if year < 100 else 0
            dt = datetime(year, int(dm.group(2)), int(dm.group(1)), hour, minute)
            if not dm.group(3) and dt > cur:
                dt = dt.replace(year=dt.year - 1)
        else:
            dt = cur.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if dt > cur:
                dt -= timedelta(days=1)
    except ValueError:
        return None
    return int(dt.timestamp())


SPLIT_COMMANDS = {"роль", "запомни"}    # их текст может прийти несколькими сообщениями
RESET_ROLE_RE = re.compile(r"(?:^|\s)/сбросить\s+роль\b", re.IGNORECASE)
REMEMBER_RE = re.compile(r"\bгрань\b[\s,.:!—-]*запомни\b|(?:^|\s)/запомни\b", re.IGNORECASE)
RAG_RE = re.compile(r"(?<![\w/])(/?)(раг|rag)(?!\w)", re.IGNORECASE)
RAG_MODE_RE = re.compile(r"(вкл(?:ючить|ючи)?|on|выкл(?:ючить|ючи)?|off)[\s.!]*", re.IGNORECASE)
_ON_OFF = r"(?:\s*[:=]?\s*(вкл\w*|выкл\w*|on|off))?"
_NUMBER = r"\d(?:[\d \u00a0\u2009\u202f]*\d)?(?:\s*(?:к|k|тыс\.?))?(?!\w)"   # 3000, 30 000, 30к, 2k, 3 тыс
_LIMIT_ARG = r"(?:\s*[:=]?\s*(" + _NUMBER + r"|сброс|стандарт))?"
COMMAND_PATTERNS = [
    ("помощь", re.compile(r"(?:^|\s)/помощь\b", re.IGNORECASE)),
    ("пауза", re.compile(r"(?:^|\s)/пауза\b(?:\s+(\d+))?", re.IGNORECASE)),
    ("лимит токенов", re.compile(r"(?:^|\s)/лимит\s+токенов\b" + _LIMIT_ARG, re.IGNORECASE)),
    ("лимит символов", re.compile(r"(?:^|\s)/лимит\s+символов\b" + _LIMIT_ARG, re.IGNORECASE)),
    ("сбросить лимит", re.compile(r"(?:^|\s)/сбросить\s+лимит\s+сообщений\b", re.IGNORECASE)),
    ("забудь", re.compile(r"(?:^|\s)/забудь\b", re.IGNORECASE)),
    ("пересказ", re.compile(r"(?:^|\s)/пересказ\b(?:\s+(\d+))?(?:\s+(?:от|с)\s+(.+))?",
                            re.IGNORECASE | re.DOTALL)),
    ("мысль", re.compile(r"(?:^|\s)/мысль\b(?:\s+(подробно))?", re.IGNORECASE)),
    ("лимит чанков", re.compile(r"(?:^|\s)/лимит\s+(?:чанков|фрагментов)\b" + _LIMIT_ARG, re.IGNORECASE)),
    ("сброс контекста", re.compile(r"(?:^|\s)/сброс(?:ить)?\s+контекст[аы]?\b(?:\s*[:=]?\s*(\d+))?",
                                   re.IGNORECASE)),
    ("автоконтекст", re.compile(r"(?:^|\s)/авто-?\s*контекст\b(.*)", re.IGNORECASE)),
    ("контекст", re.compile(r"(?:^|\s)/контекст\b(?:\s*[:=]?\s*(\d+|сброс|стандарт))?", re.IGNORECASE)),
    ("рассуждения", re.compile(r"(?:^|\s)/рассуждени[яе]\b" + _ON_OFF, re.IGNORECASE)),
]


def parse_rag(text, addressed=True):
    """«Грань, РАГ вопрос» / «/раг вопрос» → ("раг", вопрос); «/раг вкл» / «/раг выкл» →
    ("раг режим", True/False); иначе None.
    Без «/» слово должно быть написано заглавными («РАГ») или стоять сразу после «Грань,»,
    и сообщение должно быть обращением к боту — чтобы не срабатывать на случайное «раг»."""
    for m in RAG_RE.finditer(text):
        slash, word = m.group(1), m.group(2)
        after_name = bool(ADDRESS_PREFIX_RE.fullmatch(text[:m.start()])) and has_trigger(text[:m.start()])
        if slash or ((word.isupper() or after_name) and addressed):
            query = re.sub(r"^[\s,.:;!?+—-]+", "", text[m.end():]).strip()   # «РАГ+» — как «РАГ»
            mode = RAG_MODE_RE.fullmatch(query)
            if mode:
                return ("раг режим", is_on(mode.group(1)))
            return ("раг", query)
    return None


def is_on(word):
    """«вкл», «включить», «on» → True; «выкл», «off» → False."""
    return word.lower().startswith(("вкл", "on"))


def parse_command(text, addressed=True):
    """(имя, аргумент) первой найденной команды или None.
    addressed — сообщение обращено к боту (нужно для «РАГ» без косой черты)."""
    text = text or ""
    if is_stop_request(text):
        return ("стоп", None)
    if RESET_ROLE_RE.search(text):
        return ("сбросить роль", None)
    role = extract_role(text)
    if role is not None:
        return ("роль", role[0] or None)    # None — показать текущую роль
    m = REMEMBER_RE.search(text)
    if m:
        return ("запомни", text[m.end():].strip())
    rag_cmd = parse_rag(text, addressed)
    if rag_cmd:
        return rag_cmd
    for name, rx in COMMAND_PATTERNS:
        m = rx.search(text)
        if not m:
            continue
        if name == "пересказ":
            return (name, (int(m.group(1)) if m.group(1) else None,
                           m.group(2).strip() if m.group(2) else None))
        if name == "мысль":
            return (name, bool(m.group(1)))
        if name == "рассуждения":
            return (name, is_on(m.group(1)) if m.group(1) else None)
        if name == "автоконтекст":
            return (name, m.group(1).strip() or None)
        return (name, m.group(1) if m.groups() else None)
    return None


def quoted_content(message):
    """Текст и картинки reply/пересланных сообщений — для «грань, перескажи»."""
    images, parts = [], []
    if message.get("reply_message"):
        parts.append(render_message(message["reply_message"], images)[0])
    for f in message.get("fwd_messages") or []:
        parts.append(f"{display_name(f.get('from_id', 0))}: {render_message(f, images)[0]}")
    for a in message.get("attachments") or []:
        if a.get("type") == "wall":
            parts.append("\n".join(render_wall(a["wall"], images, 0)))
    text = "\n\n".join(p for p in parts if p.strip())
    return text, images


# ============ Команды ============
# (имя, как вызвать, кто может, что делает) — по этой таблице строится /помощь
HELP = [
    ("обращение", "«Грань, …» или reply на сообщение бота", "все",
     f"ответ бота; обращения за {BATCH_WAIT_SEC} с собираются и получают один общий ответ"),
    ("перескажи", "«Грань, перескажи» / «Грань, о чём тут» (reply, пересланные или пост)", "все",
     "пересказ сообщения, на которое ответили, пересланных или поста"),
    ("пересказ", "/пересказ N", "все; больше "
     f"{SUMMARY_FREE_MAX} сообщений — админ", "пересказ последних N сообщений беседы"),
    ("пересказ от", "/пересказ N от ЧЧ:ММ ДД.ММ.ГГГГ", "все; больше "
     f"{SUMMARY_FREE_MAX} сообщений — админ",
     "пересказ N сообщений начиная с даты; недостающая история догружается из ВК"),
    ("мысль", "/мысль", "все", "случайная философская мысль"),
    ("мысль подробно", "/мысль подробно", "все",
     "мысль со всей цепочкой: случайные слова → образ и роль → вопрос → ответ"),
    ("пауза", "/пауза [минуты]", "все",
     f"не отвечать {PAUSE_DEFAULT_MINUTES} мин (до {PAUSE_MAX_MINUTES}), потом ответить на всё "
     "накопившееся; «/пауза 0» — снять паузу"),
    ("забудь", "/забудь", "все", "начать контекст беседы с чистого листа (история в БД остаётся)"),
    ("контекст", "/контекст N", "все",
     f"сколько последних сообщений истории передавать модели (стандарт {CONTEXT_SIZE}, до "
     f"{CONTEXT_SIZE_MAX}); «/контекст» — показать, «/контекст сброс» — к стандарту"),
    ("автоконтекст", "/автоконтекст + пересланное сообщение (или reply на него)", "все",
     "контекст — от этого сообщения до текущего (сообщение должно быть в истории этой беседы)"),
    ("автоконтекст период", "/автоконтекст 2 ч (м, ч, д, нед)", "все",
     "контекст — все сообщения за последний период (число считается по истории)"),
    ("сброс контекста", "/сброс контекста [N]", "все",
     "отменить автоконтекст: вернуться к заданному «/контекст» размеру (или стандарту); "
     "с числом — сразу задать N"),
    ("раг", "«Грань, РАГ вопрос» или «/раг вопрос»", "все",
     f"разовый ответ строго по книгам из папки RAG (с источниками); если там ничего нет — "
     f"«{RAG_NOTHING_FOUND}», без выдумок; «/раг» без вопроса — что есть в базе знаний"),
    ("раг режим", "/раг вкл, /раг выкл", "все",
     "вкл — каждый ответ сначала ищется в базе знаний: нашлось — ответ только по фрагментам, "
     f"не нашлось — «{RAG_NO_RESULT_PREFIX}» и обычный ответ; выкл — ответы только по истории"),
    ("рассуждения", "/рассуждения вкл, /рассуждения выкл", "все",
     f"вкл — модель рассуждает перед ответом, рассуждение — в начале сообщения между строками "
     f"«{REASONING_MARK}»" + ("" if REASONING_DEFAULT else "; по умолчанию выключены")),
    ("запомни", "«Грань, запомни» + пересланные сообщения (или текст после «запомни»)",
     "админ" if REMEMBER_ADMIN_ONLY else "все",
     f"перезаписать {REMEMBER_FILE} в базе знаний и переиндексировать его"),
    ("роль", "/роль (текст)", "все", "задать боту роль в этой беседе; «/роль» — показать текущую"),
    ("сбросить роль", "/сбросить роль", "все", f"вернуть роль по умолчанию (файл {ROLE_FILE})"),
    ("помощь", "/помощь", "все", "этот список и текущие настройки беседы"),
    ("лимит токенов", "/лимит токенов N / сброс", "админ",
     f"лимит длины ответа в токенах (стандарт {MAX_TOKENS_DEFAULT}"
     + (f", сам возвращается через {TOKEN_LIMIT_RESET_MINUTES} мин" if TOKEN_LIMIT_RESET_MINUTES
        else "") + ")"),
    ("лимит символов", "/лимит символов N / сброс", "админ",
     f"сколько символов истории передавать модели (стандарт {MAX_CONTEXT_CHARS}); "
     "что не влезло — сжимается в пересказ"),
    ("лимит чанков", "/лимит чанков N / сброс", "админ",
     f"сколько найденных фрагментов базы знаний (по ~{rag.CHUNK_CHARS} символов) передавать "
     f"модели (стандарт {RAG_TOP_K}, до {RAG_TOP_K_MAX})"),
    ("сбросить лимит", "/сбросить лимит сообщений", "админ",
     f"обнулить дневной лимит ответов ({DAILY_MESSAGE_LIMIT} в сутки)"),
    ("стоп", f"«{STOP_PHRASE.capitalize()}»", "админ",
     "остановить бота" + ("; или «0» и Enter в терминале, где запущен бот" if TERMINAL_STOP else "")),
]
ADMIN_COMMANDS = {"лимит токенов", "лимит символов", "лимит чанков", "сбросить лимит", "стоп"}
LLM_COMMANDS = {"пересказ", "мысль", "перескажи", "раг"}   # на паузе откладываются, расходуют лимит


def on_off(flag):
    return "включены" if flag else "выключены"


def help_text(peer_id):
    lines = ["Команды бота «Грань»:", ""]
    for _name, usage, who, what in HELP:
        lines.append(f"• {usage} — {what}. Кто: {who}.")
    st = daily_state(peer_id)
    lines += [
        "",
        "Админ — админ или создатель беседы (боту нужны права админа в беседе); "
        "в личных сообщениях собеседнику доступны все команды, кроме остановки бота "
        "(её — только ADMIN_IDS из .env).",
        "",
        "Сейчас в этой беседе:",
        f"• лимит токенов ответа: {tokens_limit(peer_id)}" + (
            f" (до {datetime.fromtimestamp(tokens_reset_at(peer_id)):%H:%M}, потом {MAX_TOKENS_DEFAULT})"
            if tokens_reset_at(peer_id) else ""),
        f"• лимит символов истории: {chars_limit(peer_id)}",
        f"• контекст: {context_text(peer_id)}",
        f"• база знаний в ответах (/раг вкл): {'да' if rag_mode(peer_id) else 'нет'}; "
        f"фрагментов в запрос: до {chunks_limit(peer_id)}",
        f"• рассуждения: {on_off(reasoning_enabled(peer_id))}",
        "• ответов сегодня: " + (f"{st['count']} из {DAILY_MESSAGE_LIMIT}" if DAILY_MESSAGE_LIMIT > 0
                                 else f"{st['count']} (без лимита)"),
    ]
    if is_paused(peer_id):
        lines.append(f"• пауза до {datetime.fromtimestamp(peer_state(peer_id).paused_until):%H:%M:%S}")
    return "\n".join(lines)


def needs_admin(cmd):
    name, arg = cmd
    if name in ADMIN_COMMANDS:
        return True
    if name == "запомни":
        return REMEMBER_ADMIN_ONLY
    return name == "пересказ" and (arg[0] or SUMMARY_DEFAULT_COUNT) > SUMMARY_FREE_MAX


def can_stop(peer_id, user_id):
    """Остановить бота из чата: ADMIN_IDS или админ беседы (в личке — только ADMIN_IDS)."""
    if user_id in ADMIN_IDS:
        return True
    return not is_private(peer_id) and is_admin(peer_id, user_id)


@traced
def run_command(req):
    """Выполняет команду. Служебные — сразу; требующие LLM на паузе ждут её конца."""
    global STOP_REQUESTED
    peer_id, message = req.peer_id, req.message
    name, arg = req.command

    allowed = can_stop(peer_id, req.from_id) if name == "стоп" else (
        not needs_admin(req.command) or is_admin(peer_id, req.from_id))
    if not allowed:
        what = (f"Пересказ больше {SUMMARY_FREE_MAX} сообщений доступен" if name == "пересказ"
                else "Остановить бота может" if name == "стоп" else "Эта команда доступна")
        log.info("peer=%s: команда «%s» от %s отклонена — не админ", peer_id, name, req.from_id)
        send_system(peer_id, f"{what} только админ{'у' if name != 'стоп' else ''} беседы.",
                    reply_to_msg=message)
        return

    if name in LLM_COMMANDS:
        if is_paused(peer_id):
            enqueue(req)
        else:
            run_llm_command(req)
        return

    if name == "стоп":
        vk_send(peer_id, "Останавливаюсь.", reply_to_msg=message)
        log.info("Команда остановки от %s в peer=%s", req.from_id, peer_id)
        db_state_set("stopped_at", int(now()))
        STOP_REQUESTED = True
    elif name == "роль":
        if arg is None:
            current = db_get_role(peer_id)
            source = ("задана в этой беседе" if DB.execute("SELECT 1 FROM roles WHERE peer_id=?",
                                                           (peer_id,)).fetchone()
                      else f"по умолчанию, {ROLE_FILE}" if os.path.exists(role_file_path()) else "по умолчанию")
            send_system(peer_id, f"Роль ({source}):\n{current}\n\nИзменить: «/роль (текст)», "
                                 "вернуть роль по умолчанию: «/сбросить роль».", reply_to_msg=message)
            return
        db_set_role(peer_id, arg)
        log.info("peer=%s: роль обновлена (%d символов)", peer_id, len(arg))
        vk_send(peer_id, "Роль обновлена." + (req.note or ""), reply_to_msg=message)
    elif name == "сбросить роль":
        db_del_role(peer_id)
        send_system(peer_id, f"Роль сброшена — действует роль по умолчанию"
                             f"{' (' + ROLE_FILE + ')' if os.path.exists(role_file_path()) else ''}.",
                    reply_to_msg=message)
    elif name == "запомни":
        cmd_remember(req, arg)
    elif name == "забудь":
        # сбросить контекст беседы, история в БД остаётся
        db_state_set(f"forget_{peer_id}", req.ts + 1)
        db_state_del(f"ctxsum_{peer_id}")
        vk_send(peer_id, "Хорошо, начинаю с чистого листа.", reply_to_msg=message)
    elif name == "помощь":
        send_system(peer_id, help_text(peer_id), reply_to_msg=message)
    elif name == "пауза":
        cmd_pause(req, arg)
    elif name == "контекст":
        cmd_context(req, arg)
    elif name == "автоконтекст":
        cmd_autocontext(req, arg)
    elif name == "сброс контекста":
        cmd_reset_context(req, arg)
    elif name == "раг режим":
        db_state_set(f"rag_{peer_id}", "1" if arg else "0")
        log.info("peer=%s: режим базы знаний %s (от %s)", peer_id, "вкл" if arg else "выкл", req.from_id)
        send_system(peer_id, (f"Режим базы знаний включён: каждый ответ сначала ищется в базе знаний "
                              f"(до {chunks_limit(peer_id)} фрагментов). Нашлось — отвечаю только по "
                              f"фрагментам, не нашлось — пишу «{RAG_NO_RESULT_PREFIX}» и отвечаю как "
                              "обычно. Выключить: «/раг выкл».") if arg else
                    "Режим базы знаний выключен: отвечаю только по истории беседы. Разовый вопрос "
                    "к базе знаний — «/раг вопрос».", reply_to_msg=message)
    elif name == "рассуждения":
        cmd_reasoning(req, arg)
    elif name == "лимит токенов":
        cmd_limit(req, arg, "tokens", "Лимит выходных токенов", MAX_TOKENS_DEFAULT,
                  MAX_TOKENS_CAP, tokens_limit)
    elif name == "лимит символов":
        cmd_limit(req, arg, "chars", "Лимит символов истории", MAX_CONTEXT_CHARS,
                  MAX_CONTEXT_CHARS_CAP, chars_limit)
    elif name == "лимит чанков":
        cmd_limit(req, arg, "chunks", "Лимит фрагментов базы знаний", RAG_TOP_K,
                  RAG_TOP_K_MAX, chunks_limit)
    elif name == "сбросить лимит":
        db_state_del(daily_key(peer_id))
        send_system(peer_id, f"Дневной лимит сообщений сброшен: 0 из {DAILY_MESSAGE_LIMIT}.",
                    reply_to_msg=message)


def cmd_pause(req, arg):
    st = peer_state(req.peer_id)
    minutes = PAUSE_DEFAULT_MINUTES if arg is None else min(int(arg), PAUSE_MAX_MINUTES)
    if minutes <= 0:
        st.paused_until = now() if st.paused_until > now() else 0.0  # process_pending ответит сразу
        send_system(req.peer_id, "Пауза снята.", reply_to_msg=req.message)
        return
    st.paused_until = now() + minutes * 60
    send_system(req.peer_id, f"Пауза {minutes} мин: пока не отвечаю, потом отвечу "
                             "на всё, что накопится.", reply_to_msg=req.message)


def context_note(peer_id, value):
    """Предупреждение, если столько сообщений не влезет в лимит символов истории."""
    rows = db_get_context(peer_id, value)
    size = _size(context_lines(rows))
    if size > chars_limit(peer_id):
        return (f" Сейчас это ~{size} символов — больше лимита истории ({chars_limit(peer_id)}): "
                "старая часть будет сжиматься в пересказ (поднять лимит: «/лимит символов N», админ).")
    return ""


def cmd_context(req, arg):
    """/контекст — показать; /контекст N — задать; /контекст сброс — к стандарту."""
    peer_id = req.peer_id
    if arg is None:
        send_system(peer_id, f"Контекст: {context_text(peer_id)}. Изменить: «/контекст N», "
                             "по пересланному сообщению или за период: «/автоконтекст», "
                             "вернуть стандарт: «/контекст сброс».", reply_to_msg=req.message)
        return
    if not arg.isdigit():
        db_state_del(f"ctx_{peer_id}")
        db_state_del(f"autoctx_{peer_id}")
        log.info("peer=%s: контекст возвращён к стандарту (от %s)", peer_id, req.from_id)
        send_system(peer_id, f"Контекст возвращён к стандарту: {CONTEXT_SIZE} сообщений.",
                    reply_to_msg=req.message)
        return
    value = set_context(peer_id, "ctx", int(arg))
    db_state_del(f"autoctx_{peer_id}")
    log.info("peer=%s: контекст = %d (от %s)", peer_id, value, req.from_id)
    send_system(peer_id, f"Контекст: {value} последних сообщений"
                + (f" (максимум {CONTEXT_SIZE_MAX})" if int(arg) > CONTEXT_SIZE_MAX else "")
                + "." + context_note(peer_id, value), reply_to_msg=req.message)


def cmd_autocontext(req, arg):
    """/автоконтекст + пересланное/reply — от этого сообщения; /автоконтекст 2 ч — за период."""
    peer_id, message = req.peer_id, req.message
    # сама команда тоже считается: в следующем ответе история — ровно от пересланного до неё
    targets = list(message.get("fwd_messages") or [])
    if message.get("reply_message"):
        targets.append(message["reply_message"])
    if arg:
        period = parse_period(arg)
        if period is None:
            send_system(peer_id, "Не понял период. Примеры: «/автоконтекст 2 ч», «/автоконтекст 30 мин», "
                                 "«/автоконтекст 1 д».", reply_to_msg=message)
            return
        start = int(now()) - period
        count = db_count_since(peer_id, start)
        what = f"за последние {fmt_period(period)}"
    elif targets:
        found = [f for f in (find_stored_message(peer_id, t) for t in targets) if f]
        if not found:
            send_system(peer_id, "Этого сообщения нет в истории беседы — автоконтекст работает только "
                                 "с сообщениями этого чата. Можно задать период: «/автоконтекст 2 ч» "
                                 "или число: «/контекст 50».", reply_to_msg=message)
            return
        rid, ts = min(found, key=lambda f: (f[1], f[0]))       # самое раннее из пересланных
        count = db_count_since(peer_id, ts, start_id=rid)
        what = f"от сообщения {fmt_ts(ts)}"
    else:
        send_system(peer_id, "Автоконтекст: перешлите сообщение из этой беседы (или ответьте на него) "
                             "с командой «/автоконтекст» — контекст будет от него до текущего; "
                             "«/автоконтекст 2 ч» — за последние 2 часа. Сейчас: "
                             f"{context_text(peer_id)}.", reply_to_msg=message)
        return
    if count < 1:
        send_system(peer_id, f"{what.capitalize()} сообщений в истории нет — контекст не изменён "
                             f"({context_text(peer_id)}).", reply_to_msg=message)
        return
    value = set_context(peer_id, "autoctx", count)
    log.info("peer=%s: автоконтекст %s — %d сообщений (поставлено %d, от %s)", peer_id, what, count,
             value, req.from_id)
    send_system(peer_id, f"Автоконтекст: {what} — {count} сообщений, контекст {value}"
                + (f" (больше {CONTEXT_SIZE_MAX} не ставится)" if count > CONTEXT_SIZE_MAX else "")
                + "." + context_note(peer_id, value) + " Вернуть прежний: «/сброс контекста».",
                reply_to_msg=message)


def cmd_reset_context(req, arg):
    """/сброс контекста — отменить автоконтекст; /сброс контекста N — сразу задать N."""
    peer_id = req.peer_id
    db_state_del(f"autoctx_{peer_id}")
    db_state_del(f"ctxsum_{peer_id}")
    if arg:
        set_context(peer_id, "ctx", int(arg))
    log.info("peer=%s: сброс контекста%s (от %s)", peer_id, f" до {arg}" if arg else "", req.from_id)
    send_system(peer_id, f"Контекст сброшен: {context_text(peer_id)}.", reply_to_msg=req.message)


def cmd_reasoning(req, arg):
    """/рассуждения вкл | выкл; без аргумента — показать."""
    peer_id = req.peer_id
    if arg is None:
        send_system(peer_id, f"Рассуждения {on_off(reasoning_enabled(peer_id))}. Изменить: "
                             "«/рассуждения вкл» или «/рассуждения выкл».", reply_to_msg=req.message)
        return
    db_state_set(f"reasoning_{peer_id}", "1" if arg else "0")
    log.info("peer=%s: рассуждения %s (от %s)", peer_id, "вкл" if arg else "выкл", req.from_id)
    note = ""
    if arg and not REASONING_PARAM_OK:
        note = (" (API отверг параметр включения рассуждения — модель рассуждает как решит сама, "
                "показываю то, что она вернёт)")
    send_system(peer_id, (f"Рассуждения включены: модель рассуждает перед ответом, рассуждение — в "
                          f"начале сообщения между строками «{REASONING_MARK}»{note}. Рассуждение "
                          "расходует токены — если ответ не успевает дописаться, поднимите "
                          "«/лимит токенов».") if arg else
                "Рассуждения выключены.", reply_to_msg=req.message)


def cmd_limit(req, arg, key, title, default, cap, getter):
    """/лимит токенов, символов, чанков: без числа — показать, «сброс» — к стандарту."""
    peer_id = req.peer_id
    word = {"tokens": "токенов", "chars": "символов", "chunks": "чанков"}[key]
    if arg is None:
        current = getter(peer_id)
        reset_at = tokens_reset_at(peer_id) if key == "tokens" else None
        when = (f" До {datetime.fromtimestamp(reset_at):%H:%M} — потом снова {default}."
                if reset_at else "")
        send_system(peer_id, f"{title}: {current} (стандарт {default}).{when} "
                             f"Изменить: «/лимит {word} N», вернуть стандарт: «… сброс».",
                    reply_to_msg=req.message)
        return
    number = parse_limit_value(arg)
    if number is None:
        db_state_del(f"{key}_{peer_id}")
        log.info("peer=%s: %s возвращён к стандарту командой", peer_id, title)
        send_system(peer_id, f"{title} возвращён к стандарту: {default}.", reply_to_msg=req.message)
        return
    value = min(max(number, 1), cap)
    db_state_set(f"{key}_{peer_id}", f"{value}|{int(now())}")
    log.info("peer=%s: %s = %d (от %s)", peer_id, title, value, req.from_id)
    note = ""
    if key == "tokens" and TOKEN_LIMIT_RESET_MINUTES:
        reset_at = datetime.fromtimestamp(now() + TOKEN_LIMIT_RESET_MINUTES * 60)
        note = (f" В {reset_at:%H:%M} (через {TOKEN_LIMIT_RESET_MINUTES} мин) вернётся к "
                f"стандарту {default}.")
    if key == "chunks":
        note = (f" Это до ~{value * rag.CHUNK_CHARS} символов книг в запросе "
                f"(предохранитель — {RAG_MAX_CONTEXT_CHARS}).")
    send_system(peer_id, f"{title}: {value}." + (f" (максимум {cap})" if number > cap else "")
                + note, reply_to_msg=req.message)


def failure_text(fail_text):
    """«Не получилось…» + почему (LAST_LLM_ERROR) + что можно сделать."""
    reason = LAST_LLM_ERROR or "модель не дала ответа"
    hint = ""
    if "лимит" in reason and "рассуждение" in reason:
        hint = (" Можно поднять лимит: «/лимит токенов 4000» (админ)"
                + (" или выключить рассуждения: «/рассуждения выкл»." if REASONING_ON else "."))
    return f"{fail_text} Причина: {reason}.{hint}"


def llm_reply(req, produce, fail_text):
    """Ответ, на который нужен LLM: проверка дневного лимита, генерация, отправка, учёт.
    Если ответа нет — служебное сообщение с причиной: оно не попадает в контекст модели и не
    считается в дневном лимите (токены за неудачный запрос при этом могли быть списаны).
    Рассуждения включены (/рассуждения вкл) — в начале ответа блок «#рассуждения#»."""
    global LAST_LLM_ERROR
    if daily_limit_reached(req.peer_id):
        send_system(req.peer_id, LIMIT_REACHED_TEXT, reply_to_msg=req.message)
        return False
    LAST_LLM_ERROR = ""
    with with_reasoning(req.peer_id) as r:
        try:
            answer, failure = produce(), None
        except NoAnswer as e:
            answer, failure = "", str(e)
        if not answer and failure is None:
            failure = failure_text(fail_text)
        block = reasoning_block() if r.on and (answer or LLM_REASONINGS) else ""
    if not answer:
        log.warning("peer=%s: %s", req.peer_id, failure)
        send_system(req.peer_id, failure + (f"\n\n{block}" if block else ""), reply_to_msg=req.message)
        return False
    vk_send(req.peer_id, f"{block}\n\n{answer}" if block else answer, reply_to_msg=req.message)
    count_answer(req.peer_id)
    return True


@traced
def run_llm_command(req):
    name, arg = req.command
    peer_id = req.peer_id
    if name == "перескажи":
        target, target_images = quoted_content(req.message)
        llm_reply(req, lambda: summarize_text(peer_id, target, target_images),
                  "Не получилось пересказать.")
    elif name == "мысль":
        def produce():
            # без повтора с большим лимитом: короткий ответ задаёт промпт («Пиши кратко»)
            ask = lambda system, prompt: llm_call(system, prompt, max_tokens=tokens_limit(peer_id),
                                                  retry_empty=False)
            steps = FILOSOF.generate(ask)
            return filosof.format_thought(steps, detailed=arg) if steps["answer"] else ""
        llm_reply(req, produce, "Мысль не родилась — попробуйте позже.")
    elif name == "пересказ":
        cmd_summary(req, *arg)
    elif name == "раг":
        cmd_rag(req, arg)


# ============ База знаний ============
def get_kb():
    """База знаний (папка RAG_DIR): открывается при первом обращении."""
    global KB
    if KB is None:
        os.makedirs(RAG_DIR, exist_ok=True)
        embedder = None
        if RAG_EMBEDDINGS:
            try:
                embedder = rag.SentenceTransformerEmbedder(RAG_EMBEDDING_MODEL)
            except Exception as e:
                log.warning("векторный поиск недоступен (%s) — только поиск по словам", e)
        KB = rag.KnowledgeBase(RAG_DIR, RAG_INDEX_PATH, embedder=embedder, top_k=RAG_TOP_K,
                               min_coverage=RAG_MIN_COVERAGE)
    return KB


def sync_kb():
    """Переиндексирует новые и изменённые книги. Возвращает базу знаний."""
    kb = get_kb()
    changed, removed = kb.sync()
    if changed or removed:
        st = kb.stats()
        log.info("База знаний: изменено %d, удалено %d; книг %d, фрагментов %d, ошибок %d",
                 changed, removed, st["ok"], st["chunks"], len(st["errors"]))
        for path, error in st["errors"]:
            log.warning("База знаний: не прочитан %s: %s", path, error)
    return kb


def rag_status_text(kb, peer_id):
    st = kb.stats()
    lines = [f"База знаний (папка {os.path.basename(RAG_DIR)} рядом с ботом): книг {st['ok']}, "
             f"фрагментов {st['chunks']}" + (f", не прочитано файлов: {len(st['errors'])}" if st["errors"] else "")
             + "."]
    books = kb.books()
    for path, title, chunks, error in books[:30]:
        lines.append(f"• {path} — «{title}», " + (f"не прочитан: {error}" if error else f"фрагментов {chunks}"))
    if len(books) > 30:
        lines.append(f"… и ещё {len(books) - 30}")
    lines.append(f"Режим базы знаний в этой беседе: {'включён' if rag_mode(peer_id) else 'выключен'}, "
                 f"фрагментов в запрос — до {chunks_limit(peer_id)}.")
    lines.append("Разовый вопрос: «Грань, РАГ вопрос» или «/раг вопрос» (только по книгам). "
                 "«/раг вкл» — все ответы с учётом базы знаний, «/раг выкл» — только по истории.")
    return "\n".join(lines)


def kb_search(peer_id, query):
    """Найденные фрагменты (не больше /лимит чанков). Ошибку базы знаний пробрасывает."""
    k = chunks_limit(peer_id)
    hits = sync_kb().search(query, k=k) if query.strip() else []
    log.info("peer=%s: поиск в базе знаний «%s» — найдено фрагментов: %d (лимит %d)", peer_id,
             shorten(query, 100), len(hits), k)
    return hits


@traced
def cmd_rag(req, query):
    """«/раг вопрос»: разовый ответ строго по найденным фрагментам книг; ничего не нашлось —
    служебное сообщение «В базе знаний ничего не нашлось.», без выдумок."""
    peer_id = req.peer_id
    if not query:
        query = quoted_content(req.message)[0].strip()
    try:
        kb = sync_kb()
    except Exception as e:
        log.exception("база знаний: %s", e)
        send_system(peer_id, f"База знаний недоступна: {e}", reply_to_msg=req.message)
        return
    if not query:
        send_system(peer_id, rag_status_text(kb, peer_id), reply_to_msg=req.message)
        return

    def produce():
        hits = kb_search(peer_id, query)
        if not hits:
            raise NoAnswer(RAG_NOTHING_FOUND)
        system, prompt = rag.build_prompt(query, hits, RAG_MAX_CONTEXT_CHARS)
        raw = llm_call(system, prompt, max_tokens=max(RAG_MAX_TOKENS, tokens_limit(peer_id)))
        if not raw:
            return ""
        answer = rag.parse_answer(raw, hits)
        if answer is None:
            log.info("peer=%s: во фрагментах ответа нет (%s)", peer_id, rag.NO_ANSWER)
            raise NoAnswer(RAG_NOTHING_FOUND)
        return answer

    llm_reply(req, produce, "Не получилось ответить по базе знаний.")


def rag_query(requests):
    """Текст для поиска в базе знаний: новые сообщения без обращения «Грань,»."""
    return "\n".join(ADDRESS_PREFIX_RE.sub("", r.text, count=1) for r in requests).strip()


@traced
def rag_mode_answer(peer_id, requests, ctx_rows, images):
    """
    Режим «/раг вкл» — последовательная генерация:
    1) поиск в базе знаний по новым сообщениям (не больше /лимит чанков фрагментов);
    2) нашлось — ответ только по фрагментам (с номерами и источниками);
    3) не нашлось или во фрагментах ответа нет (модель ответила НЕТ_ОТВЕТА) —
       «Поиск по базе знаний не дал результата.» и обычный ответ по истории беседы.
    """
    try:
        hits = kb_search(peer_id, rag_query(requests))
    except Exception as e:
        log.exception("база знаний: %s — отвечаю без неё", e)
        hits = []
    if hits:
        fragments, used = rag.format_fragments(hits, RAG_MAX_CONTEXT_CHARS)
        raw = ask_gran(peer_id, requests, ctx_rows, images, fragments=fragments)
        if not raw:
            return ""
        answer = rag.parse_answer(raw, hits[:used])
        if answer:
            return answer
        log.info("peer=%s: во фрагментах ответа нет (%s) — обычный ответ", peer_id, rag.NO_ANSWER)
    answer = ask_gran(peer_id, requests, ctx_rows, images)
    return f"{RAG_NO_RESULT_PREFIX}\n\n{answer}" if answer else ""


def remember_content(req, text_arg):
    """Что запомнить: пересланные сообщения или reply, иначе текст после «запомни»."""
    message = req.message
    fwd = list(message.get("fwd_messages") or [])
    if not fwd and message.get("reply_message"):
        fwd = [message["reply_message"]]
    if fwd:
        authors = {f.get("from_id") for f in fwd}
        parts = []
        for f in fwd:
            body = render_message(f)[0].strip()
            if body:
                parts.append(f"{display_name(f.get('from_id', 0))}: {body}" if len(authors) > 1 else body)
        return "\n\n".join(parts)
    return (text_arg or "").strip()


@traced
def cmd_remember(req, text_arg):
    """«Грань, запомни» + пересланное: перезаписать RAG/Рефлексия.txt и переиндексировать его."""
    peer_id = req.peer_id
    content = remember_content(req, text_arg)
    if not content:
        send_system(peer_id, f"Что запомнить? Перешлите сообщения с текстом «Грань, запомни» "
                             f"(или напишите текст после «запомни») — он заменит {REMEMBER_FILE} "
                             "в базе знаний.", reply_to_msg=req.message)
        return
    os.makedirs(RAG_DIR, exist_ok=True)
    path = os.path.join(RAG_DIR, REMEMBER_FILE)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content.rstrip() + "\n")
    if os.path.exists(path):
        os.replace(path, path + ".bak")       # прежняя версия — на всякий случай
    os.replace(tmp, path)
    try:
        chunks = get_kb().index_file(REMEMBER_FILE)
    except Exception as e:
        log.exception("индексация %s: %s", REMEMBER_FILE, e)
        send_system(peer_id, f"{REMEMBER_FILE} записан ({len(content)} символов), но переиндексировать "
                             f"не удалось: {e}", reply_to_msg=req.message)
        return
    log.info("peer=%s: %s перезаписан (%d символов, %d фрагментов) от %s", peer_id, REMEMBER_FILE,
             len(content), chunks, req.from_id)
    send_system(peer_id, f"Запомнила: {REMEMBER_FILE} перезаписан ({len(content)} символов, "
                         f"{chunks} фрагментов в базе знаний).", reply_to_msg=req.message)


@traced
def cmd_summary(req, count, start):
    """/пересказ N — последние N сообщений; /пересказ N от ДАТА — N сообщений с даты."""
    peer_id = req.peer_id
    count = min(max(count or SUMMARY_DEFAULT_COUNT, 1), SUMMARY_MAX_INPUT)
    if start is None:
        rows = db_get_context(peer_id, count, exclude_ids=[req.rowid] if req.rowid else ())
        if not rows:
            vk_send(peer_id, "Нечего пересказывать.", reply_to_msg=req.message)
            return
        title = f"Пересказ последних {len(rows)} сообщений"
    else:
        start_ts = parse_start_time(start)
        if start_ts is None:
            send_system(peer_id, "Не понял дату. Пример: «/пересказ 100 от 12:00 16.10.2026».",
                        reply_to_msg=req.message)
            return
        if daily_limit_reached(peer_id):
            send_system(peer_id, LIMIT_REACHED_TEXT, reply_to_msg=req.message)
            return
        ensure_history_since(peer_id, start_ts)
        rows = db_get_range(peer_id, start_ts, count, exclude_ids=[req.rowid] if req.rowid else ())
        if not rows:
            vk_send(peer_id, f"С {fmt_ts(start_ts)} сообщений нет.", reply_to_msg=req.message)
            return
        title = f"Пересказ {len(rows)} сообщений с {fmt_ts(rows[0][4])} по {fmt_ts(rows[-1][4])}"
        if rows[0][4] > start_ts + 3600 and db_state_get(f"history_complete_{peer_id}") == "1":
            title += f" (раньше {fmt_ts(rows[0][4])} сообщений в чате нет)"
    llm_reply(req, lambda: (lambda s: f"{title}:\n\n{s}" if s else "")(summarize_rows(peer_id, rows)),
              "Не получилось пересказать.")


@traced
def answer_batch(peer_id, requests):
    """Один ответ на все обращения, накопившиеся за BATCH_WAIT_SEC (или за паузу)."""
    last = requests[-1]
    exclude = [rid for r in requests for rid in r.rowids]

    def produce():
        forget_ts = int(db_state_get(f"forget_{peer_id}", 0))
        size, source = context_setting(peer_id)
        # размер задан /контекст или /автоконтекст — берём ровно столько, без CONTEXT_MEMORY_HOURS
        since = (int(now()) - CONTEXT_MEMORY_HOURS * 3600
                 if CONTEXT_MEMORY_HOURS and source == "стандарт" else None)
        ctx_rows = db_get_context(peer_id, size, since_ts=since,
                                  min_rows=CONTEXT_MIN_MESSAGES, exclude_ids=exclude,
                                  after_ts=forget_ts)
        # картинки: из новых сообщений, затем из самых свежих сообщений истории
        images = [u for r in requests for u in r.images]
        for row in reversed(ctx_rows):
            if len(images) >= MAX_IMAGES_PER_REQUEST:
                break
            images.extend(row[5])
        if rag_mode(peer_id):
            return rag_mode_answer(peer_id, requests, ctx_rows, images)
        return ask_gran(peer_id, requests, ctx_rows, images)

    if len(requests) > 1:
        log.info("peer=%s: один ответ на %d обращений", peer_id, len(requests))
    llm_reply(last, produce, "Не получилось сформулировать ответ.")


# ============ Обработчик ============
@traced
def handle_message(message):
    peer_id = message["peer_id"]
    from_id = message.get("from_id", 0)
    text = message.get("text") or ""
    ts = message.get("date", int(now()))

    # имена авторов (в т.ч. пересланных и постов) — одним пакетом до рендеринга
    try:
        ensure_names(collect_author_ids(message))
    except Exception as e:
        log.warning("ensure_names: %s", e)

    rowid, full_text, images = store_vk_message(peer_id, message)

    if from_id == BOT_GROUP_ID or from_id < 0:
        return  # свои сообщения и сообщения других ботов только сохраняем
    if is_private(peer_id) and not ALLOW_PRIVATE_MESSAGES:
        return  # в личке бот молчит, пока это не разрешено настройкой

    st = peer_state(peer_id)
    part = {"rowid": rowid, "text": full_text, "images": images, "ts": ts, "len": len(text),
            "added": now()}
    starts_command = text.lstrip().startswith("/") or is_stop_request(text)

    # ВК режет длинные сообщения на части по ~4096 символов: продолжение /роль или «запомни»…
    buf = st.buffers.get(from_id)
    if buf:
        if not starts_command and continue_buffer(peer_id, from_id, buf, text, part):
            return
        finish_buffer(peer_id, from_id)
    # …и продолжение длинного обращения, которое ещё ждёт ответа
    if not starts_command:
        cont = find_continuation(st, from_id, ts)
        if cont:
            cont.add_part(part)
            st.last_pending_ts = now()
            log.info("peer=%s: часть %d длинного обращения от %s (%d символов)", peer_id,
                     cont.parts, from_id, len(text))
            return

    addressed = is_addressed_to_bot(message)
    req = Request(message, rowid, full_text, images)
    cmd = parse_command(text, addressed)
    if cmd is None and has_trigger(text) and is_summary_request(text) and quoted_content(message)[0]:
        cmd = ("перескажи", None)
    if cmd:
        st.chains.pop(from_id, None)
        req.command = cmd
        if cmd[0] in SPLIT_COMMANDS and buffer_may_continue(cmd, text, message):
            start_buffer(st, req, text)
            return
        run_command(req)
        return

    if not addressed:
        note_unaddressed_part(st, from_id, part)
        return
    if not should_respond(ts):
        log.info("Сообщение старое — не отвечаем.")
        return
    chain = take_chain(st, from_id, ts)
    if chain:
        req.prepend(chain)
        log.info("peer=%s: обращение от %s собрано из %d сообщений", peer_id, from_id, req.parts)
    enqueue(req)  # ответ — в process_pending, когда все допишут


def handle_event(event):
    from vk_api.bot_longpoll import VkBotEventType
    if event.type == VkBotEventType.MESSAGE_NEW:
        handle_message(event.object.message)
    elif event.type == VkBotEventType.MESSAGE_REPLY:
        # исходящие сообщения сообщества (в т.ч. написанные админом вручную)
        store_vk_message(event.object["peer_id"], dict(event.object))


# ============ Main ============
def load_env():
    """.env рядом с ботом, затем в текущей папке, затем от bot6 и bot5 (если переходим с них)."""
    from dotenv import load_dotenv
    for path in (os.path.join(BOT_DIR, ".env"), None, os.path.join(BOT_DIR, "..", "bot6", ".env"),
                 os.path.join(BOT_DIR, "..", "bot5", ".env")):
        if path is None:
            load_dotenv()
        elif os.path.exists(path):
            load_dotenv(path)


def init():
    """Подключение к БД, ВК и LLM. Импорты здесь, чтобы тесты не требовали токенов."""
    global DB, vk, llm_client, BOT_GROUP_ID, VK_USER_TOKEN, ADMIN_IDS
    import vk_api
    from vk_api.bot_longpoll import VkBotLongPoll
    from openai import OpenAI

    load_env()
    group_id = int(os.getenv("VK_GROUP_ID"))
    BOT_GROUP_ID = -group_id
    VK_USER_TOKEN = os.getenv("VK_USER_TOKEN") or None
    ADMIN_IDS = {int(x) for x in re.findall(r"\d+", os.getenv("ADMIN_IDS") or "")}
    DB = db_init(DB_PATH)
    migrate_from_bot6()
    if not os.path.exists(role_file_path()):
        log.info("Файла роли %s нет — роль по умолчанию из DEFAULT_ROLE", role_file_path())
    default_role()
    try:
        kb = sync_kb()
        st = kb.stats()
        log.info("База знаний %s: книг %d, фрагментов %d, ошибок %d (лемматизация: %s, векторы: %s)",
                 RAG_DIR, st["ok"], st["chunks"], len(st["errors"]), st["lemmatizer"], st["embeddings"])
    except Exception as e:
        log.exception("база знаний: %s", e)
    session = vk_api.VkApi(token=os.getenv("VK_TOKEN"))
    vk = session.get_api()
    llm_client = OpenAI(api_key=os.getenv("PROXYAPI_KEY"), base_url=PROXYAPI_BASE_URL)
    return VkBotLongPoll(session, group_id, wait=LONGPOLL_WAIT_SEC)


def _graceful_signal(signum, frame):
    global STOP_REQUESTED
    log.info("Получен сигнал %s, завершаюсь...", signum)
    STOP_REQUESTED = True


def terminal_stop_listener(stream=None):
    """Поток-демон (TERMINAL_STOP): «0» + Enter в терминале, где запущен бот, — остановка,
    как «Грань, стоп машина». Ввода нет (systemd, nohup, перенаправление из файла или
    /dev/null) — поток просто завершается. Под bot8_server.py бот читает терминал сервера."""
    global STOP_REQUESTED
    stream = stream or sys.stdin
    try:
        while not STOP_REQUESTED:
            line = stream.readline()
            if not line:
                log.debug("терминал: ввод закрыт — остановка «0» недоступна")
                return
            if line.strip() == "0":
                log.info("Команда остановки из терминала («0»)")
                STOP_REQUESTED = True
                return
            if line.strip():
                log.info("Терминал: для остановки бота введите 0 и нажмите Enter")
    except Exception as e:      # stdin закрыт или недоступен
        log.debug("терминал: %s — остановка «0» недоступна", e)


def start_terminal_stop():
    if sys.stdin is None or getattr(sys.stdin, "closed", True):
        return None
    t = threading.Thread(target=terminal_stop_listener, name="terminal-stop", daemon=True)
    t.start()
    return t


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="Бот «Грань» для чата сообщества ВКонтакте")
    ap.add_argument("--debug", action="store_true",
                    help="подробный лог основных функций (то же — переменная окружения BOT_DEBUG=1)")
    ap.add_argument("--log-file", help="писать лог ещё и в этот файл")
    ap.add_argument("--restart-after-hours", type=float, default=0,
                    help=f"проработав столько часов, выйти с кодом {RESTART_EXIT_CODE} для перезапуска "
                         "(его делает bot8_server.py); выход — когда никто не ждёт ответа")
    ap.add_argument("--no-terminal-stop", action="store_true",
                    help="не читать терминал (без остановки по «0» + Enter)")
    args = ap.parse_args(argv)
    args.debug = args.debug or os.getenv("BOT_DEBUG", "").strip().lower() in ("1", "true", "yes", "да")
    return args


def main(argv=None):
    global STOP_REQUESTED, EXIT_CODE
    args = parse_args(argv)
    setup_logging(args.debug, args.log_file)
    signal.signal(signal.SIGINT, _graceful_signal)
    signal.signal(signal.SIGTERM, _graceful_signal)
    if hasattr(signal, "SIGBREAK"):     # Windows: Ctrl+Break и закрытие консоли
        signal.signal(signal.SIGBREAK, _graceful_signal)

    started = time.time()
    longpoll = init()
    load_history()
    terminal = TERMINAL_STOP and not args.no_terminal_stop and start_terminal_stop()
    log.info("Слушаем Long Poll... (для остановки: '%s' от админа%s)%s%s", STOP_PHRASE,
             " или 0 + Enter в терминале" if terminal else "",
             " [подробный лог]" if args.debug else "",
             f" [перезапуск через {args.restart_after_hours:g} ч]" if args.restart_after_hours else "")

    # check() вместо listen(): Long Poll возвращается раз в LONGPOLL_WAIT_SEC,
    # и между проверками бот отвечает на накопившиеся обращения
    while not STOP_REQUESTED:
        try:
            for event in longpoll.check():
                try:
                    handle_event(event)
                except Exception as e:
                    log.exception("handle: %s", e)
                if STOP_REQUESTED:
                    break
        except Exception as e:
            log.error("longpoll error: %s", e)
            time.sleep(3)
        if STOP_REQUESTED:
            break
        try:
            process_pending()
        except Exception as e:
            log.exception("process_pending: %s", e)
        if (args.restart_after_hours and time.time() - started >= args.restart_after_hours * 3600
                and is_idle()):
            log.info("Плановый перезапуск: бот работает %.1f ч", (time.time() - started) / 3600)
            EXIT_CODE = RESTART_EXIT_CODE
            STOP_REQUESTED = True

    log.info("Бот остановлен.")
    for closable in (KB, DB):
        try:
            if closable is not None:
                closable.close()
        except Exception:
            pass
    sys.exit(EXIT_CODE)


if __name__ == "__main__":
    main()
