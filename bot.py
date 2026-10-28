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
API = f"https://api.telegram.org/bot{TOKEN}"
CHUNK = 20  # сколько участников тегать в одном сообщении

# chat_id -> {user_id: {"id": ..., "first_name": ...}}
members = {}


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
        # В группах кнопка-ссылка на приложение надёжнее, web_app там не всегда показывается
        if group:
            button = {"text": "📊 Открыть таблицы клуба", "url": GROUP_APP_LINK}
        else:
            button = {"text": "📊 Открыть таблицы клуба", "web_app": {"url": APP_URL}}
        payload["reply_markup"] = {"inline_keyboard": [[button]]}
    if reply_to:
        payload["reply_parameters"] = {"message_id": reply_to}
    api("sendMessage", payload)


def utf16_len(s):
    # Telegram считает смещения в UTF-16, поэтому эмодзи считаются как 2 символа
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


def call_everyone(msg):
    chat_id = msg["chat"]["id"]
    sender_id = msg.get("from", {}).get("id")
    load_admins(chat_id)
    users = [u for u in members.get(chat_id, {}).values() if u["id"] != sender_id]

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


PILOTS_CSV = os.environ.get(
    "PILOTS_CSV",
    "https://docs.google.com/spreadsheets/d/e/2PACX-1vR4hL-1v8UUlMxTynXm6gtaCK0KLgjAtxmxONulVDd0HWh4khlTbzaYICkV8xr_3w/pub?gid=1543070136&single=true&output=csv",
)
_cache = {"time": 0, "rows": []}


def get_pilots():
    # Таблицу читаем не чаще раза в минуту, чтобы не дёргать Google при каждом запросе
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
    if len(parts) > 1:
        username = parts[1]
    else:
        username = (msg.get("from") or {}).get("username", "")
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


def handle(update):
    msg = update.get("message")
    if not msg:
        return
    chat = msg["chat"]
    chat_id = chat["id"]

    # Запоминаем участников: вошедших, пишущих и вышедших
    for u in msg.get("new_chat_members", []):
        remember(chat_id, u)
    if "left_chat_member" in msg:
        members.get(chat_id, {}).pop(msg["left_chat_member"]["id"], None)
    if chat["type"] != "private" and "from" in msg:
        remember(chat_id, msg["from"])

    text = msg.get("text", "")
    if not text:
        return
    lower = text.lower()

    # Профиль пилота по юзернейму: /profile или /me (можно /profile @ник)
    first = lower.split()[0].split("@")[0] if lower.split() else ""
    if first in ("/profile", "/me"):
        reply_profile(msg)
        return

    # Личные сообщения: ответ на /start
    if chat["type"] == "private":
        if lower.startswith("/start"):
            send(chat_id, "Привет! Нажми кнопку ниже, чтобы открыть таблицы клуба.")
        return

    # Группа: сбор всех участников по слову «созвать» или команде /all
    if lower.startswith("/all") or "созвать" in lower:
        call_everyone(msg)
        return

    # Группа: ответ на /start или на упоминание бота
    if lower.startswith("/start") or lower.startswith("/app") or f"@{BOT_USERNAME}" in lower:
        send(chat_id, "Таблицы клуба: правила, расписание, пилоты и сводка.",
             reply_to=msg["message_id"], group=True)


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            handle(json.loads(self.rfile.read(length) or b"{}"))
        except Exception as e:
            print("Ошибка:", e)
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


