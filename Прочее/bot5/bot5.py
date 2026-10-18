# bot5.py — бот «Грань» для чата сообщества ВКонтакте (следующая версия bot4.py)
#
# Что добавлено по сравнению с bot4 (подробно — в README.md этой папки):
#   * личные сообщения — только если разрешено (ALLOW_PRIVATE_MESSAGES, по умолчанию нет);
#     в личке собеседнику доступны все команды админа;
#   * /помощь — список команд с описаниями и правами;
#   * /пауза — бот копит обращения и отвечает на них после паузы;
#   * /лимит токенов, /лимит символов — настройки беседы (только админ);
#   * пакетные ответы: бот ждёт BATCH_WAIT_SEC тишины и отвечает на все обращения разом;
#   * контекст растёт в активном споре и возвращается к CONTEXT_SIZE после тишины;
#     если история не влезает в лимит символов, старая часть сжимается в пересказ;
#   * дневной лимит сообщений с оповещениями 25/50/90/100 % и /сбросить лимит сообщений;
#   * /пересказ N от ЧЧ:ММ ДД.ММ.ГГГГ — пересказ с даты, с догрузкой истории из ВК;
#   * /мысль, /мысль подробно — философские размышления (модуль filosof.py из ai_filosof);
#   * пост в контекст попадает целиком (POST_MAX_CHARS убран).
import os
import re
import sys
import json
import math
import time
import base64
import signal
import sqlite3
import logging
import urllib.request
from datetime import datetime, timedelta

BOT_DIR = os.path.dirname(os.path.abspath(__file__))
if BOT_DIR not in sys.path:
    sys.path.insert(0, BOT_DIR)
import filosof  # noqa: E402  (лежит рядом с ботом)

# ============ НАСТРОЙКИ ============
BOT_NAME = "Грань разумного"
TRIGGER_RE = re.compile(r"\bгрань\b", re.IGNORECASE)
STOP_PHRASE = "грань, стоп машина"

# --- личные сообщения и права ---
ALLOW_PRIVATE_MESSAGES = False     # можно ли боту отвечать в личных сообщениях (по умолчанию — нет)
ADMIN_CACHE_MINUTES = 10           # сколько минут помнить список админов беседы
# ADMIN_IDS в .env — id пользователей, которые админы бота в любой беседе (через запятую)

# --- контекст ---
CONTEXT_SIZE = 30                  # обычный размер контекста (последних сообщений)
CONTEXT_SIZE_MAX = 300             # потолок роста контекста в активном споре
CONTEXT_RESET_MINUTES = 5          # столько минут без новых сообщений — контекст снова CONTEXT_SIZE
CONTEXT_MIN_MESSAGES = 10          # столько последних сообщений берём всегда, даже если они старые
CONTEXT_MEMORY_HOURS = 12          # более старые сообщения — только в пределах CONTEXT_MIN_MESSAGES (0 — без ограничения)
MAX_CONTEXT_CHARS = 12000          # стандартный лимит символов истории в запросе (меняется /лимит символов)
MAX_CONTEXT_CHARS_CAP = 300000     # больше этого /лимит символов не поставит
CONTEXT_SUMMARY_CHARS = 2000       # размер сжатого пересказа старой части истории
CONTEXT_KEEP_SHARE = 0.6           # после сжатия свежая часть занимает не больше этой доли остатка

# --- ответы ---
MAX_TOKENS_DEFAULT = 800           # стандартный лимит выходных токенов (меняется /лимит токенов)
MAX_TOKENS_CAP = 64000             # больше этого /лимит токенов не поставит
TOKEN_LIMIT_RESET_MINUTES = 60     # через столько минут /лимит токенов сам вернётся к стандарту (0 — не вернётся)
BATCH_WAIT_SEC = 8                 # ждём столько секунд без новых обращений и отвечаем на все разом
BATCH_MAX_WAIT_SEC = 60            # но не дольше этого с первого обращения
PAUSE_DEFAULT_MINUTES = 1          # /пауза без числа
PAUSE_MAX_MINUTES = 60
FRESHNESS_MINUTES = 10             # если обращение старше — не отвечаем

# --- дневной лимит ---
DAILY_MESSAGE_LIMIT = 100          # ответов бота в сутки на беседу (0 — без лимита)
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

LLM_MIN_INTERVAL_SEC = 3           # не чаще одного запроса к LLM за столько секунд
VK_MESSAGE_LIMIT = 4000            # ВК режет сообщения длиннее 4096 символов
LONGPOLL_WAIT_SEC = 1              # Long Poll возвращается не реже — чтобы вовремя отвечать пакетам

DB_PATH = "gran5.db"
OLD_DB_PATH = "gran4.db"           # БД bot4: из неё переносятся сообщения, роли и имена

PROXYAPI_BASE_URL = "https://api.proxyapi.ru/v1"
MODEL_NAME = "deepseek/deepseek-v4-flash-vision-exp"
# ===================================

CHAT_PEER_OFFSET = 2000000000      # peer_id бесед начинается с 2e9
SYSTEM_MARK = "⚙️"                 # служебные сообщения бота; в контекст LLM не попадают

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("gran")

DEFAULT_ROLE = (
    "Ты — Грань, участник беседы. Общайся живо, по-человечески, "
    "не будь слишком формальной, подстраивайся под тон беседы."
)
LIMIT_REACHED_TEXT = "Извините, сегодня лимит сообщений достигнут."

STOP_REQUESTED = False

# Заполняются в init() — так модуль можно импортировать в тестах без токенов и сети.
DB = None
vk = None
llm_client = None
BOT_GROUP_ID = 0                   # отрицательный id сообщества (from_id сообщений бота)
VK_USER_TOKEN = None               # необязательный ключ пользователя/сервисный — для wall.getById
ADMIN_IDS = set()
_last_llm_call = 0.0
_admin_cache = {}                  # peer_id -> (когда загружен, {id админов})
PEERS = {}                         # peer_id -> PeerState: очередь обращений, пауза, размер контекста
FILOSOF = filosof.ThoughtGenerator()


def now():
    """Текущее время; тесты подменяют эту функцию."""
    return time.time()


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


def db_get_context(peer_id, limit, since_ts=None, min_rows=0, exclude_ids=(), after_ts=None):
    """
    Последние `limit` сообщений беседы в порядке старые → новые.
    since_ts  — мягкая граница: старше неё берём не больше `min_rows` последних сообщений;
    after_ts  — жёсткая граница (команда /забудь): старше неё не берём ничего;
    exclude_ids — не включать эти сообщения (те, на которые сейчас отвечаем).
    """
    exclude_ids = list(exclude_ids) or [-1]
    marks = ",".join("?" * len(exclude_ids))
    rows = DB.execute(
        f"SELECT id,from_id,is_bot,text,ts,images FROM messages "
        f"WHERE peer_id=? AND id NOT IN ({marks}) AND ts>=? ORDER BY ts DESC, id DESC LIMIT ?",
        (peer_id, *exclude_ids, after_ts or 0, limit),
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


def db_get_role(peer_id):
    r = DB.execute("SELECT role FROM roles WHERE peer_id=?", (peer_id,)).fetchone()
    return r[0] if r else DEFAULT_ROLE


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


def migrate_from_bot4(old_path=OLD_DB_PATH):
    """Переносит из БД bot4 сообщения, роли, имена и метки /забудь: схема та же,
    история в bot4 уже полная, загружать её из ВК заново не нужно."""
    if db_state_get("migrated_bot4") == "1" or not os.path.exists(old_path):
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
        for key, value in old.execute("SELECT key, value FROM state WHERE key LIKE 'forget_%'"):
            DB.execute("INSERT OR IGNORE INTO state(key, value) VALUES(?,?)", (key, value))
        old.close()
        DB.commit()
        if n:
            db_state_set("initial_load_done", "1")
        db_state_set("migrated_bot4", "1")
        log.info("Из %s перенесено %d сообщений, роли и имена", old_path, n)
    except Exception as e:
        log.warning("migrate_from_bot4: %s", e)


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
        msg.get("date") or int(time.time()),
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
        db_add_message(peer_id, BOT_GROUP_ID, True, part, int(time.time()),
                       cmid=sent_cmid, vk_msg_id=sent_id)


def send_system(peer_id, text, reply_to_msg=None):
    """Служебное сообщение (помощь, лимиты, пауза): с пометкой SYSTEM_MARK, в контекст не идёт."""
    vk_send(peer_id, f"{SYSTEM_MARK} {text}", reply_to_msg=reply_to_msg)


# ============ Загрузка истории ============
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


def tokens_limit(peer_id):
    """Лимит выходных токенов беседы; через TOKEN_LIMIT_RESET_MINUTES — снова стандарт."""
    value, set_ts = _limit_get(f"tokens_{peer_id}", MAX_TOKENS_DEFAULT)
    if set_ts and TOKEN_LIMIT_RESET_MINUTES and now() - set_ts >= TOKEN_LIMIT_RESET_MINUTES * 60:
        db_state_del(f"tokens_{peer_id}")
        log.info("peer=%s: лимит токенов вернулся к стандарту %d", peer_id, MAX_TOKENS_DEFAULT)
        return MAX_TOKENS_DEFAULT
    return value


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


def daily_state(peer_id):
    raw = db_state_get(f"daily_{peer_id}")
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
    db_state_set(f"daily_{peer_id}", json.dumps(st))
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
        self.ctx_size = CONTEXT_SIZE
        self.growing = False           # активный спор: контекст растёт на 1 с каждым сообщением
        self.last_activity = 0.0       # время последнего сообщения людей
        self.last_answer_ts = 0.0
        self.last_interval = None      # время между двумя последними ответами бота


def peer_state(peer_id):
    if peer_id not in PEERS:
        PEERS[peer_id] = PeerState()
    return PEERS[peer_id]


def reset_context_size_if_quiet(st, t):
    if st.last_activity and t - st.last_activity >= CONTEXT_RESET_MINUTES * 60:
        st.ctx_size = CONTEXT_SIZE
        st.growing = False
        st.last_answer_ts = 0.0
        st.last_interval = None


def note_activity(peer_id, t=None):
    """Новое сообщение людей: после тишины — контекст к исходному, в споре — +1."""
    t = now() if t is None else t
    st = peer_state(peer_id)
    reset_context_size_if_quiet(st, t)
    if st.growing:
        st.ctx_size = min(st.ctx_size + 1, CONTEXT_SIZE_MAX)
    st.last_activity = t


def note_bot_answer(peer_id, t=None):
    """Ответ бота: если время между ответами уменьшается — начался активный спор."""
    t = now() if t is None else t
    st = peer_state(peer_id)
    if st.last_answer_ts:
        interval = t - st.last_answer_ts
        if st.last_interval is not None and interval < st.last_interval and not st.growing:
            st.growing = True
            log.info("peer=%s: активный спор, контекст растёт (сейчас %d)", peer_id, st.ctx_size)
        st.last_interval = interval
    st.last_answer_ts = t


def context_size(peer_id):
    st = peer_state(peer_id)
    reset_context_size_if_quiet(st, now())
    return st.ctx_size


def is_paused(peer_id):
    return peer_state(peer_id).paused_until > now()


class Request:
    """Обращение к боту: сообщение ВК + то, что о нём уже известно."""

    def __init__(self, message, rowid=None, text="", images=(), command=None):
        self.message = message
        self.peer_id = message["peer_id"]
        self.from_id = message.get("from_id", 0)
        self.ts = message.get("date") or int(time.time())
        self.rowid = rowid
        self.text = text
        self.images = list(images)
        self.command = command         # (имя, аргумент) — для команд, отложенных паузой


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
    for peer_id, st in list(PEERS.items()):
        if st.paused_until > t:
            continue
        resumed = bool(st.paused_until)     # пауза только что кончилась — отвечаем сразу
        st.paused_until = 0.0
        if st.pending and (resumed
                           or t - st.last_pending_ts >= BATCH_WAIT_SEC
                           or t - st.first_pending_ts >= BATCH_MAX_WAIT_SEC):
            flush(peer_id)


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
    """[(id, строка), ...] без пустых и служебных сообщений бота."""
    out = []
    for rid, from_id, is_bot, text, ts, _images in rows:
        if not text or (is_bot and text.startswith(SYSTEM_MARK)):
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
    summary = llm_call("Ты сжимаешь историю чата для памяти собеседника.", prompt,
                       max_tokens=max(300, CONTEXT_SUMMARY_CHARS // 2))
    return shorten(summary, CONTEXT_SUMMARY_CHARS)


def build_history(peer_id, rows, max_chars):
    """
    История для запроса. Влезает в max_chars — как есть. Не влезает (в активном споре
    контекст растёт) — старая часть сжимается в пересказ, а свежие сообщения идут
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


def _image_part(url):
    if IMAGE_MODE == "base64":
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                data = r.read(IMAGE_MAX_BYTES + 1)
                mime = r.headers.get_content_type() or "image/jpeg"
            if len(data) <= IMAGE_MAX_BYTES:
                url = f"data:{mime};base64,{base64.b64encode(data).decode()}"
        except Exception as e:
            log.warning("скачивание картинки: %s — передаю ссылкой", e)
    return {"type": "image_url", "image_url": {"url": url}}


def _rate_limit():
    """Не чаще одного запроса за LLM_MIN_INTERVAL_SEC: запросы к API платные."""
    global _last_llm_call
    wait = _last_llm_call + LLM_MIN_INTERVAL_SEC - time.time()
    if wait > 0:
        time.sleep(wait)
    _last_llm_call = time.time()


def llm_call(system, user_text, max_tokens=MAX_TOKENS_DEFAULT, images=()):
    images = list(dict.fromkeys(images))[:MAX_IMAGES_PER_REQUEST]
    content = user_text
    if images:
        content = [{"type": "text", "text": user_text}] + [_image_part(u) for u in images]
    _rate_limit()
    try:
        resp = llm_client.chat.completions.create(
            model=MODEL_NAME,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": content},
            ],
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        log.error("llm_call: %s", e)
        if images:
            # модель или прокси не приняли картинки — пробуем хотя бы текстом
            return llm_call(system, user_text + "\n\n(Картинки не удалось передать.)",
                            max_tokens)
        return ""


def clean_answer(text):
    """Убирает «Грань разумного:» в начале, если модель подписалась."""
    return re.sub(rf"^\s*(\[[^\]]*\]\s*)?({re.escape(BOT_NAME)}|Грань)\s*:\s*", "", text or "").strip()


def ask_gran(peer_id, requests, context_rows, images=()):
    """Один запрос к LLM на одно или несколько обращений, накопившихся за BATCH_WAIT_SEC."""
    system = build_system_prompt(db_get_role(peer_id))
    ctx = build_history(peer_id, context_rows, chars_limit(peer_id))
    log.debug("КОНТЕКСТ peer=%s:\n%s", peer_id, ctx)

    history = f"ИСТОРИЯ:\n{ctx}\n\n" if ctx else "Истории нет — это первое сообщение.\n\n"
    new = "\n".join(format_line(r.from_id, False, r.text, r.ts) for r in requests)
    if len(requests) == 1:
        task = f"НОВОЕ СООБЩЕНИЕ:\n{new}\n\n"
    else:
        task = (f"НОВЫЕ СООБЩЕНИЯ (их {len(requests)}, ответь на все одним сообщением, "
                f"обращаясь к авторам по имени):\n{new}\n\n")
    prompt = (
        "Ниже история чата (старые сообщения сверху, новые снизу) и новые "
        "сообщения, на которые надо ответить.\n\n"
        f"{history}{task}"
        + ("К запросу приложены картинки из новых сообщений и последних сообщений истории.\n\n"
           if images else "")
        + "Ответь как Грань."
    )
    return clean_answer(llm_call(system, prompt, max_tokens=tokens_limit(peer_id), images=images))


def summarize_rows(peer_id, rows):
    system = build_system_prompt(db_get_role(peer_id))
    ctx = format_context(rows, max_chars=SUMMARY_MAX_INPUT * 200)
    prompt = (
        "Ниже — история чата. Сделай краткий, но содержательный пересказ: "
        "о чём говорили, ключевые темы, кто что утверждал, к чему пришли.\n\n"
        f"{ctx}"
    )
    return llm_call(system, prompt, max_tokens=max(SUMMARY_MAX_TOKENS, tokens_limit(peer_id)))


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


def extract_role(text):
    m = re.search(r"/роль\s*\(([^)]*)\)", text, re.IGNORECASE | re.DOTALL)
    return m.group(1).strip() if m else None


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


_LIMIT_ARG = r"(?:\s+(\d+|сброс|стандарт))?"
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
]


def parse_command(text):
    """(имя, аргумент) первой найденной команды или None."""
    text = text or ""
    if is_stop_request(text):
        return ("стоп", None)
    role = extract_role(text)
    if role:
        return ("роль", role)
    for name, rx in COMMAND_PATTERNS:
        m = rx.search(text)
        if not m:
            continue
        if name == "пересказ":
            return (name, (int(m.group(1)) if m.group(1) else None,
                           m.group(2).strip() if m.group(2) else None))
        if name == "мысль":
            return (name, bool(m.group(1)))
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
    ("роль", "/роль (текст)", "все", "задать боту роль в этой беседе"),
    ("помощь", "/помощь", "все", "этот список и текущие настройки беседы"),
    ("лимит токенов", "/лимит токенов N / сброс", "админ",
     f"лимит длины ответа в токенах (стандарт {MAX_TOKENS_DEFAULT}"
     + (f", сам возвращается через {TOKEN_LIMIT_RESET_MINUTES} мин" if TOKEN_LIMIT_RESET_MINUTES
        else "") + ")"),
    ("лимит символов", "/лимит символов N / сброс", "админ",
     f"сколько символов истории передавать модели (стандарт {MAX_CONTEXT_CHARS}); "
     "что не влезло — сжимается в пересказ"),
    ("сбросить лимит", "/сбросить лимит сообщений", "админ",
     f"обнулить дневной лимит ответов ({DAILY_MESSAGE_LIMIT} в сутки)"),
    ("стоп", f"«{STOP_PHRASE.capitalize()}»", "все", "остановить бота"),
]
ADMIN_COMMANDS = {"лимит токенов", "лимит символов", "сбросить лимит"}
LLM_COMMANDS = {"пересказ", "мысль", "перескажи"}   # на паузе откладываются, расходуют лимит


def help_text(peer_id):
    lines = ["Команды бота «Грань»:", ""]
    for _name, usage, who, what in HELP:
        lines.append(f"• {usage} — {what}. Кто: {who}.")
    st = daily_state(peer_id)
    lines += [
        "",
        "Админ — админ или создатель беседы (боту нужны права админа в беседе); "
        "в личных сообщениях собеседнику доступны все команды.",
        "",
        "Сейчас в этой беседе:",
        f"• лимит токенов ответа: {tokens_limit(peer_id)}",
        f"• лимит символов истории: {chars_limit(peer_id)}",
        f"• контекст: {context_size(peer_id)} сообщений (обычно {CONTEXT_SIZE})",
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
    return name == "пересказ" and (arg[0] or SUMMARY_DEFAULT_COUNT) > SUMMARY_FREE_MAX


def run_command(req):
    """Выполняет команду. Служебные — сразу; требующие LLM на паузе ждут её конца."""
    global STOP_REQUESTED
    peer_id, message = req.peer_id, req.message
    name, arg = req.command

    if needs_admin(req.command) and not is_admin(peer_id, req.from_id):
        what = (f"Пересказ больше {SUMMARY_FREE_MAX} сообщений" if name == "пересказ"
                else "Эта команда")
        send_system(peer_id, f"{what} доступна только админу беседы.", reply_to_msg=message)
        return

    if name in LLM_COMMANDS:
        if is_paused(peer_id):
            enqueue(req)
        else:
            run_llm_command(req)
        return

    if name == "стоп":
        vk_send(peer_id, "Останавливаюсь.", reply_to_msg=message)
        log.info("Команда остановки от peer=%s", peer_id)
        db_state_set("stopped_at", int(now()))
        STOP_REQUESTED = True
    elif name == "роль":
        db_set_role(peer_id, arg)
        vk_send(peer_id, "Роль обновлена.", reply_to_msg=message)
    elif name == "забудь":
        # сбросить контекст беседы, история в БД остаётся
        db_state_set(f"forget_{peer_id}", req.ts + 1)
        db_state_del(f"ctxsum_{peer_id}")
        vk_send(peer_id, "Хорошо, начинаю с чистого листа.", reply_to_msg=message)
    elif name == "помощь":
        send_system(peer_id, help_text(peer_id), reply_to_msg=message)
    elif name == "пауза":
        cmd_pause(req, arg)
    elif name == "лимит токенов":
        cmd_limit(req, arg, "tokens", "Лимит выходных токенов", MAX_TOKENS_DEFAULT,
                  MAX_TOKENS_CAP, tokens_limit)
    elif name == "лимит символов":
        cmd_limit(req, arg, "chars", "Лимит символов истории", MAX_CONTEXT_CHARS,
                  MAX_CONTEXT_CHARS_CAP, chars_limit)
    elif name == "сбросить лимит":
        db_state_del(f"daily_{peer_id}")
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


def cmd_limit(req, arg, key, title, default, cap, getter):
    """/лимит токенов и /лимит символов: без числа — показать, «сброс» — к стандарту."""
    peer_id = req.peer_id
    if arg is None:
        send_system(peer_id, f"{title}: {getter(peer_id)} (стандарт {default}). "
                             f"Изменить: «/лимит {'токенов' if key == 'tokens' else 'символов'} "
                             "N», вернуть стандарт: «… сброс».", reply_to_msg=req.message)
        return
    if not arg.isdigit():
        db_state_del(f"{key}_{peer_id}")
        send_system(peer_id, f"{title} возвращён к стандарту: {default}.", reply_to_msg=req.message)
        return
    value = min(max(int(arg), 1), cap)
    db_state_set(f"{key}_{peer_id}", f"{value}|{int(now())}")
    note = ""
    if key == "tokens" and TOKEN_LIMIT_RESET_MINUTES:
        note = f" Через {TOKEN_LIMIT_RESET_MINUTES} мин вернётся к стандарту {default}."
    send_system(peer_id, f"{title}: {value}." + (f" (максимум {cap})" if int(arg) > cap else "")
                + note, reply_to_msg=req.message)


def llm_reply(req, produce, fail_text):
    """Ответ, на который нужен LLM: проверка дневного лимита, генерация, отправка, учёт."""
    if daily_limit_reached(req.peer_id):
        send_system(req.peer_id, LIMIT_REACHED_TEXT, reply_to_msg=req.message)
        return False
    answer = produce()
    vk_send(req.peer_id, answer or fail_text, reply_to_msg=req.message)
    if answer:
        count_answer(req.peer_id)
        note_bot_answer(req.peer_id)
    return bool(answer)


def run_llm_command(req):
    name, arg = req.command
    peer_id = req.peer_id
    if name == "перескажи":
        target, target_images = quoted_content(req.message)
        llm_reply(req, lambda: summarize_text(peer_id, target, target_images),
                  "Не получилось пересказать.")
    elif name == "мысль":
        def produce():
            ask = lambda system, prompt: llm_call(system, prompt, max_tokens=tokens_limit(peer_id))
            steps = FILOSOF.generate(ask)
            return filosof.format_thought(steps, detailed=arg) if steps["answer"] else ""
        llm_reply(req, produce, "Мысль не родилась — попробуйте позже.")
    elif name == "пересказ":
        cmd_summary(req, *arg)


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


def answer_batch(peer_id, requests):
    """Один ответ на все обращения, накопившиеся за BATCH_WAIT_SEC (или за паузу)."""
    last = requests[-1]
    exclude = [r.rowid for r in requests if r.rowid]

    def produce():
        forget_ts = int(db_state_get(f"forget_{peer_id}", 0))
        since = int(now()) - CONTEXT_MEMORY_HOURS * 3600 if CONTEXT_MEMORY_HOURS else None
        ctx_rows = db_get_context(peer_id, context_size(peer_id), since_ts=since,
                                  min_rows=CONTEXT_MIN_MESSAGES, exclude_ids=exclude,
                                  after_ts=forget_ts)
        # картинки: из новых сообщений, затем из самых свежих сообщений истории
        images = [u for r in requests for u in r.images]
        for row in reversed(ctx_rows):
            if len(images) >= MAX_IMAGES_PER_REQUEST:
                break
            images.extend(row[5])
        return ask_gran(peer_id, requests, ctx_rows, images)

    if len(requests) > 1:
        log.info("peer=%s: один ответ на %d обращений", peer_id, len(requests))
    llm_reply(last, produce, "Не получилось сформулировать ответ.")


# ============ Обработчик ============
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

    note_activity(peer_id, now())
    req = Request(message, rowid, full_text, images)

    cmd = parse_command(text)
    if cmd is None and has_trigger(text) and is_summary_request(text) and quoted_content(message)[0]:
        cmd = ("перескажи", None)
    if cmd:
        req.command = cmd
        run_command(req)
        return

    if not is_addressed_to_bot(message):
        return
    if not should_respond(ts):
        log.info("Сообщение старое — не отвечаем.")
        return
    enqueue(req)  # ответ — в process_pending, когда все допишут


def handle_event(event):
    from vk_api.bot_longpoll import VkBotEventType
    if event.type == VkBotEventType.MESSAGE_NEW:
        handle_message(event.object.message)
    elif event.type == VkBotEventType.MESSAGE_REPLY:
        # исходящие сообщения сообщества (в т.ч. написанные админом вручную)
        store_vk_message(event.object["peer_id"], dict(event.object))


# ============ Main ============
def init():
    """Подключение к БД, ВК и LLM. Импорты здесь, чтобы тесты не требовали токенов."""
    global DB, vk, llm_client, BOT_GROUP_ID, VK_USER_TOKEN, ADMIN_IDS
    from dotenv import load_dotenv
    import vk_api
    from vk_api.bot_longpoll import VkBotLongPoll
    from openai import OpenAI

    load_dotenv()
    group_id = int(os.getenv("VK_GROUP_ID"))
    BOT_GROUP_ID = -group_id
    VK_USER_TOKEN = os.getenv("VK_USER_TOKEN") or None
    ADMIN_IDS = {int(x) for x in re.findall(r"\d+", os.getenv("ADMIN_IDS") or "")}
    DB = db_init(DB_PATH)
    migrate_from_bot4()
    session = vk_api.VkApi(token=os.getenv("VK_TOKEN"))
    vk = session.get_api()
    llm_client = OpenAI(api_key=os.getenv("PROXYAPI_KEY"), base_url=PROXYAPI_BASE_URL)
    return VkBotLongPoll(session, group_id, wait=LONGPOLL_WAIT_SEC)


def _graceful_signal(signum, frame):
    global STOP_REQUESTED
    log.info("Получен сигнал %s, завершаюсь...", signum)
    STOP_REQUESTED = True


def main():
    signal.signal(signal.SIGINT, _graceful_signal)
    signal.signal(signal.SIGTERM, _graceful_signal)

    longpoll = init()
    load_history()
    log.info("Слушаем Long Poll... (для остановки: '%s')", STOP_PHRASE)

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

    log.info("Бот остановлен.")
    try:
        DB.close()
    except Exception:
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
