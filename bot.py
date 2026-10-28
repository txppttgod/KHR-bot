import os
import json
import csv
import io
import time
import requests
from http.server import BaseHTTPRequestHandler, HTTPServer

TOKEN = os.environ["BOT_TOKEN"]
APP_URL = os.environ.get("APP_URL", "https://txppttgod.github.io/index.html/club_app.html")
GROUP_APP_LINK = os.environ.get("GROUP_APP_LINK", "https://t.me/Khin_raci_bot/Khinkali_Racing")
BOT_USERNAME = os.environ.get("BOT_USERNAME", "Khin_raci_bot").lower()
PILOTS_CSV = os.environ.get(
    "PILOTS_CSV",
    "https://docs.google.com/spreadsheets/d/e/2PACX-1vR4hL-1v8UUlMxTynXm6gtaCK0KLgjAtxmxONulVDd0HWh4khlTbzaYICkV8xr_3w/pub?gid=1543070136&single=true&output=csv",
)
SB_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SB_KEY = os.environ.get("SUPABASE_KEY", "")
API = f"https://api.telegram.org/bot{TOKEN}"

CHUNK = 20              # сколько участников тегать в одном сообщении
WARN_LIMIT = 3          # сколько предупреждений до автобана
MUTE_DEFAULT_MIN = 60   # мут по умолчанию, в минутах
MOD_CMDS = {"/ban", "/unban", "/mute", "/unmute", "/warn", "/unwarn"}
PERM_KEYS = [
    "can_send_messages", "can_send_audios", "can_send_documents", "can_send_photos",
    "can_send_videos", "can_send_video_notes", "can_send_voice_notes", "can_send_polls",
    "can_send_other_messages", "can_add_web_page_previews", "can_change_info",
    "can_invite_users", "can_pin_messages", "can_manage_topics",
]
PERMS_NONE = {k: False for k in PERM_KEYS}
PERMS_ALL = {k: True for k in PERM_KEYS}

# ---------- База данных (Supabase) ----------
SB_HEADERS = {"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}", "Content-Type": "application/json"}
DB_OK = False  # пока база не прочитана, не сохраняем, чтобы не стереть данные


def db_load():
    try:
        r = requests.get(f"{SB_URL}/rest/v1/bot_state?id=eq.main&select=data", headers=SB_HEADERS, timeout=10)
        rows = r.json()
        if not isinstance(rows, list) or not rows:
            print("В базе нет строки bot_state с id=main. Выполни SQL из инструкции.")
            return False, {}
        return True, rows[0]["data"] or {}
    except Exception as e:
        print("Ошибка загрузки из базы:", e)
        return False, {}


def db_save(data):
    try:
        requests.patch(f"{SB_URL}/rest/v1/bot_state?id=eq.main", headers=SB_HEADERS,
                       json={"data": data}, timeout=10)
    except Exception as e:
        print("Ошибка сохранения в базу:", e)


def load_state():
    global DB_OK
    ok, d = db_load()
    DB_OK = ok
    members = {int(c): {int(u): v for u, v in m.items()} for c, m in d.get("members", {}).items()}
    stats = {int(c): {int(u): v for u, v in m.items()} for c, m in d.get("stats", {}).items()}
    warns = {}
    for k, n in d.get("warns", {}).items():
        c, u = k.split("_")
        warns[(int(c), int(u))] = n
    return members, stats, warns


def save_state():
    if not DB_OK:
        return
    db_save({
        "members": {str(c): {str(u): v for u, v in m.items()} for c, m in members.items()},
        "stats": {str(c): {str(u): v for u, v in m.items()} for c, m in stats.items()},
        "warns": {f"{c}_{u}": n for (c, u), n in warns.items()},
    })


members, stats, warns = load_state()
_cache = {"time": 0, "rows": []}


# ---------- Telegram ----------
def api(method, payload):
    try:
        return requests.post(f"{API}/{method}", json=payload, timeout=10).json()
    except Exception as e:
        print("Ошибка API:", e)
        return {}


def send(chat_id, text, reply_to=None, with_button=True, entities=None, group=False):
    payload = {"chat_id": chat_id, "text": text}
    if entities:
        payload["entities"] = entities
    if with_button:
        # В группах кнопка-ссылка надёжнее, web_app там не всегда показывается
        if group:
            button = {"text": "📊 Открыть таблицы клуба", "url": GROUP_APP_LINK}
        else:
            button = {"text": "📊 Открыть таблицы клуба", "web_app": {"url": APP_URL}}
        payload["reply_markup"] = {"inline_keyboard": [[button]]}
    if reply_to:
        payload["reply_parameters"] = {"message_id": reply_to}
    api("sendMessage", payload)


def utf16_len(s):
    # Telegram считает смещения в UTF-16, эмодзи занимают 2 символа
    return len(s.encode("utf-16-le")) // 2


def remember(chat_id, user):
    if not user or user.get("is_bot"):
        return
    members.setdefault(chat_id, {})[user["id"]] = {
        "id": user["id"],
        "first_name": user.get("first_name", "участник"),
    }


def load_admins(chat_id):
    res = api("getChatAdministrators", {"chat_id": chat_id})
    for m in res.get("result", []):
        remember(chat_id, m.get("user"))


def is_admin(chat_id, user_id):
    res = api("getChatMember", {"chat_id": chat_id, "user_id": user_id})
    return res.get("result", {}).get("status") in ("creator", "administrator")


def call_everyone(msg):
    chat_id = msg["chat"]["id"]
    sender_id = msg.get("from", {}).get("id")
    load_admins(chat_id)
    users = [u for u in members.get(chat_id, {}).values() if u["id"] != sender_id]
    total = api("getChatMemberCount", {"chat_id": chat_id}).get("result", 0)

    if not users:
        send(chat_id, "Пока никого не знаю. Участники попадают в список, когда пишут в чат после того, как бота добавили.",
             reply_to=msg["message_id"], with_button=False)
        return

    for start in range(0, len(users), CHUNK):
        chunk = users[start:start + CHUNK]
        text = "🚨 Общий сбор! " if start == 0 else ""
        entities = []
        for u in chunk:
            name = u["first_name"]
            entities.append({
                "type": "text_mention",
                "offset": utf16_len(text),
                "length": utf16_len(name),
                "user": {"id": u["id"], "first_name": name},
            })
            text += name + " "
        send(chat_id, text.rstrip(), with_button=False, entities=entities)

    info = f"Отмечено {len(users)} из {total - 1 if total else '?'} участников."
    if total and len(users) < total - 1:
        info += " Кого нет в списке — пусть напишет любое сообщение в чат, и бот его запомнит."
    send(chat_id, info, with_button=False)


def handle_chat_member(cm):
    # Вход и выход участников (нужны права админа и подписка на chat_member)
    chat_id = cm["chat"]["id"]
    new = cm.get("new_chat_member", {})
    user = new.get("user", {})
    status = new.get("status")
    if status in ("member", "administrator", "creator", "restricted"):
        remember(chat_id, user)
    elif status in ("left", "kicked"):
        members.get(chat_id, {}).pop(user.get("id"), None)


# ---------- Статистика ----------
def get_entry(chat_id, uid, name):
    return stats.setdefault(chat_id, {}).setdefault(uid, {
        "name": name, "count": 0, "warns": 0, "mutes": 0, "bans": 0})


def count_activity(msg):
    user = msg.get("from") or {}
    if user.get("is_bot"):
        return
    entry = get_entry(msg["chat"]["id"], user["id"], user.get("first_name", "участник"))
    entry["name"] = user.get("first_name", "участник")
    entry["count"] += 1


def record_action(chat_id, uid, name, action):
    # action: "warn", "mute", "ban"
    key = {"warn": "warns", "mute": "mutes", "ban": "bans"}[action]
    get_entry(chat_id, uid, name)[key] += 1


def reply_top(msg):
    chat_id = msg["chat"]["id"]
    rows = sorted(stats.get(chat_id, {}).values(), key=lambda d: d["count"], reverse=True)[:10]
    if not rows:
        text = "Пока нет статистики. Она считается с момента, как бот добавлен в группу."
    else:
        text = "📊 Топ-10 по сообщениям:\n" + "\n".join(
            f"{i}. {d['name']} — {d['count']}" for i, d in enumerate(rows, 1))
    send(chat_id, text, reply_to=msg["message_id"], with_button=False)


def reply_mystats(msg):
    chat_id = msg["chat"]["id"]
    # Без ответа — статистика отправителя; ответом на сообщение — статистика того участника
    target = (msg.get("reply_to_message") or {}).get("from") or msg.get("from") or {}
    uid = target.get("id")
    name = target.get("first_name", "участник")
    board = stats.get(chat_id, {})
    e = board.get(uid)
    if not e:
        text = f"{name}: пока нет данных в статистике."
    else:
        ranked = sorted(board.items(), key=lambda kv: kv[1]["count"], reverse=True)
        place = [k for k, _ in ranked].index(uid) + 1
        text = (f"📈 Статистика: {e['name']}\n"
                f"Сообщений: {e['count']} (место {place} из {len(ranked)})\n"
                f"Предупреждений: {e['warns']}\n"
                f"Мутов: {e['mutes']}\n"
                f"Банов: {e['bans']}")
    send(chat_id, text, reply_to=msg["message_id"], with_button=False)


# ---------- Модерация ----------
def moderate(msg, cmd, args):
    chat_id = msg["chat"]["id"]
    if not is_admin(chat_id, msg["from"]["id"]):
        send(chat_id, "Эта команда только для админов группы.", reply_to=msg["message_id"], with_button=False)
        return
    target_msg = msg.get("reply_to_message")
    if not target_msg or not target_msg.get("from"):
        send(chat_id, "Ответь этой командой на сообщение нарушителя.", reply_to=msg["message_id"], with_button=False)
        return
    target = target_msg["from"]
    if target.get("is_bot") or is_admin(chat_id, target["id"]):
        send(chat_id, "Нельзя применить это к админу или боту.", reply_to=msg["message_id"], with_button=False)
        return

    uid = target["id"]
    name = target.get("first_name", "участник")
    has_minutes = cmd == "mute" and args and args[0].isdigit()
    reason = " ".join(args[1:] if has_minutes else args) or "не указана"

    if cmd == "ban":
        api("banChatMember", {"chat_id": chat_id, "user_id": uid})
        record_action(chat_id, uid, name, "ban")
        text = f"🔨 {name} забанен. Причина: {reason}"
    elif cmd == "unban":
        api("unbanChatMember", {"chat_id": chat_id, "user_id": uid, "only_if_banned": True})
        text = f"✅ {name} разбанен."
    elif cmd == "mute":
        minutes = max(1, int(args[0])) if has_minutes else MUTE_DEFAULT_MIN
        api("restrictChatMember", {
            "chat_id": chat_id, "user_id": uid, "permissions": PERMS_NONE,
            "until_date": int(time.time()) + minutes * 60,
        })
        record_action(chat_id, uid, name, "mute")
        text = f"🔇 {name} в муте на {minutes} мин. Причина: {reason}"
    elif cmd == "unmute":
        api("restrictChatMember", {"chat_id": chat_id, "user_id": uid, "permissions": PERMS_ALL})
        text = f"🔊 {name} снова может писать."
    elif cmd == "warn":
        key = (chat_id, uid)
        warns[key] = warns.get(key, 0) + 1
        record_action(chat_id, uid, name, "warn")
        if warns[key] >= WARN_LIMIT:
            api("banChatMember", {"chat_id": chat_id, "user_id": uid})
            warns[key] = 0
            record_action(chat_id, uid, name, "ban")
            text = f"⚠️ {name}: {WARN_LIMIT} предупреждения, автобан. Причина: {reason}"
        else:
            text = f"⚠️ {name}, предупреждение {warns[key]} из {WARN_LIMIT}. Причина: {reason}"
    elif cmd == "unwarn":
        warns[(chat_id, uid)] = 0
        text = f"Предупреждения {name} сняты."
    else:
        return
    send(chat_id, text, reply_to=target_msg["message_id"], with_button=False)


# ---------- Профиль пилота из таблицы ----------
def get_pilots():
    # Таблицу читаем не чаще раза в минуту
    if time.time() - _cache["time"] < 60:
        return _cache["rows"]
    try:
        r = requests.get(PILOTS_CSV, timeout=10)
        r.encoding = "utf-8"
        rows = list(csv.reader(io.StringIO(r.text)))
        _cache["rows"] = [x for x in rows if x and x[0].strip().isdigit()]
        _cache["time"] = time.time()
    except Exception as e:
        print("Ошибка чтения таблицы:", e)
    return _cache["rows"]


def col(r, i):
    return r[i].strip() if len(r) > i and r[i].strip() else "—"


def find_pilot(username):
    uname = username.lower().lstrip("@")
    for r in get_pilots():
        tg = col(r, 2).lower().lstrip("@")
        if tg != "—" and tg == uname:
            return r
    return None


def reply_profile(msg):
    parts = msg.get("text", "").split()
    username = parts[1] if len(parts) > 1 else (msg.get("from") or {}).get("username", "")
    username = username.lstrip("@")
    if not username:
        text = "У тебя не задан username в Телеграме. Укажи его в настройках и повтори /profile."
    else:
        r = find_pilot(username)
        if r:
            text = (
                "👤 Профиль пилота\n"
                f"№ {col(r, 0)} — {col(r, 1)}\n"
                f"Телеграм: @{username}\n"
                f"Сдано заездов: {col(r, 3)}\n"
                f"Побед (УТС): {col(r, 4)}\n"
                f"Всего баллов: {col(r, 5)}\n"
                f"Роль: {col(r, 6)}"
            )
        else:
            text = f"@{username} пока нет в таблице. Попроси админа добавить тебя в «Статистику пилотов»."
    send(msg["chat"]["id"], text, reply_to=msg["message_id"], with_button=False)


HELP_TEXT = """📖 Команды бота

Для всех:
/help — это сообщение
/all или слово «созвать» — отметить всех участников, которых бот знает
/profile или /me — твоя карточка пилота из таблицы (можно /profile @ник)
/top — топ-10 участников по сообщениям
/stats или /mystats — статистика (ответом на сообщение — статистика того участника)
/start или /app — кнопка с таблицами клуба
@Khin_raci_bot — то же, что /app

Для админов группы (ответом на сообщение нарушителя):
/ban причина — забанить
/unban — разбанить
/mute минуты причина — мут (по умолчанию 60 минут)
/unmute — снять мут
/warn причина — предупреждение, на {limit}-м бан
/unwarn — сбросить предупреждения

В личке с ботом: /start и /profile.""".format(limit=WARN_LIMIT)


# ---------- Основная обработка ----------
def handle(msg):
    chat = msg["chat"]
    chat_id = chat["id"]
    is_group = chat["type"] != "private"

    # Участники: вошедшие и вышедшие
    for u in msg.get("new_chat_members", []):
        remember(chat_id, u)
    if "left_chat_member" in msg:
        members.get(chat_id, {}).pop(msg["left_chat_member"]["id"], None)
    service = "new_chat_members" in msg or "left_chat_member" in msg
    if is_group and "from" in msg and not service:
        remember(chat_id, msg["from"])
        count_activity(msg)

    text = msg.get("text", "")
    if not text:
        return
    lower = text.lower()
    words = lower.split()
    first = words[0].split("@")[0] if words else ""
    args = text.split()[1:]

    if first == "/help":
        send(chat_id, HELP_TEXT, reply_to=msg["message_id"], with_button=False)
        return
    if first in ("/profile", "/me"):
        reply_profile(msg)
        return
    if first == "/top":
        reply_top(msg)
        return
    if first in ("/stats", "/mystats"):
        reply_mystats(msg)
        return

    if chat["type"] == "private":
        if first == "/start":
            send(chat_id, "Привет! Нажми кнопку ниже, чтобы открыть таблицы клуба.")
        return

    if first in MOD_CMDS:
        moderate(msg, first[1:], args)
        return

    if lower.startswith("/all") or "созвать" in lower:
        call_everyone(msg)
        return

    if first in ("/start", "/app") or f"@{BOT_USERNAME}" in lower:
        send(chat_id, "Таблицы клуба: правила, расписание, пилоты и сводка.",
             reply_to=msg["message_id"], group=True)


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            update = json.loads(self.rfile.read(length) or b"{}")
            if "chat_member" in update:
                handle_chat_member(update["chat_member"])
            elif "message" in update:
                handle(update["message"])
        except Exception as e:
            print("Ошибка:", e)
        save_state()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"bot is running")

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()
    
    
                

