"""
Воспроизведение главного бага контекста bot3.py (задача #11).

В беседах ВК бот сообщества получает у входящих сообщений id = 0
(уникален только conversation_message_id). В bot3 колонка vk_msg_id
объявлена UNIQUE, а вставка идёт через INSERT OR IGNORE — поэтому в каждой
беседе сохраняется только первое сообщение с id = 0, остальные молча
отбрасываются, и контекст «не пополняется».

Запуск: python3 experiments/bot3_context_bug.py
"""
import sqlite3

con = sqlite3.connect(":memory:")
# схема и вставка — дословно из bot3.py
con.execute("""
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

def db_add_message(peer_id, from_id, is_bot, text, ts, vk_msg_id):
    con.execute(
        "INSERT OR IGNORE INTO messages(peer_id,from_id,is_bot,text,ts,vk_msg_id) "
        "VALUES(?,?,?,?,?,?)",
        (peer_id, from_id, int(is_bot), text, ts, vk_msg_id),
    )

# три сообщения в беседе, как их отдаёт Bots Long Poll (id = 0)
chat = 2000000001
for i, text in enumerate(["привет", "как дела?", "грань, что думаешь?"]):
    db_add_message(chat, 100 + i, 0, text, 1_700_000_000 + i, 0)

rows = con.execute("SELECT text FROM messages WHERE peer_id=?", (chat,)).fetchall()
print("Отправлено в БД: 3, сохранено:", len(rows), [r[0] for r in rows])
assert len(rows) == 1, "баг не воспроизвёлся"
print("Баг воспроизведён: сообщения с id=0 отбрасываются UNIQUE-ограничением.")
