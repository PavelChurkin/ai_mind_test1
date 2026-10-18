# bot.py
import os
import re
import sys
import time
import signal
import sqlite3
import logging
from datetime import datetime
from dotenv import load_dotenv
import vk_api
from vk_api.bot_longpoll import VkBotLongPoll, VkBotEventType
from openai import OpenAI

load_dotenv()

# ============ НАСТРОЙКИ ============
CONTEXT_SIZE = 30                 # максимум последних сообщений в контексте
CONTEXT_MEMORY_MINUTES = 30        # помнить только сообщения за последние N минут
INITIAL_LOAD = 1000                # сколько сообщений загрузить при первом старте
MAX_CONTEXT_LIMIT = 4000
TRIGGER_WORD = "грань"
FRESHNESS_MINUTES = 10             # если обращение старше — не отвечаем
SUMMARY_MAX_INPUT = 5000
STOP_PHRASE = "грань, стоп машина"
DB_PATH = "gran.db"

PROXYAPI_BASE_URL = "https://api.proxyapi.ru/v1"
MODEL_NAME = "deepseek/deepseek-v4-flash-vision-exp"
# ===================================

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("gran")

VK_TOKEN = os.getenv("VK_TOKEN")
VK_GROUP_ID = int(os.getenv("VK_GROUP_ID"))
PROXYAPI_KEY = os.getenv("PROXYAPI_KEY")

DEFAULT_ROLE = (
    "Ты — Грань, участник беседы. Общайся живо, по-человечески, "
    "не будь слишком формальной, подстраивайся под тон беседы."
)

STOP_REQUESTED = False

# ============ LLM клиент ============
llm_client = OpenAI(api_key=PROXYAPI_KEY, base_url=PROXYAPI_BASE_URL)

# ============ БД ============
def db_init():
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA journal_mode=WAL")
    cur = con.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            peer_id INTEGER NOT NULL,
            from_id INTEGER NOT NULL,
            is_bot INTEGER NOT NULL DEFAULT 0,
            text TEXT,
            ts INTEGER NOT NULL,
            vk_msg_id INTEGER UNIQUE
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_peer_ts ON messages(peer_id, ts)")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS state (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS roles (
            peer_id INTEGER PRIMARY KEY,
            role TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            first_name TEXT,
            last_name TEXT,
            updated_at INTEGER
        )
    """)
    con.commit()
    return con

DB = db_init()

def db_add_message(peer_id, from_id, is_bot, text, ts, vk_msg_id):
    try:
        DB.execute(
            "INSERT OR IGNORE INTO messages(peer_id,from_id,is_bot,text,ts,vk_msg_id) "
            "VALUES(?,?,?,?,?,?)",
            (peer_id, from_id, int(is_bot), text, ts, vk_msg_id),
        )
        DB.commit()
    except Exception as e:
        log.warning("db_add_message: %s", e)

def db_get_context(peer_id, limit, since_ts=None, fallback_to_last=True):
    """
    Возвращает контекст в правильном порядке: старые сверху, новые снизу.
    since_ts — если задан, обрезаем всё, что старше.
    fallback_to_last — если после фильтра по времени пусто, вернуть одно последнее сообщение.
    """
    if since_ts is not None:
        cur = DB.execute(
            "SELECT from_id,is_bot,text,ts FROM messages "
            "WHERE peer_id=? AND ts>=? "
            "ORDER BY ts ASC, id ASC LIMIT ?",
            (peer_id, since_ts, limit),
        )
        rows = cur.fetchall()
        if rows or not fallback_to_last:
            return rows
        # fallback: одно последнее сообщение
        cur = DB.execute(
            "SELECT from_id,is_bot,text,ts FROM messages "
            "WHERE peer_id=? ORDER BY ts DESC, id DESC LIMIT 1",
            (peer_id,),
        )
        rows = cur.fetchall()
        return rows

    # без фильтра по времени
    cur = DB.execute(
        "SELECT from_id,is_bot,text,ts FROM messages "
        "WHERE peer_id=? ORDER BY ts DESC, id DESC LIMIT ?",
        (peer_id, limit),
    )
    rows = cur.fetchall()
    rows.reverse()
    return rows

def db_get_role(peer_id):
    cur = DB.execute("SELECT role FROM roles WHERE peer_id=?", (peer_id,))
    r = cur.fetchone()
    return r[0] if r else DEFAULT_ROLE

def db_set_role(peer_id, role):
    DB.execute(
        "INSERT INTO roles(peer_id, role) VALUES(?,?) "
        "ON CONFLICT(peer_id) DO UPDATE SET role=excluded.role",
        (peer_id, role),
    )
    DB.commit()

def db_state_get(key, default=None):
    cur = DB.execute("SELECT value FROM state WHERE key=?", (key,))
    r = cur.fetchone()
    return r[0] if r else default

def db_state_set(key, value):
    DB.execute(
        "INSERT INTO state(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )
    DB.commit()

def db_get_user_name(user_id):
    cur = DB.execute(
        "SELECT first_name, last_name FROM users WHERE user_id=?", (user_id,)
    )
    r = cur.fetchone()
    if not r:
        return None
    name = " ".join(x for x in (r[0], r[1]) if x).strip()
    return name or None

def db_upsert_user(user_id, first_name, last_name):
    DB.execute(
        "INSERT INTO users(user_id, first_name, last_name, updated_at) "
        "VALUES(?,?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET "
        "first_name=excluded.first_name, last_name=excluded.last_name, updated_at=excluded.updated_at",
        (user_id, first_name or "", last_name or "", int(time.time())),
    )
    DB.commit()

# ============ VK ============
vk_session = vk_api.VkApi(token=VK_TOKEN)
vk = vk_session.get_api()
longpoll = VkBotLongPoll(vk_session, VK_GROUP_ID)
BOT_GROUP_ID = -VK_GROUP_ID

def vk_send(peer_id, text, reply_to=None):
    try:
        params = dict(peer_id=peer_id, message=text,
                      random_id=int(time.time() * 1000) % (2**31))
        if reply_to:
            params["reply_to"] = reply_to
        vk.messages.send(**params)
    except Exception as e:
        log.error("vk_send: %s", e)

def vk_fetch_users(user_ids):
    """Пакетно тянем имена пользователей (до 1000 за раз)."""
    if not user_ids:
        return
    ids = [str(uid) for uid in user_ids if uid and uid > 0]
    for i in range(0, len(ids), 900):
        chunk = ids[i:i + 900]
        try:
            users = vk.users.get(user_ids=",".join(chunk), fields="")
            for u in users:
                db_upsert_user(u["id"], u.get("first_name", ""), u.get("last_name", ""))
        except Exception as e:
            log.warning("vk_fetch_users: %s", e)

def ensure_user_cached(user_id):
    """Дозагрузка имени одного пользователя, если его нет в БД или данные устарели (>24ч)."""
    if not user_id or user_id < 0:
        return
    cur = DB.execute("SELECT updated_at FROM users WHERE user_id=?", (user_id,))
    r = cur.fetchone()
    if r and (int(time.time()) - r[0]) < 86400:
        return
    vk_fetch_users([user_id])

def refresh_users_for_peer(peer_id):
    """Собрать все from_id из сообщений беседы и подтянуть имена."""
    cur = DB.execute(
        "SELECT DISTINCT from_id FROM messages WHERE peer_id=? AND from_id>0",
        (peer_id,),
    )
    ids = [r[0] for r in cur.fetchall()]
    vk_fetch_users(ids)

# ============ Первичная загрузка ============
def initial_load_if_needed():
    if db_state_get("initial_load_done") == "1":
        log.info("История уже загружена ранее — пропускаем импорт.")
        return

    log.info("Первичная загрузка истории (до %d сообщений на диалог)...", INITIAL_LOAD)
    try:
        dialogs = vk.messages.getConversations(count=200)["items"]
    except Exception as e:
        log.error("getConversations: %s", e)
        return

    total = 0
    for d in dialogs:
        peer_id = d["conversation"]["peer"]["id"]
        try:
            history = vk.messages.getHistory(
                peer_id=peer_id, count=min(INITIAL_LOAD, 200)
            )["items"]
        except Exception as e:
            log.warning("getHistory peer=%s: %s", peer_id, e)
            continue

        collected = list(history)
        offset = len(history)
        while len(collected) < INITIAL_LOAD and history:
            try:
                history = vk.messages.getHistory(
                    peer_id=peer_id, count=200, offset=offset
                )["items"]
            except Exception:
                break
            if not history:
                break
            collected.extend(history)
            offset += len(history)

        # VK отдаёт от новых к старым — разворачиваем, чтобы id-шники росли
        # вместе с реальным временем (нужно для правильной сортировки по id).
        collected.reverse()

        for m in collected:
            text = m.get("text") or ""
            from_id = m.get("from_id", 0)
            is_bot = 1 if from_id == BOT_GROUP_ID else 0
            db_add_message(peer_id, from_id, is_bot, text,
                           m.get("date", 0), m.get("id"))
        total += len(collected)
        log.info("peer=%s загружено %d", peer_id, len(collected))

        refresh_users_for_peer(peer_id)

    db_state_set("initial_load_done", "1")
    log.info("Первичная загрузка завершена, всего %d сообщений.", total)

# ============ LLM ============
def build_system_prompt(role):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    return (
        f"{role}\n\n"
        f"Сейчас: {now}.\n"
        "Ты пишешь в чате ВКонтакте от лица сообщества. Отвечай кратко и по делу, "
        "если не просят длинного ответа. Используй живой язык."
    )

def format_context(rows):
    """rows: [(from_id, is_bot, text, ts), ...] — в порядке старые→новые."""
    lines = []
    for from_id, is_bot, text, ts in rows:
        if not text:
            continue
        if is_bot or from_id == BOT_GROUP_ID:
            who = "Грань разумного"
        else:
            who = db_get_user_name(from_id) or f"user{from_id}"
        # дата с днём и месяцем
        t = datetime.fromtimestamp(ts).strftime("%d.%m %H:%M")
        lines.append(f"[{t}] {who}: {text}")
    return "\n".join(lines)

def llm_call(system, user_text, max_tokens=800):
    try:
        resp = llm_client.chat.completions.create(
            model=MODEL_NAME,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_text},
            ],
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        log.error("llm_call: %s", e)
        return ""

def ask_gran(peer_id, user_text, context_rows):
    role = db_get_role(peer_id)
    system = build_system_prompt(role)
    ctx = format_context(context_rows)

    print(4*'\n'+5*'===')
    print(ctx)

    if ctx:
        prompt = (
            "Ниже история чата (старые сообщения сверху, новые снизу) и новое "
            "сообщение, на которое надо ответить.\n\n"
            f"ИСТОРИЯ:\n{ctx}\n\n"
            f"НОВОЕ СООБЩЕНИЕ:\n{user_text}\n\n"
            "Ответь как Грань."
        )
    else:
        prompt = (
            "Истории нет — это первое сообщение в текущем окне.\n\n"
            f"НОВОЕ СООБЩЕНИЕ:\n{user_text}\n\n"
            "Ответь как Грань."
        )

    return llm_call(system, prompt)

def summarize(peer_id, count):
    """Пересказ последних N сообщений — БЕЗ фильтра по времени."""
    role = db_get_role(peer_id)
    system = build_system_prompt(role)
    rows = db_get_context(peer_id, min(count, SUMMARY_MAX_INPUT), since_ts=None)
    if not rows:
        return "Нечего пересказывать."
    ctx = format_context(rows)
    prompt = (
        "Ниже — история чата. Сделай краткий, но содержательный пересказ: "
        "о чём говорили, ключевые темы, кто что утверждал, к чему пришли.\n\n"
        f"{ctx}"
    )
    return llm_call(system, prompt, max_tokens=1500)

def summarize_text(text):
    system = "Ты — полезный помощник, который делает краткое резюме текста."
    prompt = f"Перескажи суть следующего текста кратко и по делу:\n\n{text}"
    return llm_call(system, prompt, max_tokens=1200)

# ============ Парсинг ============
def get_reply_text(message):
    rp = message.get("reply_message")
    return (rp.get("text") or "") if rp else None

def get_forwarded_text(message):
    fwd = message.get("fwd_messages") or []
    texts = [f.get("text") or "" for f in fwd]
    texts = [t for t in texts if t]
    return "\n\n".join(texts) if texts else None

def has_trigger(text):
    return bool(re.search(r"\bгрань\b", text or "", re.IGNORECASE))

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

def is_stop_request(text):
    return STOP_PHRASE in (text or "").lower()

# ============ Обработчик ============
def handle_message(message):
    global STOP_REQUESTED

    peer_id = message["peer_id"]
    from_id = message["from_id"]
    text = message.get("text") or ""
    ts = message.get("date", int(time.time()))
    vk_msg_id = message.get("id")
    is_bot = 1 if from_id == BOT_GROUP_ID else 0

    db_add_message(peer_id, from_id, is_bot, text, ts, vk_msg_id)

    if is_bot:
        return

    # подтянуть имя отправителя в БД
    try:
        ensure_user_cached(from_id)
    except Exception as e:
        log.warning("ensure_user_cached: %s", e)

    # ---- стоп-машина ----
    if is_stop_request(text):
        vk_send(peer_id, "Останавливаюсь.", reply_to=vk_msg_id)
        log.info("Команда остановки от peer=%s", peer_id)
        db_state_set("stopped_at", int(time.time()))
        STOP_REQUESTED = True
        return

    # ---- /роль (...) ----
    new_role = extract_role(text)
    if new_role:
        db_set_role(peer_id, new_role)
        vk_send(peer_id, "Роль обновлена.", reply_to=vk_msg_id)
        return

    # ---- /пересказ N ---- (без фильтра по времени)
    cnt = extract_summary_count(text)
    if cnt:
        cnt = min(max(cnt, 1), SUMMARY_MAX_INPUT)
        summary = summarize(peer_id, cnt)
        vk_send(peer_id, f"Пересказ последних {cnt} сообщений:\n\n{summary}",
                reply_to=vk_msg_id)
        return

    # ---- триггер "Грань" ----
    if has_trigger(text):
        # пересказ реплая / пересланного
        if is_summary_request(text):
            target = get_reply_text(message) or get_forwarded_text(message)
            if target:
                vk_send(peer_id, summarize_text(target), reply_to=vk_msg_id)
                return

        if not should_respond(ts):
            log.info("Сообщение старое — не отвечаем.")
            return

        # контекст с фильтром по времени: последние N минут
        since = int(time.time()) - CONTEXT_MEMORY_MINUTES * 60
        limit = min(CONTEXT_SIZE, MAX_CONTEXT_LIMIT)
        ctx_rows = db_get_context(peer_id, limit, since_ts=since,
                                  fallback_to_last=True)

        answer = ask_gran(peer_id, text, ctx_rows) or "Не получилось сформулировать ответ."
        vk_send(peer_id, answer, reply_to=vk_msg_id)

# ============ Main ============
def _graceful_signal(signum, frame):
    global STOP_REQUESTED
    log.info("Получен сигнал %s, завершаюсь...", signum)
    STOP_REQUESTED = True

def main():
    signal.signal(signal.SIGINT, _graceful_signal)
    signal.signal(signal.SIGTERM, _graceful_signal)

    initial_load_if_needed()
    log.info("Слушаем Long Poll... (для остановки: 'Грань, стоп машина')")

    while not STOP_REQUESTED:
        try:
            for event in longpoll.listen():
                if STOP_REQUESTED:
                    break
                try:
                    if event.type == VkBotEventType.MESSAGE_NEW:
                        handle_message(event.object.message)
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