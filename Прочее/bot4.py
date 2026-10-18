# bot4.py — бот «Грань» для чата сообщества ВКонтакте (следующая версия bot3.py)
#
# Что изменилось по сравнению с bot3 (подробно — в README.md этой папки):
#   * контекст больше не пустеет: сообщения в беседах хранятся по
#     (peer_id, conversation_message_id), а не по id, который у бота в беседе = 0;
#   * свои ответы бот сохраняет в историю сразу после отправки;
#   * контекст — последние N сообщений, а не «только за 30 минут»;
#   * бот отвечает на reply к своим сообщениям (без слова «грань»);
#   * в историю попадает текст пересланных сообщений (с вложенностью);
#   * бот видит картинки (vision-модель) и текст постов, которыми поделились.
import os
import re
import sys
import json
import time
import base64
import signal
import sqlite3
import logging
import urllib.request
from datetime import datetime

# ============ НАСТРОЙКИ ============
BOT_NAME = "Грань разумного"
TRIGGER_RE = re.compile(r"\bгрань\b", re.IGNORECASE)
STOP_PHRASE = "грань, стоп машина"

CONTEXT_SIZE = 40                  # максимум последних сообщений в контексте
CONTEXT_MIN_MESSAGES = 10          # столько последних сообщений берём всегда, даже если они старые
CONTEXT_MEMORY_HOURS = 12          # более старые сообщения — только в пределах CONTEXT_MIN_MESSAGES
MAX_CONTEXT_CHARS = 12000          # ограничение длины истории в промпте (обрезаем самые старые)
INITIAL_LOAD = 1000                # сколько сообщений загрузить при первом старте
SYNC_ON_START = 200                # при каждом следующем старте — догрузить пропущенное
FRESHNESS_MINUTES = 10             # если обращение старше — не отвечаем
SUMMARY_MAX_INPUT = 5000
RESPOND_IN_PRIVATE = True          # в личных сообщениях отвечать без слова «грань»

FWD_MAX_DEPTH = 4                  # глубина вложенности пересланных сообщений
POST_MAX_CHARS = 4000              # сколько символов поста брать в контекст
QUOTE_MAX_CHARS = 200              # цитата сообщения, на которое ответили (в истории)

MAX_IMAGES_PER_REQUEST = 4         # картинок в одном запросе к LLM
IMAGE_MODE = "base64"              # "base64" — скачать и передать данными, "url" — передать ссылку
IMAGE_MAX_BYTES = 5 * 1024 * 1024
IMAGE_MAX_SIDE = 1280              # предпочитаем размер фото не больше этой стороны

LLM_MIN_INTERVAL_SEC = 3           # не чаще одного запроса к LLM за столько секунд
VK_MESSAGE_LIMIT = 4000            # ВК режет сообщения длиннее 4096 символов

DB_PATH = "gran4.db"
OLD_DB_PATH = "gran.db"            # БД bot3: из неё переносятся роли и имена

PROXYAPI_BASE_URL = "https://api.proxyapi.ru/v1"
MODEL_NAME = "deepseek/deepseek-v4-flash-vision-exp"
# ===================================

CHAT_PEER_OFFSET = 2000000000      # peer_id бесед начинается с 2e9

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("gran")

DEFAULT_ROLE = (
    "Ты — Грань, участник беседы. Общайся живо, по-человечески, "
    "не будь слишком формальной, подстраивайся под тон беседы."
)

STOP_REQUESTED = False

# Заполняются в init() — так модуль можно импортировать в тестах без токенов и сети.
DB = None
vk = None
llm_client = None
BOT_GROUP_ID = 0                   # отрицательный id сообщества (from_id сообщений бота)
VK_USER_TOKEN = None               # необязательный ключ пользователя/сервисный — для wall.getById
_last_llm_call = 0.0


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


def db_get_context(peer_id, limit, since_ts=None, min_rows=0, exclude_id=None, after_ts=None):
    """
    Последние `limit` сообщений беседы в порядке старые → новые.
    since_ts  — мягкая граница: старше неё берём не больше `min_rows` последних сообщений;
    after_ts  — жёсткая граница (команда /забудь): старше неё не берём ничего;
    exclude_id — не включать это сообщение (то, на которое сейчас отвечаем).
    Строки: (from_id, is_bot, text, ts, images[]).
    """
    rows = DB.execute(
        "SELECT id,from_id,is_bot,text,ts,images FROM messages "
        "WHERE peer_id=? AND id!=? AND ts>=? ORDER BY ts DESC, id DESC LIMIT ?",
        (peer_id, exclude_id or -1, after_ts or 0, limit),
    ).fetchall()
    rows.reverse()
    if since_ts is not None:
        recent = [r for r in rows if r[4] >= since_ts]
        if len(recent) < min_rows:
            recent = rows[-min_rows:] if min_rows else []
        rows = recent
    return [(r[1], r[2], r[3], r[4], json.loads(r[5]) if r[5] else []) for r in rows]


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


def migrate_from_bot3(old_path=OLD_DB_PATH):
    """Переносит роли и имена из БД bot3. Сообщения не переносим: в старой БД
    они неполные (см. баг с id=0) — история заново загружается из ВК."""
    if db_state_get("migrated_bot3") == "1" or not os.path.exists(old_path):
        return
    try:
        old = sqlite3.connect(old_path)
        for peer_id, role in old.execute("SELECT peer_id, role FROM roles"):
            DB.execute("INSERT OR IGNORE INTO roles(peer_id, role) VALUES(?,?)", (peer_id, role))
        for row in old.execute("SELECT user_id, first_name, last_name, updated_at FROM users"):
            DB.execute("INSERT OR IGNORE INTO users VALUES(?,?,?,?)", row)
        old.close()
        DB.commit()
        db_state_set("migrated_bot3", "1")
        log.info("Роли и имена перенесены из %s", old_path)
    except Exception as e:
        log.warning("migrate_from_bot3: %s", e)


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
    now = int(time.time())
    missing = []
    for i in set(ids):
        if not i or i == BOT_GROUP_ID:
            continue
        r = DB.execute("SELECT updated_at FROM users WHERE user_id=?", (i,)).fetchone()
        if not r or now - r[0] >= 86400:
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
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


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
                         f"«{shorten(obj.get('text'), POST_MAX_CHARS)}»]")
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
    """Текст поста (записи), которым поделились, с репостами и картинками."""
    if post.get("is_deleted") or post.get("deleted"):
        return ["[запись удалена или недоступна]"]
    if not (post.get("text") or post.get("attachments") or post.get("copy_history")):
        post = fetch_wall_post(post.get("owner_id") or post.get("to_id"), post.get("id")) or post
    author = display_name(post.get("from_id") or post.get("owner_id") or 0)
    text = shorten(post.get("text"), POST_MAX_CHARS)
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
    """Как render_message, но с пометкой, на что это ответ."""
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
    Отправляет ответ и сразу сохраняет его в историю (в bot3 ответы бота
    в историю не попадали). В беседах reply делается через параметр forward
    с conversation_message_ids: reply_to там не работает, потому что id = 0.
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


# ============ Загрузка истории ============
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
        collected, offset = [], 0
        while len(collected) < per_peer:
            try:
                items = vk.messages.getHistory(
                    peer_id=peer_id, count=min(200, per_peer - len(collected)), offset=offset,
                )["items"]
            except Exception as e:
                log.warning("getHistory peer=%s: %s", peer_id, e)
                break
            if not items:
                break
            collected.extend(items)
            offset += len(items)

        # ВК отдаёт от новых к старым — разворачиваем
        collected.reverse()
        ids = set()
        for m in collected:
            collect_author_ids(m, ids)
        ensure_names(ids)
        for m in collected:
            store_vk_message(peer_id, m)
        total += len(collected)
        log.info("peer=%s загружено %d", peer_id, len(collected))

    db_state_set("initial_load_done", "1")
    log.info("Загрузка истории завершена, всего %d сообщений.", total)


# ============ LLM ============
def build_system_prompt(role):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    return (
        f"{role}\n\n"
        f"Сейчас: {now}.\n"
        "Ты пишешь в чате ВКонтакте от лица сообщества. Отвечай кратко и по делу, "
        "если не просят длинного ответа. Используй живой язык. "
        "Пересланные сообщения помечены «[переслано от …]», посты — «[запись …]», "
        "картинки — «[фото]»; сами картинки приложены к запросу, если они есть. "
        "Не подписывайся своим именем в начале ответа."
    )


def format_context(rows, max_chars=MAX_CONTEXT_CHARS):
    """rows: [(from_id, is_bot, text, ts, images), ...] — старые → новые."""
    lines = []
    for from_id, is_bot, text, ts, _images in rows:
        if not text:
            continue
        who = BOT_NAME if is_bot or from_id == BOT_GROUP_ID else display_name(from_id)
        t = datetime.fromtimestamp(ts).strftime("%d.%m %H:%M")
        lines.append(f"[{t}] {who}: {text}")
    # не даём истории раздуть запрос: выбрасываем самые старые строки
    while lines and sum(len(l) + 1 for l in lines) > max_chars:
        lines.pop(0)
    return "\n".join(lines)


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


def llm_call(system, user_text, max_tokens=800, images=()):
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


def ask_gran(peer_id, new_text, context_rows, images=()):
    system = build_system_prompt(db_get_role(peer_id))
    ctx = format_context(context_rows)
    log.debug("КОНТЕКСТ peer=%s:\n%s", peer_id, ctx)

    history = f"ИСТОРИЯ:\n{ctx}\n\n" if ctx else "Истории нет — это первое сообщение.\n\n"
    prompt = (
        "Ниже история чата (старые сообщения сверху, новые снизу) и новое "
        "сообщение, на которое надо ответить.\n\n"
        f"{history}"
        f"НОВОЕ СООБЩЕНИЕ:\n{new_text}\n\n"
        + ("К запросу приложены картинки из нового сообщения и последних сообщений истории.\n\n"
           if images else "")
        + "Ответь как Грань."
    )
    return clean_answer(llm_call(system, prompt, images=images))


def summarize(peer_id, count):
    """Пересказ последних N сообщений — без фильтра по времени."""
    system = build_system_prompt(db_get_role(peer_id))
    rows = db_get_context(peer_id, min(count, SUMMARY_MAX_INPUT))
    if not rows:
        return "Нечего пересказывать."
    ctx = format_context(rows, max_chars=SUMMARY_MAX_INPUT * 200)
    prompt = (
        "Ниже — история чата. Сделай краткий, но содержательный пересказ: "
        "о чём говорили, ключевые темы, кто что утверждал, к чему пришли.\n\n"
        f"{ctx}"
    )
    return llm_call(system, prompt, max_tokens=1500)


def summarize_text(text, images=()):
    system = "Ты — полезный помощник, который делает краткое резюме текста."
    prompt = f"Перескажи суть следующего текста кратко и по делу:\n\n{text}"
    return llm_call(system, prompt, max_tokens=1200, images=images)


# ============ Парсинг команд ============
def has_trigger(text):
    return bool(TRIGGER_RE.search(text or ""))


def is_private(peer_id):
    return 0 < peer_id < CHAT_PEER_OFFSET


def is_reply_to_bot(message):
    rp = message.get("reply_message")
    return bool(rp) and rp.get("from_id") == BOT_GROUP_ID


def is_addressed_to_bot(message):
    """Обращение к боту: слово «грань», reply на сообщение бота или личка."""
    return (has_trigger(message.get("text"))
            or is_reply_to_bot(message)
            or (RESPOND_IN_PRIVATE and is_private(message.get("peer_id", 0))))


def should_respond(message_ts):
    return (int(time.time()) - message_ts) <= FRESHNESS_MINUTES * 60


def extract_role(text):
    m = re.search(r"/роль\s*\(([^)]*)\)", text, re.IGNORECASE | re.DOTALL)
    return m.group(1).strip() if m else None


def extract_summary_count(text):
    m = re.search(r"/пересказ\s+(\d+)", text, re.IGNORECASE)
    return int(m.group(1)) if m else None


def is_summary_request(text):
    low = (text or "").lower()
    return ("грань, перескажи" in low) or ("грань, о чём тут" in low) \
        or ("грань, о чем тут" in low)


def is_forget_request(text):
    return bool(re.search(r"(^|\s)/забудь\b", text or "", re.IGNORECASE))


def is_stop_request(text):
    return STOP_PHRASE in (text or "").lower()


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


# ============ Обработчик ============
def handle_message(message):
    global STOP_REQUESTED

    peer_id = message["peer_id"]
    from_id = message.get("from_id", 0)
    text = message.get("text") or ""
    ts = message.get("date", int(time.time()))

    # имена авторов (в т.ч. пересланных и постов) — одним пакетом до рендеринга
    try:
        ensure_names(collect_author_ids(message))
    except Exception as e:
        log.warning("ensure_names: %s", e)

    rowid, full_text, images = store_vk_message(peer_id, message)

    if from_id == BOT_GROUP_ID or from_id < 0:
        return  # свои сообщения и сообщения других ботов только сохраняем

    # ---- стоп-машина ----
    if is_stop_request(text):
        vk_send(peer_id, "Останавливаюсь.", reply_to_msg=message)
        log.info("Команда остановки от peer=%s", peer_id)
        db_state_set("stopped_at", int(time.time()))
        STOP_REQUESTED = True
        return

    # ---- /роль (...) ----
    new_role = extract_role(text)
    if new_role:
        db_set_role(peer_id, new_role)
        vk_send(peer_id, "Роль обновлена.", reply_to_msg=message)
        return

    # ---- /забудь ---- (сбросить контекст беседы, история в БД остаётся)
    if is_forget_request(text):
        db_state_set(f"forget_{peer_id}", ts + 1)
        vk_send(peer_id, "Хорошо, начинаю с чистого листа.", reply_to_msg=message)
        return

    # ---- /пересказ N ---- (без фильтра по времени)
    cnt = extract_summary_count(text)
    if cnt:
        cnt = min(max(cnt, 1), SUMMARY_MAX_INPUT)
        vk_send(peer_id, f"Пересказ последних {cnt} сообщений:\n\n{summarize(peer_id, cnt)}",
                reply_to_msg=message)
        return

    if not is_addressed_to_bot(message):
        return

    # ---- пересказ reply / пересланного / поста ----
    if has_trigger(text) and is_summary_request(text):
        target, target_images = quoted_content(message)
        if target:
            vk_send(peer_id, summarize_text(target, target_images) or "Не получилось пересказать.",
                    reply_to_msg=message)
            return

    if not should_respond(ts):
        log.info("Сообщение старое — не отвечаем.")
        return

    forget_ts = int(db_state_get(f"forget_{peer_id}", 0))
    since = int(time.time()) - CONTEXT_MEMORY_HOURS * 3600
    ctx_rows = db_get_context(peer_id, CONTEXT_SIZE, since_ts=since,
                              min_rows=CONTEXT_MIN_MESSAGES, exclude_id=rowid,
                              after_ts=forget_ts)

    # картинки: из нового сообщения, затем из самых свежих сообщений истории
    all_images = list(images)
    for row in reversed(ctx_rows):
        if len(all_images) >= MAX_IMAGES_PER_REQUEST:
            break
        all_images.extend(row[4])

    answer = ask_gran(peer_id, full_text, ctx_rows, all_images) or "Не получилось сформулировать ответ."
    vk_send(peer_id, answer, reply_to_msg=message)


# ============ Main ============
def init():
    """Подключение к БД, ВК и LLM. Импорты здесь, чтобы тесты не требовали токенов."""
    global DB, vk, llm_client, BOT_GROUP_ID, VK_USER_TOKEN
    from dotenv import load_dotenv
    import vk_api
    from vk_api.bot_longpoll import VkBotLongPoll
    from openai import OpenAI

    load_dotenv()
    group_id = int(os.getenv("VK_GROUP_ID"))
    BOT_GROUP_ID = -group_id
    VK_USER_TOKEN = os.getenv("VK_USER_TOKEN") or None
    DB = db_init(DB_PATH)
    migrate_from_bot3()
    session = vk_api.VkApi(token=os.getenv("VK_TOKEN"))
    vk = session.get_api()
    llm_client = OpenAI(api_key=os.getenv("PROXYAPI_KEY"), base_url=PROXYAPI_BASE_URL)
    return VkBotLongPoll(session, group_id)


def _graceful_signal(signum, frame):
    global STOP_REQUESTED
    log.info("Получен сигнал %s, завершаюсь...", signum)
    STOP_REQUESTED = True


def main():
    from vk_api.bot_longpoll import VkBotEventType

    signal.signal(signal.SIGINT, _graceful_signal)
    signal.signal(signal.SIGTERM, _graceful_signal)

    longpoll = init()
    load_history()
    log.info("Слушаем Long Poll... (для остановки: '%s')", STOP_PHRASE)

    while not STOP_REQUESTED:
        try:
            for event in longpoll.listen():
                if STOP_REQUESTED:
                    break
                try:
                    if event.type == VkBotEventType.MESSAGE_NEW:
                        handle_message(event.object.message)
                    elif event.type == VkBotEventType.MESSAGE_REPLY:
                        # исходящие сообщения сообщества (в т.ч. написанные админом вручную)
                        store_vk_message(event.object["peer_id"], dict(event.object))
                    if STOP_REQUESTED:
                        break
                except Exception as e:
                    log.exception("handle: %s", e)
        except Exception as e:
            log.error("longpoll error: %s", e)
            time.sleep(3)

    log.info("Бот остановлен.")
    try:
        DB.close()
    except Exception:
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
