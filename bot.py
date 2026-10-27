import os
import json
import requests
from http.server import BaseHTTPRequestHandler, HTTPServer

TOKEN = os.environ["BOT_TOKEN"]
APP_URL = os.environ.get("APP_URL", "https://txppttgod.github.io/index.html/club_app.html")
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


def send(chat_id, text, reply_to=None, with_button=True, entities=None):
    payload = {"chat_id": chat_id, "text": text}
    if entities:
        payload["entities"] = entities
    if with_button:
        payload["reply_markup"] = {
            "inline_keyboard": [[
                {"text": "📊 Открыть таблицы клуба", "web_app": {"url": APP_URL}}
            ]]
        }
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
             reply_to=msg["message_id"])


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
