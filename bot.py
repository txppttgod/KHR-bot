import os
import sys
import json
import csv
import io
import re
import time
import base64
import datetime
import threading
import requests
import queue
import hmac
import hashlib
import random
from urllib.parse import unquote, parse_qsl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Без этого Python на Render копит print() в буфере, и в Logs ничего не видно
sys.stdout.reconfigure(line_buffering=True)

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
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "")  # можно не задавать: бот сам выберет свежую flash-модель
GEMINI_API = "https://generativelanguage.googleapis.com/v1beta"
SHEET_HOOK = os.environ.get("SHEET_HOOK", "")  # адрес Apps Script для выгрузки дуэлей в Google-таблицу

CHUNK = 20              # сколько участников тегать в одном сообщении
WARN_LIMIT = 3          # сколько предупреждений до автобана
MUTE_DEFAULT_MIN = 60   # мут по умолчанию, в минутах
MODLOG_SIZE = 50        # сколько последних действий модерации хранить на чат
MAX_ROLES = 5           # сколько должностей можно дать одному человеку
DUEL_NORM_DEFAULT = 20  # норма дуэлей в неделю, меняется командой /setnorm
DUEL_KEEP_DAYS = 70     # сколько дней хранить дуэли
# Защита от «пустых» заездов: если соперник проехал лидером на 0, игра сама даёт преследователю 100 баллов.
# Поэтому дуэль идёт в норму пилоту, только если у него самого не меньше 100.1 балла.
MIN_SCORE = 100.1
CLUB_ONLY = True        # в норму идут только дуэли с соперником из таблицы пилотов клуба
PERM_KEYS = [
    "can_send_messages", "can_send_audios", "can_send_documents", "can_send_photos",
    "can_send_videos", "can_send_video_notes", "can_send_voice_notes", "can_send_polls",
    "can_send_other_messages", "can_add_web_page_previews", "can_change_info",
    "can_invite_users", "can_pin_messages", "can_manage_topics",
]
PERMS_NONE = {k: False for k in PERM_KEYS}
PERMS_ALL = {k: True for k in PERM_KEYS}

# Уровни в системе бота. Владелец группы всегда 3, админы Телеграма минимум 2.
LEVELS = {0: "Участник", 1: "Модератор", 2: "Администратор", 3: "Владелец"}
LEVEL_ICONS = {1: "🛡", 2: "⭐", 3: "👑"}

DEFAULT_WELCOME = ("Добро пожаловать в клуб, {name}! 🏁\n"
                   "Правила — /rules, все команды — /help. Таблицы клуба — по кнопке ниже.")

# ---------- База данных (Supabase) ----------
SB_HEADERS = {"apikey": SB_KEY, "Content-Type": "application/json"}
if SB_KEY.startswith("eyJ"):
    # Старые ключи (service_role) — это JWT, их передают ещё и в Authorization.
    # Новые ключи sb_secret_... в Authorization класть нельзя, Supabase их там отклоняет.
    SB_HEADERS["Authorization"] = f"Bearer {SB_KEY}"
DB_OK = False  # прочитана ли база перед текущим сообщением; если нет — не сохраняем, чтобы не стереть данные


def db_load():
    if not SB_URL or not SB_KEY:
        print("Не заданы SUPABASE_URL или SUPABASE_KEY в Render → Environment. Данные не сохраняются!")
        return False, {}
    try:
        r = requests.get(f"{SB_URL}/rest/v1/bot_state?id=eq.main&select=data", headers=SB_HEADERS, timeout=15)
        rows = r.json()
        if not isinstance(rows, list):
            print(f"Ошибка загрузки из базы: HTTP {r.status_code} {r.text[:300]}")
            return False, {}
        if not rows:
            print("В базе нет строки bot_state с id=main (или ключ не даёт её прочитать). Выполни SQL из инструкции.")
            return False, {}
        return True, rows[0]["data"] or {}
    except Exception as e:
        print("Ошибка загрузки из базы:", e)
        return False, {}


def db_save(data):
    try:
        r = requests.patch(f"{SB_URL}/rest/v1/bot_state?id=eq.main", headers=SB_HEADERS,
                           json={"data": data}, timeout=15)
        if r.status_code >= 300:
            print(f"Ошибка сохранения в базу: HTTP {r.status_code} {r.text[:300]}")
    except Exception as e:
        print("Ошибка сохранения в базу:", e)


def int_keys(d, depth=2):
    # В JSON ключи — строки, а в коде id чатов и людей — числа
    if depth == 0 or not isinstance(d, dict):
        return d
    return {int(k): int_keys(v, depth - 1) for k, v in d.items()}


def str_keys(d, depth=2):
    if depth == 0 or not isinstance(d, dict):
        return d
    return {str(k): str_keys(v, depth - 1) for k, v in d.items()}


def load_state():
    # Читаем базу перед каждым сообщением. Раньше она читалась один раз при запуске,
    # и если в этот момент Supabase не ответил, бот до перезапуска ничего не сохранял.
    # Так же не теряются данные, когда Render при деплое ненадолго держит две копии бота.
    global DB_OK, members, stats, warns, staff, roles, settings, modlog, duels, profiles, tracks
    ok, d = db_load()
    DB_OK = ok
    if not ok:
        return
    members = int_keys(d.get("members", {}))
    stats = int_keys(d.get("stats", {}))
    staff = int_keys(d.get("staff", {}))        # чат → {человек: уровень}
    roles = int_keys(d.get("roles", {}))        # чат → {человек: [должности]}
    settings = int_keys(d.get("settings", {}), 1)  # чат → {rules, welcome, welcome_off}
    modlog = int_keys(d.get("modlog", {}), 1)      # чат → [последние действия]
    duels = int_keys(d.get("duels", {}))        # чат → {человек: [дуэли]}
    profiles = int_keys(d.get("profiles", {}), 1)  # человек → {car, about, username, …}
    tracks = int_keys(d.get("tracks", {}), 1)      # чат → {трасса: {"n": сколько дуэлей за всё время, "name": как писать}}
    warns = {}
    for k, n in d.get("warns", {}).items():
        c, u = k.split("_")
        warns[(int(c), int(u))] = n


def save_state():
    if not DB_OK:
        return
    db_save(state_dict())


def state_dict():
    return ({
        "members": str_keys(members),
        "stats": str_keys(stats),
        "staff": str_keys(staff),
        "roles": str_keys(roles),
        "settings": str_keys(settings, 1),
        "modlog": str_keys(modlog, 1),
        "duels": str_keys(duels),
        "profiles": str_keys(profiles, 1),
        "tracks": str_keys(tracks, 1),
        "warns": {f"{c}_{u}": n for (c, u), n in warns.items()},
    })


members, stats, warns, staff, roles, settings, modlog, duels, profiles, tracks = {}, {}, {}, {}, {}, {}, {}, {}, {}, {}
load_state()
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
        payload["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
    return api("sendMessage", payload)


def reply(msg, text, entities=None):
    send(msg["chat"]["id"], text, reply_to=msg["message_id"], with_button=False, entities=entities)


def utf16_len(s):
    # Telegram считает смещения в UTF-16, эмодзи занимают 2 символа
    return len(s.encode("utf-16-le")) // 2


def with_mentions(prefix, users, sep=" "):
    # Текст с кликабельными именами: работает и для людей без @ника
    text, entities = prefix, []
    for u in users:
        name = name_of(u)
        entities.append({"type": "text_mention", "offset": utf16_len(text), "length": utf16_len(name),
                         "user": {"id": u["id"], "first_name": name}})
        text += name + sep
    return text.rstrip(), entities


def name_of(user):
    return (user or {}).get("first_name") or (user or {}).get("username") or "участник"


def remember(chat_id, user):
    if not user or user.get("is_bot"):
        return
    members.setdefault(chat_id, {})[user["id"]] = {
        "id": user["id"],
        "first_name": user.get("first_name", "участник"),
        "username": user.get("username", ""),
    }


def get_admins(chat_id):
    res = api("getChatAdministrators", {"chat_id": chat_id})
    return [m for m in res.get("result", []) if not m.get("user", {}).get("is_bot")]


def load_admins(chat_id):
    for m in get_admins(chat_id):
        remember(chat_id, m.get("user"))


def get_level(chat_id, uid):
    res = api("getChatMember", {"chat_id": chat_id, "user_id": uid}).get("result", {})
    status = res.get("status")
    if status == "creator":
        return 3
    own = staff.get(chat_id, {}).get(uid, 0)
    return max(own, 2) if status == "administrator" else own


def find_target(msg, args):
    # Цель команды: ответ на сообщение, @ник, упоминание без ника или id.
    # Возвращает (человек или None, оставшиеся аргументы)
    chat_id = msg["chat"]["id"]
    rep = (msg.get("reply_to_message") or {}).get("from")
    if rep:
        return rep, args
    for ent in msg.get("entities", []):
        if ent.get("type") == "text_mention":
            # Имя-упоминание занимает в тексте одно или несколько слов: убираем их из аргументов
            words = len(name_of(ent["user"]).split())
            return ent["user"], args[words:]
    if args:
        a = args[0]
        if a.startswith("@"):
            uname = a[1:].lower()
            for u in members.get(chat_id, {}).values():
                if u.get("username", "").lower() == uname:
                    return dict(u), args[1:]
        elif a.isdigit() and len(a) >= 5:
            uid = int(a)
            return dict(members.get(chat_id, {}).get(uid, {"id": uid, "first_name": a})), args[1:]
    return None, args


DUR_UNITS = {"": 60, "m": 60, "м": 60, "min": 60, "мин": 60,
             "h": 3600, "ч": 3600, "d": 86400, "д": 86400}


def parse_duration(args):
    # «30», «30m», «2ч», «1д» → секунды. Возвращает (секунды или None, оставшиеся аргументы)
    if args:
        m = re.fullmatch(r"(\d+)([a-zа-я]*)", args[0].lower())
        if m and m.group(2) in DUR_UNITS:
            return int(m.group(1)) * DUR_UNITS[m.group(2)], args[1:]
    return None, args


def fmt_duration(sec):
    if sec % 86400 == 0:
        return f"{sec // 86400} дн"
    if sec % 3600 == 0:
        return f"{sec // 3600} ч"
    return f"{sec // 60} мин"


def text_after_command(msg):
    # Всё, что после команды, с сохранением переносов строк; или текст сообщения, на которое ответили
    parts = msg.get("text", "").split(None, 1)
    if len(parts) > 1:
        return parts[1].strip()
    return ((msg.get("reply_to_message") or {}).get("text") or "").strip()


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
        text, entities = with_mentions("🚨 Общий сбор! " if start == 0 else "", users[start:start + CHUNK])
        send(chat_id, text, with_button=False, entities=entities)

    info = f"Отмечено {len(users)} из {total - 1 if total else '?'} участников."
    if total and len(users) < total - 1:
        info += " Кого нет в списке — пусть напишет любое сообщение в чат, и бот его запомнит."
    send(chat_id, info, with_button=False)


def welcome(chat_id, user):
    conf = settings.get(chat_id, {})
    if conf.get("welcome_off") or user.get("is_bot"):
        return
    text = (conf.get("welcome") or DEFAULT_WELCOME).replace("{name}", name_of(user))
    send(chat_id, text, group=True)


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


def day_key(days_ago=0):
    # Дни считаем по Москве (UTC+3)
    return time.strftime("%Y-%m-%d", time.gmtime(time.time() + 3 * 3600 - days_ago * 86400))


def count_activity(msg):
    user = msg.get("from") or {}
    if user.get("is_bot"):
        return
    entry = get_entry(msg["chat"]["id"], user["id"], user.get("first_name", "участник"))
    entry["name"] = user.get("first_name", "участник")
    entry["count"] += 1
    daily = entry.setdefault("daily", {})
    today = day_key()
    daily[today] = daily.get(today, 0) + 1
    cutoff = day_key(40)  # старые дни не храним, чтобы база не росла
    for d in [d for d in daily if d < cutoff]:
        del daily[d]


def msgs_in_days(entry, days):
    if not entry:
        return 0
    cutoff = day_key(days - 1)
    return sum(n for d, n in entry.get("daily", {}).items() if d >= cutoff)


def roles_by_username():
    result = {}
    for r in get_pilots():
        tg = col(r, 2).lower().lstrip("@")
        if tg != "—":
            result[tg] = col(r, 6)
    return result


def club_roles(chat_id, user, table_roles=None):
    # Должности из бота; если их не задали — роль из таблицы «Статистика пилотов»
    own = roles.get(chat_id, {}).get(user["id"], [])
    if own:
        return ", ".join(own)
    uname = (user.get("username") or "").lower()
    if table_roles is None:
        table_roles = roles_by_username()
    return table_roles.get(uname, "—") if uname else "—"


def reply_roster(msg):
    chat_id = msg["chat"]["id"]
    board = stats.get(chat_id, {})
    table_roles = roles_by_username()
    rows = []
    for p in members.get(chat_id, {}).values():
        e = board.get(p["id"])
        rows.append({
            "name": p["first_name"],
            "username": p.get("username", ""),
            "role": club_roles(chat_id, p, table_roles),
            "today": msgs_in_days(e, 1),
            "week": msgs_in_days(e, 7),
            "all": e["count"] if e else 0,
        })
    rows.sort(key=lambda r: r["all"], reverse=True)

    if not rows:
        send(chat_id, "Пока никого не знаю. Участники появятся, когда напишут в чат.", with_button=False)
        return

    head = (f"👥 Участники, которых знает бот: {len(rows)}\n"
            f"Сообщений: сегодня {sum(r['today'] for r in rows)}, "
            f"за неделю {sum(r['week'] for r in rows)}, всего {sum(r['all'] for r in rows)}\n"
            "Формат: имя @ник — должность | сегодня / неделя / всего\n")
    lines = []
    for i, r in enumerate(rows, 1):
        uname = f" @{r['username']}" if r["username"] else ""
        lines.append(f"{i}. {r['name']}{uname} — {r['role']} | {r['today']} / {r['week']} / {r['all']}")
    send_long(chat_id, head, lines)


def send_long(chat_id, head, lines):
    # Телеграм ограничивает длину сообщения, поэтому режем список на части
    chunks, cur = [], head
    for line in lines:
        if len(cur) + len(line) + 1 > 3500:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    chunks.append(cur)
    for c in chunks:
        send(chat_id, c.rstrip(), with_button=False)


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
    reply(msg, text)


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
    reply(msg, text)


# ---------- Модерация ----------
def tg(msg, method, payload):
    # Вызов Телеграма с понятным ответом, если у бота не хватает прав
    res = api(method, payload)
    if not res.get("ok"):
        reply(msg, "Телеграм не дал это сделать: " + res.get("description", "нет ответа") +
              "\nПроверь, что бот — админ группы с нужными правами.")
        return False
    return True


def log_action(msg, target, action, reason=""):
    lst = modlog.setdefault(msg["chat"]["id"], [])
    lst.append({"t": int(time.time()), "by": name_of(msg.get("from")), "who": name_of(target) if target else "",
                "uid": target["id"] if target else 0, "act": action, "why": reason})
    del lst[:-MODLOG_SIZE]


def pick_target(msg, args, my, check_level=True):
    target, rest = find_target(msg, args)
    if not target:
        reply(msg, "Ответь командой на сообщение участника или укажи @ник.")
        return None, rest
    if target.get("is_bot"):
        reply(msg, "Это бот, к нему команда не применяется.")
        return None, rest
    if check_level and get_level(msg["chat"]["id"], target["id"]) >= my:
        reply(msg, "Нельзя: у этого участника такой же или более высокий уровень. Кто есть кто — /staff")
        return None, rest
    return target, rest


def cmd_ban(msg, args, my):
    chat_id = msg["chat"]["id"]
    target, rest = pick_target(msg, args, my)
    if not target:
        return
    dur, rest = parse_duration(rest)
    reason = " ".join(rest) or "не указана"
    payload = {"chat_id": chat_id, "user_id": target["id"]}
    if dur:
        payload["until_date"] = int(time.time()) + dur
    if not tg(msg, "banChatMember", payload):
        return
    record_action(chat_id, target["id"], name_of(target), "ban")
    members.get(chat_id, {}).pop(target["id"], None)
    term = f" на {fmt_duration(dur)}" if dur else ""
    log_action(msg, target, "бан" + term, reason)
    reply(msg, f"🔨 {name_of(target)} забанен{term}. Причина: {reason}")


def cmd_unban(msg, args, my):
    target, _ = pick_target(msg, args, my)
    if target and tg(msg, "unbanChatMember", {"chat_id": msg["chat"]["id"], "user_id": target["id"],
                                             "only_if_banned": True}):
        log_action(msg, target, "разбан")
        reply(msg, f"✅ {name_of(target)} разбанен и может вернуться по ссылке.")


def cmd_kick(msg, args, my):
    chat_id = msg["chat"]["id"]
    target, rest = pick_target(msg, args, my)
    if not target:
        return
    reason = " ".join(rest) or "не указана"
    # Кик = бан и сразу разбан: человек вылетает, но может вернуться
    if not tg(msg, "banChatMember", {"chat_id": chat_id, "user_id": target["id"]}):
        return
    api("unbanChatMember", {"chat_id": chat_id, "user_id": target["id"], "only_if_banned": True})
    members.get(chat_id, {}).pop(target["id"], None)
    log_action(msg, target, "кик", reason)
    reply(msg, f"👢 {name_of(target)} исключён из чата. Причина: {reason}")


def cmd_mute(msg, args, my):
    chat_id = msg["chat"]["id"]
    target, rest = pick_target(msg, args, my)
    if not target:
        return
    dur, rest = parse_duration(rest)
    dur = dur or MUTE_DEFAULT_MIN * 60
    reason = " ".join(rest) or "не указана"
    if not tg(msg, "restrictChatMember", {"chat_id": chat_id, "user_id": target["id"], "permissions": PERMS_NONE,
                                          "until_date": int(time.time()) + dur}):
        return
    record_action(chat_id, target["id"], name_of(target), "mute")
    log_action(msg, target, f"мут на {fmt_duration(dur)}", reason)
    reply(msg, f"🔇 {name_of(target)} в муте на {fmt_duration(dur)}. Причина: {reason}")


def cmd_unmute(msg, args, my):
    target, _ = pick_target(msg, args, my)
    if target and tg(msg, "restrictChatMember", {"chat_id": msg["chat"]["id"], "user_id": target["id"],
                                                 "permissions": PERMS_ALL}):
        log_action(msg, target, "размут")
        reply(msg, f"🔊 {name_of(target)} снова может писать.")


def cmd_warn(msg, args, my):
    chat_id = msg["chat"]["id"]
    target, rest = pick_target(msg, args, my)
    if not target:
        return
    reason = " ".join(rest) or "не указана"
    key = (chat_id, target["id"])
    warns[key] = warns.get(key, 0) + 1
    record_action(chat_id, target["id"], name_of(target), "warn")
    log_action(msg, target, "варн", reason)
    if warns[key] >= WARN_LIMIT:
        if tg(msg, "banChatMember", {"chat_id": chat_id, "user_id": target["id"]}):
            warns[key] = 0
            record_action(chat_id, target["id"], name_of(target), "ban")
            members.get(chat_id, {}).pop(target["id"], None)
            log_action(msg, target, "автобан", f"{WARN_LIMIT} варна")
            reply(msg, f"⚠️ {name_of(target)}: {WARN_LIMIT} предупреждения, автобан. Причина: {reason}")
        return
    reply(msg, f"⚠️ {name_of(target)}, предупреждение {warns[key]} из {WARN_LIMIT}. Причина: {reason}")


def cmd_unwarn(msg, args, my):
    target, _ = pick_target(msg, args, my)
    if target:
        warns[(msg["chat"]["id"], target["id"])] = 0
        log_action(msg, target, "снял варны")
        reply(msg, f"Предупреждения {name_of(target)} сняты.")


def cmd_warns(msg, args, my):
    chat_id = msg["chat"]["id"]
    target, _ = find_target(msg, args)
    target = target or msg["from"]
    n = warns.get((chat_id, target["id"]), 0)
    last = [e for e in modlog.get(chat_id, []) if e["uid"] == target["id"] and e["act"] == "варн"][-n:] if n else []
    text = f"⚠️ {name_of(target)}: предупреждений {n} из {WARN_LIMIT}."
    if last:
        text += "\n" + "\n".join(f"• {time.strftime('%d.%m', time.gmtime(e['t'] + 3 * 3600))} — {e['why']} ({e['by']})"
                                 for e in last)
    reply(msg, text)


def cmd_del(msg, args, my):
    target_msg = msg.get("reply_to_message")
    if not target_msg:
        reply(msg, "Ответь этой командой на сообщение, которое нужно удалить.")
        return
    if tg(msg, "deleteMessage", {"chat_id": msg["chat"]["id"], "message_id": target_msg["message_id"]}):
        api("deleteMessage", {"chat_id": msg["chat"]["id"], "message_id": msg["message_id"]})


def cmd_pin(msg, args, my):
    target_msg = msg.get("reply_to_message")
    if not target_msg:
        reply(msg, "Ответь этой командой на сообщение, которое нужно закрепить.")
        return
    if tg(msg, "pinChatMessage", {"chat_id": msg["chat"]["id"], "message_id": target_msg["message_id"]}):
        reply(msg, "📌 Закреплено.")


def cmd_unpin(msg, args, my):
    payload = {"chat_id": msg["chat"]["id"]}
    if msg.get("reply_to_message"):
        payload["message_id"] = msg["reply_to_message"]["message_id"]
    if tg(msg, "unpinChatMessage", payload):
        reply(msg, "Откреплено.")


# ---------- Уровни (кто модератор, кто админ) ----------
def cmd_promote(msg, args, my):
    chat_id = msg["chat"]["id"]
    target, rest = pick_target(msg, args, my)
    if not target:
        return
    current = staff.get(chat_id, {}).get(target["id"], 0)
    new = int(rest[0]) if rest and rest[0].isdigit() else current + 1
    if not 1 <= new < my:
        reply(msg, f"Можно выдать уровень от 1 до {my - 1}: " +
              ", ".join(f"{k} — {LEVELS[k]}" for k in range(1, my)))
        return
    staff.setdefault(chat_id, {})[target["id"]] = new
    remember(chat_id, target)
    log_action(msg, target, f"уровень → {LEVELS[new]}")
    reply(msg, f"{LEVEL_ICONS[new]} {name_of(target)} теперь {LEVELS[new]}.")


def cmd_demote(msg, args, my):
    chat_id = msg["chat"]["id"]
    target, rest = pick_target(msg, args, my)
    if not target:
        return
    current = staff.get(chat_id, {}).get(target["id"], 0)
    new = int(rest[0]) if rest and rest[0].isdigit() else current - 1
    new = max(0, min(new, current))
    if new:
        staff.setdefault(chat_id, {})[target["id"]] = new
    else:
        staff.get(chat_id, {}).pop(target["id"], None)
    log_action(msg, target, f"уровень → {LEVELS[new]}")
    text = f"{name_of(target)} теперь {LEVELS[new]}."
    if get_level(chat_id, target["id"]) >= 2:
        text += " Он остаётся админом Телеграма, поэтому в боте у него всё равно уровень Администратор."
    reply(msg, text)


def reply_staff(msg):
    chat_id = msg["chat"]["id"]
    people = {}  # id → (уровень, человек)
    for m in get_admins(chat_id):
        u = m["user"]
        people[u["id"]] = (3 if m["status"] == "creator" else 2, u)
    for uid, lvl in staff.get(chat_id, {}).items():
        if uid not in people:
            people[uid] = (lvl, members.get(chat_id, {}).get(uid, {"id": uid, "first_name": str(uid)}))
    if not people:
        reply(msg, "Пока нет ни модераторов, ни админов.")
        return
    lines = []
    for lvl in (3, 2, 1):
        group = [u for (l, u) in people.values() if l == lvl]
        if group:
            lines.append(f"\n{LEVEL_ICONS[lvl]} {LEVELS[lvl]}:")
            lines += [f"  • {name_of(u)}" + (f" @{u['username']}" if u.get("username") else "") for u in group]
    reply(msg, "Команда управления чатом:" + "\n".join(lines))


# ---------- Должности в клубе ----------
def cmd_setrole(msg, args, my):
    chat_id = msg["chat"]["id"]
    target, rest = pick_target(msg, args, my, check_level=False)
    if not target:
        return
    role = " ".join(rest).strip()
    if not role:
        reply(msg, "Напиши должность: /setrole @ник Настройщик")
        return
    lst = roles.setdefault(chat_id, {}).setdefault(target["id"], [])
    if role.lower() in (r.lower() for r in lst):
        reply(msg, f"У {name_of(target)} уже есть должность «{role}».")
        return
    if len(lst) >= MAX_ROLES:
        reply(msg, f"У одного человека может быть до {MAX_ROLES} должностей.")
        return
    lst.append(role)
    remember(chat_id, target)
    log_action(msg, target, f"должность + {role}")
    reply(msg, f"🎖 {name_of(target)}: {', '.join(lst)}")


def cmd_unrole(msg, args, my):
    chat_id = msg["chat"]["id"]
    target, rest = pick_target(msg, args, my, check_level=False)
    if not target:
        return
    role = " ".join(rest).strip().lower()
    lst = roles.get(chat_id, {}).get(target["id"], [])
    left = [r for r in lst if role and r.lower() != role]
    if left:
        roles[chat_id][target["id"]] = left
    else:
        roles.get(chat_id, {}).pop(target["id"], None)
    log_action(msg, target, f"должность − {role or 'все'}")
    reply(msg, f"🎖 {name_of(target)}: {', '.join(left) or 'без должности'}")


def reply_team(msg):
    chat_id = msg["chat"]["id"]
    table_roles = roles_by_username()
    by_role = {}
    for p in members.get(chat_id, {}).values():
        own = roles.get(chat_id, {}).get(p["id"])
        for r in own or [club_roles(chat_id, p, table_roles)]:
            if r != "—":
                by_role.setdefault(r, []).append(p)
    if not by_role:
        reply(msg, "Должностей пока нет. Админ может выдать: /setrole @ник Настройщик")
        return
    lines = []
    for r in sorted(by_role, key=lambda r: (-len(by_role[r]), r)):
        lines.append(f"\n🎖 {r} ({len(by_role[r])}):")
        lines += [f"  • {name_of(p)}" + (f" @{p['username']}" if p.get("username") else "") for p in by_role[r]]
    send_long(chat_id, "👥 Состав клуба по должностям", lines)


# ---------- Правила, приветствие, жалобы, журнал ----------
def reply_rules(msg):
    text = settings.get(msg["chat"]["id"], {}).get("rules")
    if text:
        send(msg["chat"]["id"], "📜 Правила чата\n\n" + text, reply_to=msg["message_id"], group=True)
    else:
        send(msg["chat"]["id"], "📜 Правила клуба — во вкладке «Правила» в приложении.",
             reply_to=msg["message_id"], group=True)


def cmd_setrules(msg, args, my):
    text = text_after_command(msg)
    conf = settings.setdefault(msg["chat"]["id"], {})
    if not text or text == "-":
        conf.pop("rules", None)
        reply(msg, "Свои правила убраны, /rules снова ведёт в приложение.")
        return
    conf["rules"] = text
    log_action(msg, None, "обновил правила")
    reply(msg, "📜 Правила сохранены. Посмотреть: /rules")


def cmd_setwelcome(msg, args, my):
    text = text_after_command(msg)
    conf = settings.setdefault(msg["chat"]["id"], {})
    if not text or text == "-":
        conf.pop("welcome", None)
        reply(msg, "Вернул стандартное приветствие.")
        return
    conf["welcome"] = text
    reply(msg, "👋 Приветствие сохранено. Так его увидит новичок:\n\n" + text.replace("{name}", name_of(msg["from"])))


def cmd_welcome(msg, args, my):
    conf = settings.setdefault(msg["chat"]["id"], {})
    if args and args[0].lower() in ("off", "выкл"):
        conf["welcome_off"] = True
        reply(msg, "Приветствие новичков выключено. Включить: /welcome on")
    elif args and args[0].lower() in ("on", "вкл"):
        conf.pop("welcome_off", None)
        reply(msg, "Приветствие новичков включено.")
    else:
        state = "выключено" if conf.get("welcome_off") else "включено"
        reply(msg, f"Приветствие сейчас {state}. /welcome on или /welcome off, текст — /setwelcome")


def reply_report(msg):
    chat_id = msg["chat"]["id"]
    bad = msg.get("reply_to_message")
    if not bad:
        reply(msg, "Ответь /report на сообщение, на которое жалуешься.")
        return
    people = {m["user"]["id"]: m["user"] for m in get_admins(chat_id)}
    for uid in staff.get(chat_id, {}):
        people.setdefault(uid, members.get(chat_id, {}).get(uid, {"id": uid, "first_name": "модератор"}))
    reason = text_after_command(msg) if msg.get("text", "").split(None, 1)[1:] else ""
    head = f"🚩 Жалоба от {name_of(msg['from'])} на {name_of(bad.get('from'))}"
    head += f": {reason}\n" if reason else "\n"
    text, entities = with_mentions(head + "Модерация: ", list(people.values()), sep=", ")
    send(chat_id, text.rstrip(","), reply_to=bad["message_id"], with_button=False, entities=entities)


def cmd_modlog(msg, args, my):
    lst = modlog.get(msg["chat"]["id"], [])[-10:]
    if not lst:
        reply(msg, "Журнал пуст.")
        return
    lines = [f"{time.strftime('%d.%m %H:%M', time.gmtime(e['t'] + 3 * 3600))} · {e['by']} → {e['act']}"
             + (f" · {e['who']}" if e["who"] else "") + (f" ({e['why']})" if e.get("why") else "")
             for e in reversed(lst)]
    reply(msg, "📒 Последние действия модерации:\n" + "\n".join(lines))


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
    chat_id = msg["chat"]["id"]
    args = msg.get("text", "").split()[1:]
    target, _ = find_target(msg, args)
    if target:
        username = target.get("username", "")
    elif args and args[0].startswith("@"):
        username = args[0][1:]
    else:
        target = msg.get("from") or {}
        username = target.get("username", "")

    if not username:
        text = "У этого участника не задан username в Телеграме, поэтому его нельзя найти в таблице."
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
                f"Роль в таблице: {col(r, 6)}"
            )
        else:
            text = f"@{username} пока нет в таблице. Попроси админа добавить тебя в «Статистику пилотов»."

    if target and msg["chat"]["type"] != "private":
        lvl = get_level(chat_id, target["id"])
        own = roles.get(chat_id, {}).get(target["id"], [])
        text += f"\n\nВ чате: {LEVEL_ICONS.get(lvl, '')} {LEVELS[lvl]}".replace("  ", " ")
        if own:
            text += f"\nДолжности: {', '.join(own)}"
        text += f"\nПредупреждений: {warns.get((chat_id, target['id']), 0)} из {WARN_LIMIT}"
    reply(msg, text)


# ---------- Учёт дуэлей по скриншотам ----------
# Участники кидают в тему отчётов скриншоты «ИСТОРИЯ ЗАЕЗДОВ vs соперник». ИИ (Gemini) читает
# каждую строку: дата, результат, счёт. Бот засчитывает каждую дуэль один раз (повторы со старых
# скринов отбрасываются) и считает, сколько дуэлей за текущую неделю (пн–вс по Москве).
DUEL_PROMPT = (
    "Это скриншот из игры CarX Drift Racing 3. Нужный экран называется «ИСТОРИЯ ЗАЕЗДОВ» "
    "(история парных дуэлей против одного соперника); вверху написано «vs <ник соперника>». "
    "Каждая строка таблицы — одна дуэль: конфигурация трассы, дата в формате Д/М/ГГ "
    "(сначала день, потом месяц, потом год), результат «Победа», «Поражение» или «Ничья» "
    "и в скобках два числа очков, например «Поражение (176.9 / 178.8)».\n"
    "Верни JSON. is_duel_history — true только если это именно такой экран. opponent — ник соперника "
    "из «vs ...» ровно как написано. rows — все строки, которые видны целиком: date в формате ГГГГ-ММ-ДД, "
    "result: win, loss или draw, score — два числа через « / » как на экране, track — конфигурация. "
    "Не придумывай строки, которых не видно, и не пропускай видимые. "
    "unreadable — true, если на экране есть строки, которые не удалось прочитать уверенно: "
    "обрезаны, размыты, слишком мелкие или перекрыты."
)
DUEL_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "is_duel_history": {"type": "BOOLEAN"},
        "opponent": {"type": "STRING"},
        "rows": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "date": {"type": "STRING"},
            "result": {"type": "STRING", "enum": ["win", "loss", "draw"]},
            "score": {"type": "STRING"},
            "track": {"type": "STRING"},
        }, "required": ["date", "result", "score"]}},
        "unreadable": {"type": "BOOLEAN"},
    },
    "required": ["is_duel_history", "rows"],
}
RESULT_RU = {"win": "Победа", "loss": "Поражение", "draw": "Ничья"}
_albums = {}       # (чат, media_group_id) → сводка по альбому, чтобы отвечать одним сообщением
_seen_msgs = set() # уже обработанные скрины (если Телеграм пришлёт обновление повторно)
_sheet = {"timer": None}
_models = {"time": 0, "list": []}
_cooldown = {}               # модель → до какого времени её не трогать (лимит, отключена, молчит)
LOCK = threading.RLock()     # база и память бота: меняет только один поток за раз
DUEL_QUEUE = queue.Queue()   # скрины ждут, пока их прочитает ИИ
DUEL_ASYNC = False           # при запуске на сервере включается; в тестах скрины читаются сразу


def msk_today():
    return (datetime.datetime.utcnow() + datetime.timedelta(hours=3)).date()


def week_bounds(day=None, back=0):
    day = day or msk_today()
    start = day - datetime.timedelta(days=day.weekday() + 7 * back)
    return start, start + datetime.timedelta(days=6)


def week_label(start, end):
    return f"{start:%d.%m}–{end:%d.%m}"


def get_norm(chat_id):
    return settings.get(chat_id, {}).get("norm", DUEL_NORM_DEFAULT)


def own_score(s):
    # Счёт хранится «свои / соперника»: первое число — баллы самого пилота
    nums = re.findall(r"\d+(?:\.\d+)?", s or "")
    return float(nums[0]) if nums else 0.0


def club_names():
    return {col(r, 1).strip().lower() for r in get_pilots() if col(r, 1) != "—"}


def why_not_counted(d, names=None):
    # Почему дуэль не идёт в норму этому пилоту ("" — идёт). Проверяется при каждом подсчёте,
    # поэтому правила действуют и для уже сданных дуэлей.
    if own_score(d["s"]) < MIN_SCORE:
        return f"меньше {MIN_SCORE:g} балла"
    names = club_names() if names is None else names
    if CLUB_ONLY and names and d["o"].strip().lower() not in names:
        return "соперник не из клуба"
    return ""


def duels_in(chat_id, uid, start, end):
    a, b = start.isoformat(), end.isoformat()
    names = club_names()
    return sum(1 for d in duels.get(chat_id, {}).get(uid, []) if a <= d["d"] <= b and not why_not_counted(d, names))


def duels_word(n):
    n = abs(n) % 100
    return "дуэлей" if 11 <= n <= 14 else {1: "дуэль", 2: "дуэли", 3: "дуэли", 4: "дуэли"}.get(n % 10, "дуэлей")


def pace(left, days):
    # Подсказка по темпу: сколько дуэлей в день нужно, чтобы закрыть норму до воскресенья
    if left <= 0 or days <= 0:
        return ""
    if days == 1:
        return f"все {left} — сегодня"
    per = -(-left // days)
    return f"это по {per} {duels_word(per)} в день"


def norm_score(s):
    nums = re.findall(r"\d+(?:[.,]\d+)?", s or "")
    return " / ".join(n.replace(",", ".") for n in nums[:2])


def tg_file(file_id):
    path = api("getFile", {"file_id": file_id}).get("result", {}).get("file_path")
    if not path:
        return None
    try:
        r = requests.get(f"https://api.telegram.org/file/bot{TOKEN}/{path}", timeout=30)
        return r.content if r.status_code == 200 else None
    except Exception as e:
        print("Ошибка скачивания скрина:", e)
        return None


def thinking_config(model):
    # Новые модели по умолчанию долго «думают» (до минуты на скрин). Чтобы прочитать таблицу, это не нужно.
    v = re.search(r"gemini-(\d+(?:\.\d+)?)", model)
    if v and float(v.group(1)) < 3:
        return {"thinkingBudget": 0}
    return {"thinkingLevel": "low"}


def gemini_read(image, mime):
    contents = [{"parts": [
        {"text": DUEL_PROMPT},
        {"inline_data": {"mime_type": mime, "data": base64.b64encode(image).decode()}},
    ]}]
    last = 0
    # Модели, которые недавно упёрлись в лимит или молчали, на время пропускаем — сразу берём рабочую
    now = time.time()
    models = [m for m in gemini_models() if _cooldown.get(m, 0) < now] or gemini_models()
    for model in models[:3]:
        think, retried = thinking_config(model), False
        for _ in range(3):
            cfg = {"temperature": 0, "responseMimeType": "application/json", "responseSchema": DUEL_SCHEMA}
            if think:
                cfg["thinkingConfig"] = think
            started = time.time()
            try:
                r = requests.post(f"{GEMINI_API}/models/{model}:generateContent",
                                  headers={"x-goog-api-key": GEMINI_KEY},
                                  json={"contents": contents, "generationConfig": cfg}, timeout=75)
            except Exception as e:
                print(f"Gemini {model}: нет ответа ({e.__class__.__name__}), пробую следующую")
                r = None
                break
            if r.status_code == 400 and think:
                print(f"Gemini {model}: не принял настройку размышлений, повторяю без неё")
                think = None
                continue
            if r.status_code in (429, 500, 503) and not retried:
                retried = True
                time.sleep(8 if r.status_code == 429 else 3)  # лимит бесплатного ключа или перегрузка
                continue
            if r.status_code < 300:
                print(f"Gemini {model}: ответ за {time.time() - started:.0f} с")
            break
        if r is None:
            last = 503
            _cooldown[model] = time.time() + 3600
            continue
        last = r.status_code
        if r.status_code in (404, 429):
            _cooldown[model] = time.time() + (86400 if r.status_code == 404 else 3600)
        if r.status_code >= 300:
            # Модель отключена, перегружена или упёрлась в лимит — пробуем следующую
            print(f"Gemini {model}: HTTP {r.status_code} {r.text[:200]}")
            continue
        try:
            parts = r.json()["candidates"][0]["content"]["parts"]
            return json.loads("".join(p.get("text", "") for p in parts if not p.get("thought"))), ""
        except Exception as e:
            print("Непонятный ответ Gemini:", e, r.text[:300])
            return None, "ИИ вернул непонятный ответ"
    if last in (429, 500, 503):
        return None, "ИИ сейчас перегружен, модератор может перечитать скрин позже: ответь на него /recheck"
    return None, f"ошибка ИИ (HTTP {last}), подробности в логах Render"


def gemini_models():
    # Google время от времени отключает старые модели, поэтому раз в сутки спрашиваем, какие есть.
    # Порядок: GEMINI_MODEL из Render, затем свежие flash-модели (сначала обычные, потом lite и preview).
    if time.time() - _models["time"] > 86400 or not _models["list"]:
        found = []
        try:
            r = requests.get(f"{GEMINI_API}/models?pageSize=1000", headers={"x-goog-api-key": GEMINI_KEY}, timeout=20)
            for m in r.json().get("models", []):
                name = m.get("name", "").replace("models/", "")
                if ("generateContent" in m.get("supportedGenerationMethods", []) and "flash" in name
                        and not re.search(r"image|tts|audio|live|embed|native|robotics|computer|thinking", name)):
                    found.append(name)
        except Exception as e:
            print("Не удалось получить список моделей Gemini:", e)

        def rank(name):
            v = re.search(r"gemini-(\d+(?:\.\d+)?)", name)
            return ("lite" in name, bool(re.search(r"preview|exp", name)), -float(v.group(1)) if v else 0, name)
        found.sort(key=rank)
        if found:
            _models["list"], _models["time"] = found, time.time()
            print("Модели Gemini:", ", ".join(found[:6]))
    # gemini-flash-latest — только запасной вариант: на нём чаще всего «перегружено»
    order = [m for m in (GEMINI_MODEL,) if m] + (_models["list"] or ["gemini-2.5-flash"]) + ["gemini-flash-latest"]
    return list(dict.fromkeys(order))


def duel_image(msg):
    # Скрин фото или файлом (без сжатия). Возвращает (file_id, mime) или (None, None)
    if msg.get("photo"):
        return msg["photo"][-1]["file_id"], "image/jpeg"
    doc = msg.get("document") or {}
    if doc.get("mime_type", "").startswith("image/") and doc.get("file_size", 0) < 15_000_000:
        return doc["file_id"], doc["mime_type"]
    return None, None


def small_photo(msg):
    # Телеграм сжал скрин так сильно, что мелкие цифры могут не прочитаться (файлом без сжатия такого не бывает)
    big = (msg.get("photo") or [{}])[-1]
    return bool(msg.get("photo")) and 0 < min(big.get("width", 0), big.get("height", 0)) < 500


def is_duel_report(msg):
    conf = settings.get(msg["chat"]["id"], {})
    if "duel_thread" not in conf or (msg.get("from") or {}).get("is_bot"):
        return False
    return msg.get("message_thread_id", 0) == conf["duel_thread"] and duel_image(msg)[0] is not None


def game_name(chat_id, uid):
    # Ник в игре — колонка «Имя» в таблице пилотов, ищем по @нику в Телеграме
    uname = members.get(chat_id, {}).get(uid, {}).get("username") or ""
    r = find_pilot(uname) if uname else None
    return col(r, 1) if r else ""


def uid_by_game_name(chat_id, name):
    for r in get_pilots():
        if col(r, 1).lower() == name.lower():
            tgname = col(r, 2).lower().lstrip("@")
            for u in members.get(chat_id, {}).values():
                if (u.get("username") or "").lower() == tgname:
                    return u["id"]
    return None


OPPOSITE = {"win": "loss", "loss": "win", "draw": "draw"}


def mirror(score):
    parts = score.split(" / ")
    return f"{parts[1]} / {parts[0]}" if len(parts) == 2 else score


# ---------- Популярные карты: какие трассы клуб катает чаще всего ----------
def track_key(name):
    return re.sub(r"\s+", " ", (name or "").strip()).lower()


def ensure_tracks(chat_id):
    # Счётчик «за всё время» живёт отдельно от дуэлей (их бот хранит 70 дней).
    # Первый раз заполняем его из уже сданных дуэлей: каждую считаем один раз, без копий у соперника.
    if chat_id in tracks:
        return tracks[chat_id]
    t = tracks[chat_id] = {}
    for lst in duels.get(chat_id, {}).values():
        for d in lst:
            k = track_key(d.get("t"))
            if k and not d.get("by"):
                e = t.setdefault(k, {"n": 0, "name": d["t"].strip()})
                e["n"] += 1
    return t


def track_count(chat_id, name, delta):
    k = track_key(name)
    if not k:
        return
    e = ensure_tracks(chat_id).setdefault(k, {"n": 0, "name": name.strip()})
    e["n"] = max(0, e["n"] + delta)


def tracks_top(chat_id, start=None, end=None, limit=10):
    # За неделю — по сданным дуэлям этой недели, за всё время — по счётчику
    if start is None:
        rows = [(e["name"], e["n"]) for e in ensure_tracks(chat_id).values() if e["n"] > 0]
    else:
        a, b, cnt, nm = start.isoformat(), end.isoformat(), {}, {}
        for lst in duels.get(chat_id, {}).values():
            for d in lst:
                k = track_key(d.get("t"))
                if k and not d.get("by") and a <= d["d"] <= b:
                    cnt[k] = cnt.get(k, 0) + 1
                    nm.setdefault(k, d["t"].strip())
        rows = [(nm[k], n) for k, n in cnt.items()]
    rows.sort(key=lambda x: (-x[1], x[0].lower()))
    # [название, сколько дуэлей, версия логотипа (0 — нет), ключ для ссылки на логотип]
    known = tracks.get(chat_id, {})
    return [[n, c, known.get(track_key(n), {}).get("lv", 0) if known.get(track_key(n), {}).get("logo") else 0,
             track_key(n)] for n, c in rows[:limit]]


# ---------- Логотипы карт: ставит только владелец клуба через ЛС бота (/maplogo) ----------
_logo_cache = {}  # ключ трассы → (file_id, байты)


def is_owner(uid):
    chat_id = club_chat()
    return bool(chat_id) and get_level(chat_id, uid) >= 3


def maplogo_menu(msg):
    uid = msg["from"]["id"]
    if msg["chat"]["type"] != "private":
        reply(msg, "Логотипы карт ставятся в личке бота: напиши ему /maplogo.")
        return
    if not is_owner(uid):
        reply(msg, "Логотипы карт может ставить только владелец клуба.")
        return
    chat_id = club_chat()
    t = ensure_tracks(chat_id)
    keys = sorted(t, key=lambda k: (-t[k]["n"], k))[:30]
    if not keys:
        reply(msg, "Пока нет ни одной карты — они появятся, когда бот прочитает трассы со скринов дуэлей.")
        return
    profiles.setdefault(uid, {})["maps"] = keys
    kb = [[{"text": ("🖼 " if t[k].get("logo") else "") + f"{t[k]['name']} · {t[k]['n']}", "callback_data": f"ml:{i}"}]
          for i, k in enumerate(keys)]
    api("sendMessage", {"chat_id": msg["chat"]["id"], "reply_markup": {"inline_keyboard": kb},
                        "text": "🗺 Выбери карту, для которой поставить логотип.\n🖼 — логотип уже есть, его можно заменить."})


def maplogo_callback(cq):
    uid = cq["from"]["id"]
    chat = (cq.get("message") or {}).get("chat", {}).get("id", uid)
    answer = lambda text="", alert=False: api("answerCallbackQuery", {"callback_query_id": cq["id"], "text": text,
                                                                      "show_alert": alert})
    if not is_owner(uid):
        answer("Логотипы карт может ставить только владелец клуба.", True)
        return
    kind, i = cq["data"].split(":")
    keys = profiles.get(uid, {}).get("maps", [])
    t = ensure_tracks(club_chat())
    if not i.isdigit() or int(i) >= len(keys) or keys[int(i)] not in t:
        answer("Список устарел, набери /maplogo ещё раз.", True)
        return
    key = keys[int(i)]
    if kind == "mlx":
        t[key].pop("logo", None)
        profiles[uid].pop("await", None)  # картинку больше не ждём
        api("sendMessage", {"chat_id": chat, "text": f"🗑 Логотип карты «{t[key]['name']}» убран."})
    else:
        profiles[uid]["await"] = "maplogo:" + key
        payload = {"chat_id": chat, "text": f"🖼 Пришли картинку-логотип для карты «{t[key]['name']}» одним фото."}
        if t[key].get("logo"):
            payload["reply_markup"] = {"inline_keyboard": [[{"text": "🗑 Убрать логотип", "callback_data": f"mlx:{i}"}]]}
        api("sendMessage", payload)
    answer()


def maplogo_photo(msg, key):
    t = ensure_tracks(club_chat())
    profiles.get(msg["from"]["id"], {}).pop("await", None)
    if key not in t or not is_owner(msg["from"]["id"]):
        reply(msg, "Не получилось: набери /maplogo и выбери карту ещё раз.")
        return
    t[key]["logo"] = duel_image(msg)[0]
    t[key]["lv"] = int(time.time())
    reply(msg, f"✅ Логотип для карты «{t[key]['name']}» поставлен — он уже в приложении. Ещё карту: /maplogo")


def map_logo(key):
    # Байты логотипа карты для сайта (токен бота наружу не попадает)
    e = tracks.get(club_chat(), {}).get(key)
    if not e or not e.get("logo"):
        return None
    cached = _logo_cache.get(key)
    if cached and cached[0] == e["logo"]:
        return cached[1]
    data = tg_file(e["logo"])
    if data:
        _logo_cache[key] = (e["logo"], data)
        while len(_logo_cache) > 60:
            _logo_cache.pop(next(iter(_logo_cache)))
    return data


def score_pair(s):
    nums = re.findall(r"\d+(?:\.\d+)?", s or "")
    return (float(nums[0]), float(nums[1])) if len(nums) >= 2 else None


def same_duel(mine, day, opp, score, track, group):
    # Защита от «дюпа»: скрин недоезженной дуэли (один проехал лид+чейз, другой пока только лид),
    # а потом скрин той же дуэли целиком. Счёт разный, но одно из двух чисел не меняется —
    # по нему, дате, сопернику и трассе узнаём ту же дуэль. 0 и 100 игра даёт автоматически,
    # поэтому по ним не сравниваем. Строки с того же скрина — всегда разные дуэли.
    new = score_pair(score)
    if not new:
        return None
    for d in mine:
        if d["d"] != day or d["o"].lower() != opp.lower() or d.get("g") == group:
            continue
        if track and d.get("t") and d["t"].lower() != track.lower():
            continue
        old = score_pair(d["s"])
        if old and any(a == b and a not in (0.0, 100.0) for a, b in zip(old, new)):
            return d
    return None


def update_duel(chat_id, uid, ex, res, score):
    # Оставляем более полный вариант счёта (с большей суммой баллов) и правим копию у соперника
    old, new = score_pair(ex["s"]), score_pair(score)
    if old and sum(new) <= sum(old):
        return
    old_mirror = mirror(ex["s"])
    for other, lst in duels.get(chat_id, {}).items():
        if other == uid:
            continue
        for d in lst:
            # Вторая половина той же дуэли: копия у соперника (by == uid) или его исходная запись (ex — копия)
            if d.get("g") == ex.get("g") and d["d"] == ex["d"] and d["s"] == old_mirror \
                    and (d.get("by") == uid or other == ex.get("by")):
                d["s"], d["r"] = mirror(score), OPPOSITE.get(res, res)
    ex["s"], ex["r"] = score, res


def add_duels(chat_id, uid, opponent, rows, group, items=None, alerts=None):
    # Возвращает (новых, повторов, вне срока). В items дописывает каждую строку скрина с её судьбой,
    # чтобы бот показал в ответе, что именно прочитал и куда засчитал.
    # Дуэль с участником клуба засчитывается обоим с первого же скрина: сопернику она пишется
    # с пометкой «by» (по чьему отчёту). Когда соперник потом сдаёт ту же дуэль со своей стороны,
    # второй раз она не считается, а помечается «c» — подтверждена обоими.
    # В alerts — подозрение на накрутку: другой участник уже сдал те же дуэли с тем же соперником.
    items = [] if items is None else items
    ensure_tracks(chat_id)  # заполнить счётчик карт до того, как добавим новые дуэли
    mine = duels.setdefault(chat_id, {}).setdefault(uid, [])
    have = {(d["d"], d["o"].lower(), d["s"]) for d in mine}
    have |= {(d, o, mirror(s)) for d, o, s in have}  # у соперника счёт может быть записан наоборот
    today = msk_today()
    oldest = today - datetime.timedelta(days=DUEL_KEEP_DAYS)
    added = dup = skip = 0
    opp = re.sub(r"^\s*vs\.?\s*", "", opponent or "", flags=re.I).strip() or "?"
    opp_uid, my_name = uid_by_game_name(chat_id, opp), game_name(chat_id, uid)
    names = club_names()
    same = {}  # другой участник → сколько точно таких же дуэлей он уже сдал
    for row in rows:
        score, res = norm_score(row.get("score")), row.get("result", "")
        try:
            day = datetime.date.fromisoformat(str(row.get("date", "")).strip())
        except ValueError:
            skip += 1
            items.append((None, opp, res, score, "skip"))
            continue
        if day > today + datetime.timedelta(days=1) or day < oldest:
            skip += 1
            items.append((day, opp, res, score, "skip"))
            continue
        key = (day.isoformat(), opp.lower(), score)
        if key in have:
            dup += 1
            ex = next((d for d in mine if d["d"] == key[0] and d["o"].lower() == key[1]
                       and d["s"] in (score, mirror(score))), {})
            if ex.get("by") and ex["by"] != uid:
                # Эту дуэль уже засчитали по отчёту соперника — теперь её подтвердили обе стороны
                ex["c"] = 1
                for d in duels[chat_id].get(ex["by"], []):
                    if d["d"] == key[0] and d.get("g") == ex.get("g") and d["s"] in (score, mirror(score)):
                        d["c"] = 1
                items.append((day, opp, res, score, "conf"))
            else:
                items.append((day, opp, res, score, "dup"))
            continue
        ex = same_duel(mine, key[0], opp, score, (row.get("track") or "")[:40], group)
        if ex:
            # Та же дуэль, только тогда ещё не доезженная: обновляем счёт, второй раз не считаем
            update_duel(chat_id, uid, ex, res, score)
            dup += 1
            items.append((day, opp, res, score, "upd"))
            continue
        have.add(key)
        have.add((key[0], key[1], mirror(score)))
        rec = {"d": key[0], "o": opp, "r": res, "s": score, "t": (row.get("track") or "")[:40], "g": group}
        credited = False  # "" — сопернику не писали, "ok" — засчитана и ему, иначе причина, почему ему не в норму
        if opp_uid and opp_uid != uid and my_name:
            theirs = duels[chat_id].setdefault(opp_uid, [])
            old = [d for d in theirs if d["d"] == rec["d"] and d["o"].lower() == my_name.lower()
                   and d["s"] in (score, mirror(score))]
            if old:  # соперник сдал её раньше, когда зачёт обоим ещё не работал
                for d in old:
                    d["c"] = 1
                rec["c"] = 1
            else:
                theirs_rec = {"d": rec["d"], "o": my_name, "r": OPPOSITE.get(res, res), "s": mirror(score),
                              "t": rec["t"], "g": group, "by": uid}
                theirs.append(theirs_rec)
                theirs.sort(key=lambda d: d["d"])
                credited = why_not_counted(theirs_rec, names) or "ok"
        for other, lst in duels[chat_id].items():
            if other not in (uid, opp_uid) and any(d["d"] == rec["d"] and d["o"].lower() == opp.lower()
                                                   and d["s"] == score for d in lst):
                same[other] = same.get(other, 0) + 1
        mine.append(rec)
        if not rec.get("c"):  # если соперник уже сдал эту дуэль раньше, карту за неё он уже посчитал
            track_count(chat_id, rec["t"], +1)
        reason = why_not_counted(rec, names)
        if not reason:
            added += 1
        items.append((day, opp, res, score, "new", rec.get("c"), credited, reason))
    mine.sort(key=lambda d: d["d"])
    if alerts is not None:
        # Одна совпавшая дуэль «100 / 100» бывает и честно, поэтому шумим от двух совпадений
        # или когда совпал весь скрин
        for other, n in same.items():
            if n >= 2 or n == len(rows):
                alerts.append((other, opp, n))
    return added, dup, skip


def prune_duels():
    cutoff = (msk_today() - datetime.timedelta(days=DUEL_KEEP_DAYS)).isoformat()
    for chat in duels.values():
        for uid in list(chat):
            chat[uid] = [d for d in chat[uid] if d["d"] >= cutoff]


def duel_report_text(chat_id, user, s):
    start, end = week_bounds()
    week, norm = duels_in(chat_id, user["id"], start, end), get_norm(chat_id)
    who = name_of(user) + (f" (@{user['username']})" if user.get("username") else "")
    lines = [f"📸 Отчёт по дуэлям: {who}", f"Засчитано новых дуэлей: {s['added']}"]
    extra = []
    nocount = sum(1 for it in s.get("items", []) if it[4] == "new" and len(it) > 7 and it[7])
    if nocount:
        extra.append(f"не в норму: {nocount} (меньше {MIN_SCORE:g} балла или соперник не из клуба)")
    if s["dup"]:
        extra.append(f"повторы (уже были): {s['dup']}")
    if s["skip"]:
        extra.append(f"вне срока или без даты: {s['skip']}")
    if extra:
        lines.append(" · ".join(extra).capitalize())
    items = s.get("items", [])
    if items:
        # Каждая прочитанная дуэль и куда она пошла: так видно, почему «за неделю» меньше, чем на скрине
        lines.append("")
        for day, opp, res, score, status, *extra in items[:25]:
            when = f"{day:%d.%m}" if day else "без даты"
            if status == "new":
                confirmed, credited, reason = (list(extra) + [None, None, ""])[:3]
                if reason:
                    where = f"не в норму: {reason}"
                else:
                    ws, we = week_bounds(day)
                    where = "эта неделя" if ws == start else f"неделя {week_label(ws, we)}"
                    if confirmed:
                        where += " ✔ подтверждена соперником"
                if credited:
                    where += " · засчитана и сопернику" if credited == "ok" else f" · сопернику не в норму: {credited}"
            else:
                where = {"dup": "уже засчитана раньше", "skip": "вне срока",
                         "upd": "та же дуэль, что уже сдана (тогда была не доезжена) — счёт обновлён, второй раз не считается",
                         "conf": "уже засчитана по отчёту соперника ✔ подтверждена"}[status]
            lines.append(f"{when} vs {opp} — {RESULT_RU.get(res, res)} ({score}) → {where}")
        if len(items) > 25:
            lines.append(f"…и ещё {len(items) - 25}")
    if s["bad"]:
        lines.append(f"Не похоже на «Историю заездов»: {s['bad']} скрин.")
    if s.get("blurry"):
        lines.append(f"⚠️ Плохо читается: {s['blurry']} скрин. Часть строк могла не попасть в отчёт — "
                     "пришли этот скрин ещё раз файлом (без сжатия). Повторы бот не посчитает.")
    for e in s["errors"]:
        lines.append("⚠️ " + e)
    hint = pace(norm - week, (end - msk_today()).days + 1)
    status = "✅ норма выполнена" if week >= norm else f"осталось {norm - week}" + (f" ({hint})" if hint else "")
    lines.append(f"\nЗа неделю {week_label(start, end)}: {week} из {norm} — {status}")
    if s.get("undone"):
        lines.append("\n❌ " + s["undone"])
    return "\n".join(lines)


def handle_duel_report(msg):
    # ИИ читает скрин долго (до минуты). Если делать это прямо в обработчике, бот не отвечает,
    # Render считает его зависшим и перезапускает. Поэтому скрины читает отдельный поток по очереди.
    key = (msg["chat"]["id"], msg["message_id"])
    if key in _seen_msgs:
        return
    _seen_msgs.add(key)
    if DUEL_ASYNC:
        DUEL_QUEUE.put(msg)
    else:
        apply_duel(msg, *read_duel(msg))


def duel_worker():
    while True:
        msg = DUEL_QUEUE.get()
        try:
            res, err = read_duel(msg)  # без блокировки: остальные сообщения бот обрабатывает в это время
            with LOCK:
                load_state()
                apply_duel(msg, res, err)
                save_state()
        except Exception as e:
            print("Ошибка обработки скрина:", repr(e))


def read_duel(msg):
    if not GEMINI_KEY:
        print("Не задан GEMINI_API_KEY")
        return None, "не задан GEMINI_API_KEY в Render, скрины не читаются"
    file_id, mime = duel_image(msg)
    image = tg_file(file_id)
    if not image:
        return None, "не удалось скачать скрин из Телеграма"
    return gemini_read(image, mime)


def apply_duel(msg, res, err):
    chat_id, user = msg["chat"]["id"], msg["from"]
    group = msg.get("media_group_id") or str(msg["message_id"])
    key = (chat_id, group)
    s = _albums.get(key)
    if not s:
        s = _albums[key] = {"added": 0, "dup": 0, "skip": 0, "bad": 0, "errors": [], "reply": None, "items": [],
                            "uid": user["id"], "t": time.time()}
    for k in [k for k, v in _albums.items() if time.time() - v["t"] > 3600]:
        del _albums[k]

    if err:
        if err not in s["errors"]:
            s["errors"].append(err)
    elif not res.get("is_duel_history") or not res.get("rows"):
        s["bad"] += 1
    else:
        alerts = []
        before = len(s["items"])
        a, d, k = add_duels(chat_id, user["id"], res.get("opponent"), res["rows"], group, s["items"], alerts)
        uts_on_report(chat_id, user["id"], res.get("opponent"), s["items"][before:])  # матч мини-УТС?
        s["added"] += a
        s["dup"] += d
        s["skip"] += k
        if res.get("unreadable") or small_photo(msg):
            s["blurry"] = s.get("blurry", 0) + 1
        print(f"Дуэли {name_of(user)}: {res.get('opponent')} +{a}, повторов {d}, вне срока {k}")
        for other, opp, n in alerts:
            o = members.get(chat_id, {}).get(other, {"first_name": str(other)})
            who = name_of(user) + (f" @{user['username']}" if user.get("username") else "")
            cid = str(chat_id)[4:] if str(chat_id).startswith("-100") else ""
            link = (f"\nСкрин: https://t.me/c/{cid}/" + (f"{msg['message_thread_id']}/" if msg.get("message_thread_id") else "")
                    + str(msg["message_id"])) if cid else ""
            print(f"Подозрение на накрутку: {who} и {name_of(o)}, vs {opp}, совпало {n}")
            dm_admins(chat_id, f"🚩 Возможная накрутка: {who} сдал(а) дуэли vs {opp}, которые уже сдал(а) "
                               f"{name_of(o)} — совпало {n} шт. (та же дата, тот же соперник, тот же счёт). "
                               f"Похоже на один и тот же скрин.{link}\nЕсли это так — отмени отчёт кнопкой под ответом бота.")

    text = duel_report_text(chat_id, user, s)
    markup = {"inline_keyboard": [[{"text": "❌ Отменить этот отчёт", "callback_data": f"du:{user['id']}:{group}"}]]}
    if s["added"] == 0:
        markup = {"inline_keyboard": []}
    if s["reply"]:
        api("editMessageText", {"chat_id": chat_id, "message_id": s["reply"], "text": text, "reply_markup": markup})
    else:
        payload = {"chat_id": chat_id, "text": text, "reply_markup": markup,
                   "reply_parameters": {"message_id": msg["message_id"], "allow_sending_without_reply": True}}
        if msg.get("message_thread_id"):
            payload["message_thread_id"] = msg["message_thread_id"]
        s["reply"] = api("sendMessage", payload).get("result", {}).get("message_id")
    prune_duels()
    schedule_sheet_sync(chat_id)


def handle_callback(cq):
    data = cq.get("data", "")
    if data.startswith("nw:"):
        norm_warn_callback(cq)
        return
    if data.startswith("pf:"):
        profile_callback(cq)
        return
    if data.startswith("pfdel:"):
        photo_remove_callback(cq)
        return
    if data.startswith(("ml:", "mlx:")):
        maplogo_callback(cq)
        return
    if data.startswith("lv:"):
        livery_callback(cq)
        return
    if data.startswith("lvrm:"):
        livery_remove_callback(cq)
        return
    if data.startswith("lvcar:"):
        livery_car_callback(cq)
        return
    if data.startswith("lvo:"):
        order_callback(cq)
        return
    if data.startswith("ut:"):
        uts_callback(cq)
        return
    if data.startswith("dz:"):
        dz_callback(cq)
        return
    msg = cq.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    if not data.startswith("du:") or not chat_id:
        api("answerCallbackQuery", {"callback_query_id": cq["id"]})
        return
    _, uid, group = data.split(":", 2)
    uid = int(uid)
    clicker = cq["from"]
    # Отменять могут только администраторы: свои отчёты и отчёты тех, кто ниже рангом.
    # Отчёты администраторов отменяет владелец.
    my = get_level(chat_id, clicker["id"])
    if my < 2:
        deny = "Отменять отчёты могут только администраторы."
    elif clicker["id"] != uid and get_level(chat_id, uid) >= my:
        deny = "Отчёт администратора может отменить только владелец."
    else:
        deny = ""
    if deny:
        api("answerCallbackQuery", {"callback_query_id": cq["id"], "show_alert": True, "text": deny})
        return
    mine = duels.get(chat_id, {}).get(uid, [])
    removed = sum(1 for d in mine if d.get("g") == group and not d.get("by"))
    for d in mine:
        if d.get("g") == group and not d.get("by") and not d.get("c"):
            track_count(chat_id, d.get("t"), -1)
    if uid in duels.get(chat_id, {}):
        duels[chat_id][uid] = [d for d in mine if d.get("g") != group or d.get("by")]
    # Снимаем и дуэли, которые этот отчёт засчитал соперникам
    for other, lst in duels.get(chat_id, {}).items():
        if other != uid:
            duels[chat_id][other] = [d for d in lst if not (d.get("by") == uid and d.get("g") == group)]
    start, end = week_bounds()
    week = duels_in(chat_id, uid, start, end)
    text = (msg.get("text", "") + f"\n\n❌ Отменено ({name_of(clicker)}): снято дуэлей {removed}. "
            f"Теперь за неделю: {week} из {get_norm(chat_id)}")
    api("editMessageText", {"chat_id": chat_id, "message_id": msg["message_id"], "text": text,
                            "reply_markup": {"inline_keyboard": []}})
    api("answerCallbackQuery", {"callback_query_id": cq["id"], "text": f"Снято дуэлей: {removed}"})
    log_action({"chat": {"id": chat_id}, "from": clicker}, members.get(chat_id, {}).get(uid, {"id": uid}),
               f"отменил отчёт по дуэлям (−{removed})")
    schedule_sheet_sync(chat_id)


def duel_people(chat_id):
    # Все, кого показывать в норме: пилоты из таблицы (по @нику) и все, кто сдавал отчёты
    by_name = {(u.get("username") or "").lower(): u for u in members.get(chat_id, {}).values() if u.get("username")}
    people, seen = [], set()
    for r in get_pilots():
        tgname = col(r, 2).lower().lstrip("@")
        u = by_name.get(tgname) if tgname != "—" else None
        people.append({"name": col(r, 1), "tg": "" if tgname == "—" else tgname, "uid": u["id"] if u else None})
        if u:
            seen.add(u["id"])
    for uid in duels.get(chat_id, {}):
        if uid not in seen and duels[chat_id][uid]:
            u = members.get(chat_id, {}).get(uid, {})
            people.append({"name": u.get("first_name", str(uid)), "tg": u.get("username", ""), "uid": uid})
    return people


def reply_norm(msg, args):
    chat_id = msg["chat"]["id"]
    norm = get_norm(chat_id)
    start, end = week_bounds()
    pstart, pend = week_bounds(back=1)
    if args and args[0].lower() in ("all", "все", "всех"):
        rows = []
        for p in duel_people(chat_id):
            n = duels_in(chat_id, p["uid"], start, end) if p["uid"] else 0
            rows.append((n, p))
        rows.sort(key=lambda x: -x[0])
        done = sum(1 for n, _ in rows if n >= norm)
        head = (f"🏁 Норма дуэлей за неделю {week_label(start, end)}: {norm}\n"
                f"Выполнили: {done} из {len(rows)}\n")
        lines = [f"{'✅' if n >= norm else '⏳'} {p['name']}{' @' + p['tg'] if p['tg'] else ''} — {n}/{norm}"
                 + ("" if p["uid"] else " (бот не знает @ника в чате)") for n, p in rows]
        send_long(chat_id, head, lines)
        return
    target, _ = find_target(msg, args)
    target = target or msg["from"]
    uid = target["id"]
    week, prev = duels_in(chat_id, uid, start, end), duels_in(chat_id, uid, pstart, pend)
    status = "✅ выполнена" if week >= norm else f"осталось {norm - week}"
    recent = [d for d in duels.get(chat_id, {}).get(uid, []) if d["d"] >= start.isoformat()][-10:]
    text = (f"🏁 Дуэли: {name_of(target)}\n"
            f"Эта неделя {week_label(start, end)}: {week} из {norm} — {status}\n"
            f"Прошлая неделя {week_label(pstart, pend)}: {prev} из {norm}")
    if recent:
        text += "\n\nПоследние за неделю:\n" + "\n".join(
            f"{datetime.date.fromisoformat(d['d']):%d.%m} vs {d['o']} — {RESULT_RU.get(d['r'], d['r'])} ({d['s']})"
            + (f" ✗ не в норму: {why_not_counted(d)}" if why_not_counted(d) else "")
            for d in recent)
    reply(msg, text)


def cmd_setnorm(msg, args, my):
    if not args or not args[0].isdigit() or not 0 < int(args[0]) <= 500:
        reply(msg, f"Напиши так: /setnorm 20. Сейчас норма {get_norm(msg['chat']['id'])} дуэлей в неделю.")
        return
    settings.setdefault(msg["chat"]["id"], {})["norm"] = int(args[0])
    log_action(msg, None, f"поставил норму дуэлей {args[0]} в неделю")
    reply(msg, f"🏁 Норма: {args[0]} дуэлей в неделю (пн–вс). Посмотреть всех: /norm all")
    schedule_sheet_sync(msg["chat"]["id"])


def cmd_duelhere(msg, args, my):
    conf = settings.setdefault(msg["chat"]["id"], {})
    if args and args[0].lower() in ("off", "выкл"):
        conf.pop("duel_thread", None)
        reply(msg, "Учёт дуэлей по скринам выключен.")
        return
    conf["duel_thread"] = msg.get("message_thread_id", 0)
    log_action(msg, None, "включил учёт дуэлей в этой теме")
    warn = "" if GEMINI_KEY else "\n⚠️ Ещё не задан GEMINI_API_KEY в Render — без него скрины не читаются."
    reply(msg, "📸 Теперь бот считает дуэли по скринам «История заездов» из этой темы. "
               f"Норма: {get_norm(msg['chat']['id'])} в неделю. Проверить себя: /norm, всех: /norm all" + warn)


def cmd_recheck(msg, args, my):
    # Перечитать скрин, который ИИ не смог прочитать (дуэли засчитываются автору скрина, повторы не считаются)
    chat_id = msg["chat"]["id"]
    src = msg.get("reply_to_message") or {}
    author = src.get("from") or {}
    if not duel_image(src)[0] or not author:
        reply(msg, "Ответь /recheck на сообщение со скрином «История заездов».")
        return
    if author["id"] != msg["from"]["id"] and get_level(chat_id, msg["from"]["id"]) < 1:
        reply(msg, "Перечитать чужой скрин может только модератор.")
        return
    _seen_msgs.discard((chat_id, src["message_id"]))
    _albums.pop((chat_id, str(src["message_id"])), None)  # новый ответ, а не правка старой ошибки
    src = dict(src, chat=msg["chat"])
    src.pop("media_group_id", None)  # перечитанный скрин — отдельный отчёт со своей кнопкой отмены
    handle_duel_report(src)


def duels_json():
    with LOCK:
        load_state()
        start, end = week_bounds()
        pstart, pend = week_bounds(back=1)
        norm, pilots, names = DUEL_NORM_DEFAULT, {}, club_names()
        for chat_id, people in duels.items():
            norm = get_norm(chat_id)
            for uid, lst in people.items():
                uname = (members.get(chat_id, {}).get(uid, {}).get("username") or "").lower()
                if not uname:
                    continue
                p = pilots.setdefault(uname, {"week": 0, "prev": 0, "total": 0})
                p["week"] += duels_in(chat_id, uid, start, end)
                p["prev"] += duels_in(chat_id, uid, pstart, pend)
                p["total"] += len(lst)
                p["streak"] = norm_streak(chat_id, uid, norm)
                p["stats"] = pilot_stats(counted(chat_id, uid, names))
        club = club_chat()
        awards = {"week": week_awards(club, start, end, names), "prev": week_awards(club, pstart, pend, names)} if club else {}
        # Профили: есть ли фото машины (v — версия, чтобы сайт подхватил новое фото) и подпись
        prof = {(p.get("username") or "").lower(): {"photo": p.get("v", 0) if p.get("car") else 0,
                                                    "about": p.get("about", "")}
                for p in profiles.values() if p.get("username") and (p.get("car") or p.get("about"))}
        return {"week": week_label(start, end), "prev_week": week_label(pstart, pend), "norm": norm,
                "pilots": pilots, "profiles": prof, "tracks": tracks_json(), "awards": awards,
                "rivalries": rivalries(club, names) if club else [], "designers": liveries_json(), "uts": uts_json(),
                "elo": elo_json(club, names) if club else [], "ach": ach_json(club) if club else {},
                "ach_info": {k: list(v) for k, v in ACH.items()},
                "lfg": [uts_name(int(k)) for k in settings.get(club, {}).get("dz", {}).get("lfg", {})] if club else []}


def tracks_json():
    # Популярные карты для приложения: за эту неделю и за всё время (по чату клуба)
    chat_id = club_chat()
    if not chat_id:
        return {"week": [], "all": []}
    fresh = chat_id not in tracks
    start, end = week_bounds()
    out = {"week": tracks_top(chat_id, start, end), "all": tracks_top(chat_id)}
    out["kings"] = map_kings(chat_id)
    if fresh:
        save_state()  # счётчик «за всё время» только что заполнили из сданных дуэлей — запоминаем
    return out


# ---------- Статистика для приложения: личные цифры, короли карт, награды недели, серии, соперничества ----------
# Всё считается только по дуэлям, которые идут в норму, и только за то время, что бот хранит дуэли (10 недель).
AWARD_MIN = 10  # «Снайпер недели» — только у кого хотя бы столько дуэлей за неделю


def counted(chat_id, uid, names, start=None, end=None):
    a, b = (start.isoformat() if start else ""), (end.isoformat() if end else "9999")
    return [d for d in duels.get(chat_id, {}).get(uid, []) if a <= d["d"] <= b and not why_not_counted(d, names)]


def pilot_name(chat_id, uid):
    return game_name(chat_id, uid) or members.get(chat_id, {}).get(uid, {}).get("first_name", "")


def norm_streak(chat_id, uid, norm, today=None):
    # Сколько недель подряд выполнена норма: от прошлой недели назад до первой невыполненной.
    # Текущая неделя добавляется, только когда норма на ней уже выполнена (иначе серия не обнуляется до понедельника).
    n = 1 if duels_in(chat_id, uid, *week_bounds(today)) >= norm else 0
    for back in range(1, DUEL_KEEP_DAYS // 7 + 1):
        if duels_in(chat_id, uid, *week_bounds(today, back=back)) < norm:
            break
        n += 1
    return n


def pilot_stats(lst):
    # Личные цифры для профиля: победы, лучший счёт, любимая карта, с кем катает чаще всего
    maps, riv = {}, {}
    for d in lst:
        if d.get("t"):
            maps.setdefault(track_key(d["t"]), [d["t"].strip(), 0])[1] += 1
        r = riv.setdefault(d["o"].strip().lower(), [d["o"].strip(), 0, 0, 0])  # [ник, дуэлей, побед, поражений]
        r[1] += 1
        r[2] += d["r"] == "win"
        r[3] += d["r"] == "loss"
    return {"n": len(lst), "w": sum(d["r"] == "win" for d in lst), "l": sum(d["r"] == "loss" for d in lst),
            "best": max((own_score(d["s"]) for d in lst), default=0),
            "map": max(maps.values(), key=lambda x: x[1])[0] if maps else "",
            "rivals": sorted(riv.values(), key=lambda x: (-x[1], x[0].lower()))[:3]}


def club_uids(chat_id):
    # Пилоты по порядку ника — чтобы при равенстве награды и короли не прыгали между обновлениями
    return sorted(duels.get(chat_id, {}), key=lambda uid: pilot_name(chat_id, uid).lower())


def week_awards(chat_id, start, end, names=None):
    names = club_names() if names is None else names
    out = {}
    for uid in club_uids(chat_id):
        lst = counted(chat_id, uid, names, start, end)
        who = pilot_name(chat_id, uid)
        if not lst or not who:
            continue
        n, w = len(lst), sum(d["r"] == "win" for d in lst)
        if n > out.get("active", ["", 0])[1]:
            out["active"] = [who, n]
        if n >= AWARD_MIN:
            pct, cur = round(w / n * 100), out.get("sniper")
            if not cur or (pct, n) > (cur[1], cur[2]):
                out["sniper"] = [who, pct, n]
        for d in lst:
            sc = score_pair(d["s"])
            if sc and sc[0] > out.get("record", ["", 0])[1]:
                out["record"] = [who, sc[0], d.get("t", ""), d["o"]]
            # Дуэль недели — самая близкая по счёту, где оба по-настоящему проехали
            if sc and min(sc) >= MIN_SCORE and d["r"] == "win":
                gap = round(abs(sc[0] - sc[1]), 1)
                if "closest" not in out or gap < out["closest"][4]:
                    out["closest"] = [who, d["o"], sc[0], sc[1], gap, d.get("t", ""), d["d"]]
    return out


def map_kings(chat_id, names=None):
    # Король карты — у кого больше всех побед на ней (при равенстве — меньше поражений)
    names = club_names() if names is None else names
    best = {}
    for uid in club_uids(chat_id):
        who = pilot_name(chat_id, uid)
        if not who:
            continue
        per = {}
        for d in counted(chat_id, uid, names):
            if d.get("t"):
                p = per.setdefault(track_key(d["t"]), [0, 0])
                p[0] += d["r"] == "win"
                p[1] += d["r"] == "loss"
        for k, (w, l) in per.items():
            if w and (k not in best or (w, -l) > (best[k][1], best[k][2])):
                best[k] = [who, w, -l]
    return {k: [v[0], v[1]] for k, v in best.items()}


def rivalries(chat_id, names=None, limit=5):
    # Пары пилотов клуба, которые чаще всего катают друг с другом, и личный счёт между ними.
    # Одна дуэль лежит у обоих пилотов (у соперника — по отчёту первого), поэтому считаем её один раз.
    names = club_names() if names is None else names
    seen, pairs = set(), {}
    for uid in club_uids(chat_id):
        me = pilot_name(chat_id, uid)
        if not me:
            continue
        for d in counted(chat_id, uid, names):
            opp = d["o"].strip()
            if opp.lower() not in names or opp.lower() == me.lower():
                continue
            key = (d["d"], tuple(sorted((me.lower(), opp.lower()))), tuple(sorted(score_pair(d["s"]) or ())))
            if key in seen:
                continue
            seen.add(key)
            a, b = sorted((me, opp), key=str.lower)
            p = pairs.setdefault((a.lower(), b.lower()), [a, b, 0, 0, 0])  # [пилот A, пилот B, побед A, побед B, дуэлей]
            p[4] += 1
            if d["r"] in ("win", "loss"):
                a_won = (d["r"] == "win") == (me.lower() == a.lower())
                p[2 if a_won else 3] += 1
    return sorted(pairs.values(), key=lambda p: (-p[4], p[0].lower()))[:limit]


def sheet_payload(chat_id):
    names = {u["id"]: u for u in members.get(chat_id, {}).values()}
    names_ = club_names()
    duel_rows = []
    for uid, lst in duels.get(chat_id, {}).items():
        u = names.get(uid, {})
        for d in lst:
            day = datetime.date.fromisoformat(d["d"])
            duel_rows.append([week_label(*week_bounds(day)), day.strftime("%d.%m.%Y"), u.get("first_name", str(uid)),
                              "@" + u["username"] if u.get("username") else "", d["o"],
                              RESULT_RU.get(d["r"], d["r"]), d["s"], d.get("t", ""),
                              ("не в норму: " + why_not_counted(d, names_)) if why_not_counted(d, names_) else
                              "✔ сдали оба" if d.get("c") else "по отчёту соперника" if d.get("by") else ""])
    duel_rows.sort(key=lambda r: (r[1][6:] + r[1][3:5] + r[1][:2], r[2]), reverse=True)
    norm = get_norm(chat_id)
    weeks = [week_bounds(back=i) for i in range(4)]
    norm_rows = []
    for p in duel_people(chat_id):
        counts = [duels_in(chat_id, p["uid"], a, b) if p["uid"] else 0 for a, b in weeks]
        norm_rows.append([p["name"], "@" + p["tg"] if p["tg"] else "", counts[0], norm,
                          "✅" if counts[0] >= norm else f"осталось {norm - counts[0]}"] + counts[1:])
    norm_rows.sort(key=lambda r: -r[2])
    return {
        "duels_head": ["Неделя", "Дата", "Пилот", "Телеграм", "Соперник", "Результат", "Счёт", "Трасса",
                       "Отметка"],
        "duels": duel_rows,
        "norm_head": ["Пилот", "Телеграм", f"Эта неделя {week_label(*weeks[0])}", "Норма", "Статус"]
                     + [week_label(*w) for w in weeks[1:]],
        "norm": norm_rows,
    }


def schedule_sheet_sync(chat_id):
    # Выгружаем в Google-таблицу через Apps Script. Ждём пару секунд, чтобы альбом ушёл одной выгрузкой.
    if not SHEET_HOOK:
        return
    if _sheet["timer"]:
        _sheet["timer"].cancel()
    _sheet["timer"] = threading.Timer(5, sync_sheet, args=(chat_id,))
    _sheet["timer"].start()


def sync_sheet(chat_id):
    try:
        r = requests.post(SHEET_HOOK, json=sheet_payload(chat_id), timeout=30)
        if r.status_code >= 300 or "ok" not in r.text[:50]:
            print(f"Ошибка выгрузки в таблицу: HTTP {r.status_code} {r.text[:200]}")
    except Exception as e:
        print("Ошибка выгрузки в таблицу:", e)


# ---------- Автоматика: напоминания, итоги недели, сообщения админам, резервная копия ----------
REMIND_DAY, REMIND_HOUR = 4, 18    # пятница 18:00 МСК — напоминание тем, у кого меньше половины нормы
SUMMARY_DAY, SUMMARY_HOUR = 0, 10  # понедельник 10:00 МСК — итоги прошлой недели
NUDGE_DAY, NUDGE_HOUR = 3, 18      # четверг 18:00 МСК — личное напоминание в ЛС тем, кто не добрал норму
_auto = {"ping": 0, "backup": 0}


def msk_now():
    return datetime.datetime.utcnow() + datetime.timedelta(hours=3)


def admin_ids(chat_id):
    ids = {m["user"]["id"] for m in get_admins(chat_id)}
    ids |= {uid for uid, lvl in staff.get(chat_id, {}).items() if lvl >= 2}
    return ids


def dm_admins(chat_id, text, markup=None):
    # Писать в ЛС бот может только тем, кто хоть раз открыл его и нажал Start
    sent = 0
    for uid in admin_ids(chat_id):
        payload = {"chat_id": uid, "text": text}
        if markup:
            payload["reply_markup"] = markup
        if api("sendMessage", payload).get("ok"):
            sent += 1
        else:
            print(f"Не смог написать админу {uid} в ЛС: пусть откроет @{BOT_USERNAME} и нажмёт Start")
    return sent


def post_to_reports(chat_id, text):
    payload = {"chat_id": chat_id, "text": text}
    if settings.get(chat_id, {}).get("duel_thread"):
        payload["message_thread_id"] = settings[chat_id]["duel_thread"]
    api("sendMessage", payload)


def mention(p):
    return f"@{p['tg']}" if p["tg"] else p["name"]


def norm_rows(chat_id, start, end):
    return [(duels_in(chat_id, p["uid"], start, end) if p["uid"] else 0, p) for p in duel_people(chat_id)]


def friday_reminder(chat_id, now):
    start, end = week_bounds(now.date())
    norm = get_norm(chat_id)
    behind = sorted([(n, p) for n, p in norm_rows(chat_id, start, end) if n < norm / 2], key=lambda x: x[0])
    if not behind:
        return
    left = (end - now.date()).days + 1
    post_to_reports(chat_id, f"⏰ До конца недели {week_label(start, end)} осталось {left} дн. Норма — {norm} дуэлей.\n"
                             "Пока меньше половины:\n" + "\n".join(f"{mention(p)} — {n}/{norm} ({pace(norm - n, left)})"
                                                                   for n, p in behind)
                    + "\n\nСкрины «История заездов» кидайте сюда.")


def thursday_nudge(chat_id, now):
    # Личное напоминание в ЛС тем, кто отстаёт от нормы. Дойдёт только до тех, кто хоть раз нажал Start у бота.
    start, end = week_bounds(now.date())
    norm, left_days = get_norm(chat_id), (end - now.date()).days + 1
    sent = 0
    for n, p in norm_rows(chat_id, start, end):
        if p["uid"] and n < norm:
            text = (f"🏁 {p['name']}, на этой неделе у тебя {n} из {norm} дуэлей — осталось {norm - n}.\n"
                    f"До конца недели {left_days} дн. (до воскресенья включительно) — {pace(norm - n, left_days)}.\n"
                    "Скрины «История заездов» кидай в тему отчётов клуба.\n"
                    "Не с кем катать? Напиши в чате «ищу дуэль» — бот найдёт соперника.")
            sent += bool(api("sendMessage", {"chat_id": p["uid"], "text": text}).get("ok"))
    print(f"Напоминание о норме в ЛС: доставлено {sent}")


def awards_text(aw):
    lines = []
    if aw.get("active"):
        lines.append(f"🔥 Самый активный — {aw['active'][0]}: {aw['active'][1]} дуэл.")
    if aw.get("sniper"):
        lines.append(f"🎯 Снайпер — {aw['sniper'][0]}: {aw['sniper'][1]}% побед из {aw['sniper'][2]} дуэл.")
    if aw.get("record"):
        r = aw["record"]
        lines.append(f"🚀 Рекорд — {r[0]}: {r[1]:g} балла" + (f" на {r[2]}" if r[2] else ""))
    if aw.get("closest"):
        c = aw["closest"]
        lines.append(f"⚔️ Дуэль недели — {c[0]} vs {c[1]}: {c[2]:g} / {c[3]:g}, разница {c[4]:g}" + (f" ({c[5]})" if c[5] else ""))
    return "\n".join(lines)


def monday_summary(chat_id, now):
    start, end = week_bounds(now.date(), back=1)
    norm = get_norm(chat_id)
    rows = sorted(norm_rows(chat_id, start, end), key=lambda x: -x[0])
    done = [(n, p) for n, p in rows if n >= norm]
    fail = [(n, p) for n, p in rows if n < norm]
    text = f"🏁 Итоги недели {week_label(start, end)}: норму ({norm}) выполнили {len(done)} из {len(rows)}"
    if done:
        text += "\n\n" + "\n".join(f"✅ {p['name']} — {n}" for n, p in done)
    if fail:
        text += "\n\n" + "\n".join(f"❌ {p['name']} — {n}/{norm}" for n, p in fail)
    aw = "\n".join(x for x in (awards_text(week_awards(chat_id, start, end)), uts_week_champion(chat_id, start, end)) if x)
    if aw:
        text += "\n\n🏆 Награды недели\n" + aw
    post_to_reports(chat_id, text)
    # Кто две недели подряд не сдал ни одной дуэли — админам в ЛС отдельным списком
    p2start, p2end = week_bounds(now.date(), back=2)
    idle = [p for n, p in rows if n == 0 and (not p["uid"] or duels_in(chat_id, p["uid"], p2start, p2end) == 0)]
    if idle:
        dm_admins(chat_id, f"😴 Две недели подряд ({week_label(p2start, p2end)} и {week_label(start, end)}) "
                           f"ни одной дуэли в норму: {len(idle)}\n" + "\n".join(f"• {p['name']}" + (f" @{p['tg']}" if p["tg"] else "")
                                                                               for p in idle))
    if not fail:
        return
    # Варны не выдаём сами: админ решает, у кого уважительная причина
    buttons = [[{"text": f"⚠️ Варн: {p['name']} ({n}/{norm})",
                 "callback_data": f"nw:{chat_id}:{p['uid']}:{start.isoformat()}"}] for n, p in fail if p["uid"]][:50]
    unknown = [p["name"] for n, p in fail if not p["uid"]]
    msg = (f"📋 Не выполнили норму за неделю {week_label(start, end)}: {len(fail)}.\n"
           "Правило: нет нормы — варн, кроме уважительных причин. Нажми на тех, кому выдать варн.")
    if unknown:
        msg += "\n\nЭтих бот не знает в чате, варн только вручную: " + ", ".join(unknown)
    dm_admins(chat_id, msg, {"inline_keyboard": buttons})


def norm_warn_callback(cq):
    _, chat_id, uid, week = cq["data"].split(":")
    chat_id, uid = int(chat_id), int(uid)
    clicker = cq["from"]
    answer = lambda text, alert=False: api("answerCallbackQuery", {"callback_query_id": cq["id"], "text": text,
                                                                   "show_alert": alert})
    if get_level(chat_id, clicker["id"]) < 2:
        answer("Выдавать варны за норму могут только администраторы.", True)
        return
    warned = settings.setdefault(chat_id, {}).setdefault("norm_warned", {})
    done = warned.setdefault(week, [])
    for old in sorted(warned)[:-8]:  # храним отметки только за последние недели
        del warned[old]
    msg = cq.get("message") or {}
    if msg.get("reply_markup"):
        keep = [row for row in msg["reply_markup"].get("inline_keyboard", []) if row[0].get("callback_data") != cq["data"]]
        api("editMessageReplyMarkup", {"chat_id": msg["chat"]["id"], "message_id": msg["message_id"],
                                       "reply_markup": {"inline_keyboard": keep}})
    if uid in done:
        answer("Этому участнику варн за эту неделю уже выдан.")
        return
    done.append(uid)
    target = members.get(chat_id, {}).get(uid, {"id": uid, "first_name": str(uid)})
    reason = f"норма дуэлей за неделю {week_label(*week_bounds(datetime.date.fromisoformat(week)))}"
    fake = {"chat": {"id": chat_id}, "from": clicker}
    key = (chat_id, uid)
    warns[key] = warns.get(key, 0) + 1
    record_action(chat_id, uid, name_of(target), "warn")
    log_action(fake, target, "варн", reason)
    text = f"⚠️ {name_of(target)}, предупреждение {warns[key]} из {WARN_LIMIT}. Причина: {reason}"
    if warns[key] >= WARN_LIMIT and api("banChatMember", {"chat_id": chat_id, "user_id": uid}).get("ok"):
        warns[key] = 0
        record_action(chat_id, uid, name_of(target), "ban")
        members.get(chat_id, {}).pop(uid, None)
        log_action(fake, target, "автобан", f"{WARN_LIMIT} варна")
        text = f"⚠️ {name_of(target)}: {WARN_LIMIT} предупреждения, автобан. Причина: {reason}"
    post_to_reports(chat_id, text)
    answer(f"Варн выдан: {name_of(target)}")


# ---------- Профиль участника: фото машины и подпись, оформляются в ЛС бота ----------
ABOUT_MAX = 100
PROFILE_INTRO = ("🏎 Твой профиль в приложении клуба.\n"
                 "Здесь можно поставить фото своей машины и короткую подпись — их увидят все во вкладке «Пилоты».\n\n"
                 "Машина на фото обязательно должна быть в ливрее клуба KHR.")
CAR_PROMPT = ("🚗 Пришли скрин своей машины из игры одним фото.\n\n"
              "Важно: машина обязательно должна быть в ливрее клуба KHR. "
              "Фото без клубной ливреи админы уберут.")
_photo_cache = {}  # @ник → (file_id, байты): чтобы не качать одно фото из Телеграма на каждый показ


def club_chat():
    return next((c for c, conf in settings.items() if "duel_thread" in conf), None)


def can_have_profile(user):
    # Профиль только у пилотов из таблицы клуба (по @нику в Телеграме)
    uname = (user or {}).get("username") or ""
    return bool(uname) and find_pilot(uname) is not None


def profile_menu(chat_id, text):
    kb = [[{"text": "🚗 Фото машины", "callback_data": "pf:car"}],
          [{"text": "📝 О себе", "callback_data": "pf:about"}, {"text": "👁 Мой профиль", "callback_data": "pf:view"}],
          [{"text": "🗑 Убрать фото", "callback_data": "pf:del"}]]
    api("sendMessage", {"chat_id": chat_id, "text": text, "reply_markup": {"inline_keyboard": kb}})


def myprofile(msg):
    user = msg["from"]
    if msg["chat"]["type"] != "private":
        payload = {"chat_id": msg["chat"]["id"], "text": "Профиль оформляется в личке бота — нажми кнопку ниже.",
                   "reply_parameters": {"message_id": msg["message_id"], "allow_sending_without_reply": True},
                   "reply_markup": {"inline_keyboard": [[{"text": "🏎 Оформить профиль",
                                                          "url": f"https://t.me/{BOT_USERNAME}?start=profile"}]]}}
        if msg.get("message_thread_id"):
            payload["message_thread_id"] = msg["message_thread_id"]
        api("sendMessage", payload)
        return
    if not can_have_profile(user):
        reply(msg, "Профиль можно оформить, только если ты есть в таблице пилотов клуба (бот ищет тебя по @нику "
                   "в Телеграме). Попроси админа добавить тебя в таблицу.")
        return
    profiles.setdefault(user["id"], {}).pop("await", None)
    profiles[user["id"]]["username"] = user.get("username", "")
    profile_menu(msg["chat"]["id"], PROFILE_INTRO)


def profile_text(user, p):
    r = find_pilot(user.get("username", "")) if user.get("username") else None
    lines = [f"👤 {col(r, 1) if r else name_of(user)}" + (f" · №{col(r, 0)}" if r else "")]
    if r:
        lines.append(f"Роль: {col(r, 6)}")
    lines.append(f"О себе: {p.get('about') or '—'}")
    if not p.get("car"):
        lines.append("Фото машины ещё нет — нажми «🚗 Фото машины».")
    return "\n".join(lines)


def notify_new_car(uid, user, p):
    chat_id = club_chat()
    if not chat_id:
        return
    who = name_of(user) + (f" @{user['username']}" if user.get("username") else "")
    for admin in admin_ids(chat_id):
        api("sendPhoto", {"chat_id": admin, "photo": p["car"],
                          "caption": f"🚗 {who} поставил(а) новое фото машины в профиль.\n"
                                     "Если машина не в ливрее клуба или фото неуместное — убери его.",
                          "reply_markup": {"inline_keyboard": [[{"text": "🗑 Убрать фото",
                                                                 "callback_data": f"pfdel:{uid}:{p['v']}"}]]}})


def profile_callback(cq):
    action = cq["data"].split(":", 1)[1]
    user, uid = cq["from"], cq["from"]["id"]
    chat_id = (cq.get("message") or {}).get("chat", {}).get("id", uid)
    answer = lambda text="", alert=False: api("answerCallbackQuery", {"callback_query_id": cq["id"], "text": text,
                                                                      "show_alert": alert})
    if not can_have_profile(user):
        answer("Профиль можно оформить, только если ты есть в таблице пилотов клуба.", True)
        return
    p = profiles.setdefault(uid, {})
    p["username"] = user.get("username", "")
    if action in ("car", "again"):
        p["await"] = "car"
        api("sendMessage", {"chat_id": chat_id, "text": CAR_PROMPT})
    elif action == "about":
        p["await"] = "about"
        api("sendMessage", {"chat_id": chat_id, "text": f"📝 Напиши о себе одним сообщением, до {ABOUT_MAX} символов.\n"
                                                        "Например: «Тандем-пилот, люблю Ebisu». Чтобы убрать подпись — пришли «-»."})
    elif action == "view":
        if p.get("car"):
            api("sendPhoto", {"chat_id": chat_id, "photo": p["car"], "caption": profile_text(user, p)})
        else:
            api("sendMessage", {"chat_id": chat_id, "text": profile_text(user, p)})
    elif action == "del":
        had = p.pop("car", None)
        p.pop("pending", None)
        api("sendMessage", {"chat_id": chat_id, "text": "🗑 Фото машины убрано из профиля." if had else "Фото и так нет."})
    elif action == "ok":
        if not p.get("pending"):
            answer("Сначала пришли фото машины.", True)
            return
        p["car"] = p.pop("pending")
        p["v"] = int(time.time())
        p.pop("await", None)
        api("sendMessage", {"chat_id": chat_id, "text": "✅ Готово, фото уже в приложении клуба (вкладка «Пилоты»)."})
        notify_new_car(uid, user, p)
    answer()


def profile_photo_message(msg):
    # Фото в личке бота: если человек сейчас ставит фото машины — показываем и спрашиваем «Ставим?»
    uid = msg["from"]["id"]
    p = profiles.get(uid, {})
    if str(p.get("await", "")).startswith("maplogo:"):
        maplogo_photo(msg, p["await"].split(":", 1)[1])
        return
    if p.get("await") == "livery" or str(p.get("await", "")).startswith("lv_ep:"):
        livery_photo(msg, p)
        return
    if p.get("await") != "car":
        reply(msg, "Чтобы поставить фото машины в профиль, набери /myprofile и нажми «🚗 Фото машины».")
        return
    p["pending"] = duel_image(msg)[0]
    p.pop("await", None)
    api("sendPhoto", {"chat_id": msg["chat"]["id"], "photo": p["pending"],
                      "caption": "Ставим это фото? Напоминаю: машина должна быть в ливрее клуба KHR.",
                      "reply_markup": {"inline_keyboard": [[{"text": "✅ Ставим", "callback_data": "pf:ok"},
                                                            {"text": "🔄 Другое фото", "callback_data": "pf:again"}]]}})


def profile_text_message(msg):
    # Текст в личке бота, когда человек пишет «о себе». Возвращает True, если сообщение забрали.
    p = profiles.get(msg["from"]["id"], {})
    text = msg.get("text", "").strip()
    wait = str(p.get("await", ""))
    if (wait in ("lv_title", "lv_for") or wait.startswith(("lv_et:", "lv_ef:"))) and text and not text.startswith("/"):
        livery_text(msg, p)
        return True
    if wait.startswith("uts_") and text and not text.startswith("/"):
        uts_draft_text_input(msg, p)
        return True
    if wait.startswith("order:") and text and not text.startswith("/"):
        order_text(msg, p)
        return True
    if p.get("await") != "about" or text.startswith("/"):
        p.pop("await", None) if text.startswith("/") else None
        return False
    p.pop("await", None)
    if text == "-":
        p.pop("about", None)
        reply(msg, "Подпись убрана.")
    else:
        p["about"] = text[:ABOUT_MAX]
        reply(msg, "✅ Подпись сохранена" + (f" (обрезал до {ABOUT_MAX} символов)" if len(text) > ABOUT_MAX else "")
              + ". Посмотреть: /myprofile → «👁 Мой профиль».")
    return True


def photo_remove_callback(cq):
    _, uid, v = cq["data"].split(":")
    uid, v = int(uid), int(v)
    clicker = cq["from"]
    chat_id = club_chat()
    answer = lambda text, alert=False: api("answerCallbackQuery", {"callback_query_id": cq["id"], "text": text,
                                                                   "show_alert": alert})
    if not chat_id or get_level(chat_id, clicker["id"]) < 2:
        answer("Убирать фото могут только администраторы.", True)
        return
    p = profiles.get(uid, {})
    msg = cq.get("message") or {}
    if p.get("v") != v or not p.get("car"):
        answer("Это фото уже заменили или убрали.")
        return
    p.pop("car", None)
    api("editMessageCaption", {"chat_id": msg.get("chat", {}).get("id"), "message_id": msg.get("message_id"),
                               "caption": (msg.get("caption") or "") + f"\n\n❌ Убрано ({name_of(clicker)})",
                               "reply_markup": {"inline_keyboard": []}})
    api("sendMessage", {"chat_id": uid, "text": "Администратор убрал фото твоей машины из профиля.\n"
                                                "Поставь фото машины в ливрее клуба KHR: /myprofile"})
    answer("Фото убрано")


def profile_photo(username):
    # Байты фото машины для сайта. Токен бота наружу не попадает: сайт берёт фото у бота, а не у Телеграма.
    uname = (username or "").lower().lstrip("@")
    p = next((p for p in profiles.values() if (p.get("username") or "").lower() == uname and p.get("car")), None)
    if not p:
        return None
    cached = _photo_cache.get(uname)
    if cached and cached[0] == p["car"]:
        return cached[1]
    data = tg_file(p["car"])
    if data:
        _photo_cache[uname] = (p["car"], data)
        while len(_photo_cache) > 80:
            _photo_cache.pop(next(iter(_photo_cache)))
    return data


# ---------- Мастерская ливрей: портфолио ливрейщиков клуба ----------
# Ливрейщика назначает владелец (/designer @ник) — или он сам им становится, если в таблице пилотов
# или в должностях бота (/setrole) у него есть «ливре…». Ливреи он загружает в ЛС бота (/livery),
# а приложение показывает их на странице «Мастерская ливрей». Хранятся в профиле: profiles[uid]["liveries"],
# у каждой: id, fs (до 5 фото), t (описание), for (для кого), d (дата), likes ({id пилота: дата лайка}).
LIVERY_MAX = 40       # сколько ливрей в портфолио одного ливрейщика
LIVERY_PHOTOS = 5     # сколько фото в одной ливрее
LIVERY_TITLE_MAX = 150
LIVERY_FOR_MAX = 40
ORDER_TEXT_MAX = 600
_livery_cache = {}    # (id ливреи, номер фото) → (file_id, байты)


def is_designer(user):
    chat_id = club_chat()
    if not chat_id or not user:
        return False
    if user["id"] in settings.get(chat_id, {}).get("designers", []):
        return True
    own = roles.get(chat_id, {}).get(user["id"], [])
    r = find_pilot(user.get("username") or "") if user.get("username") else None
    return any("ливре" in x.lower() for x in own + ([col(r, 6)] if r else []))


def club_user_by_name(name):
    # Участник клуба по @нику в Телеграме (бот знает тех, кто писал в чат клуба)
    uname = name.lower().lstrip("@")
    return next((dict(u) for u in members.get(club_chat(), {}).values() if (u.get("username") or "").lower() == uname), None)


def owner_ids(chat_id):
    ids = {m["user"]["id"] for m in get_admins(chat_id) if m.get("status") == "creator"}
    return ids | {uid for uid, lvl in staff.get(chat_id, {}).items() if lvl >= 3}


def lv_photos(it):
    return it.get("fs") or ([it["f"]] if it.get("f") else [])


def find_livery(lid):
    # (id ливрейщика, ливрея) по id ливреи
    for uid, p in profiles.items():
        for it in p.get("liveries", []):
            if str(it["id"]) == str(lid):
                return uid, it
    return None, None


def designer_name(uid):
    p = profiles.get(uid, {})
    u = members.get(club_chat(), {}).get(uid, {})
    r = find_pilot(u.get("username") or p.get("username") or "") if (u.get("username") or p.get("username")) else None
    return col(r, 1) if r else u.get("first_name", "ливрейщик")


def livery_caption(uid, it):
    return (f"🎨 «{it.get('t') or 'Без названия'}»\nАвтор: {designer_name(uid)}"
            + (f"\nДля пилота: {it['for']}" if it.get("for") else ""))


def cmd_designer(msg, args):
    # /designer — список, /designer @ник — назначить ливрейщиком, /designer @ник off — снять
    if not is_owner(msg["from"]["id"]):
        reply(msg, "Назначать ливрейщиков может только владелец клуба.")
        return
    conf = settings.setdefault(club_chat(), {})
    lst = conf.setdefault("designers", [])
    if not args:
        names = [name_of(members.get(club_chat(), {}).get(uid, {"first_name": str(uid)})) for uid in lst]
        reply(msg, "🎨 Ливрейщики: " + (", ".join(names) if names else "пока никого") +
              "\nНазначить: /designer @ник · снять: /designer @ник off\n"
              "Ещё ливрейщиком считается тот, у кого в таблице пилотов или в /setrole есть должность «Дизайнер ливрей».")
        return
    target = (msg.get("reply_to_message") or {}).get("from") or club_user_by_name(args[0])
    if not target:
        reply(msg, f"Не нашёл {args[0]} в чате клуба. Человек должен хоть раз написать в чат, тогда бот его узнает.")
        return
    who = name_of(target) + (f" @{target['username']}" if target.get("username") else "")
    if args[-1].lower() in ("off", "выкл", "снять"):
        if target["id"] in lst:
            lst.remove(target["id"])
        reply(msg, f"Снял с {who} статус ливрейщика. Его ливреи больше не показываются в приложении"
              + (" (но должность «ливре…» в таблице или /setrole всё ещё даёт доступ)." if is_designer(target) else "."))
        return
    if target["id"] not in lst:
        lst.append(target["id"])
    reply(msg, f"🎨 {who} теперь ливрейщик клуба. Загружать ливреи — в ЛС бота командой /livery.")
    api("sendMessage", {"chat_id": target["id"],
                        "text": "🎨 Тебя назначили ливрейщиком клуба KHR!\nТеперь у тебя есть своя страница в приложении клуба "
                                "(«Мастерская ливрей»). Загружай туда свои работы: /livery"})


def cmd_liveryhere(msg, args, my):
    # Тема чата, куда бот публикует новые ливреи и «Ливрею недели» (без неё — в общий чат)
    conf = settings.setdefault(msg["chat"]["id"], {})
    if args and args[0].lower() in ("off", "выкл"):
        conf.pop("livery_thread", None)
        reply(msg, "Ливреи теперь публикуются в общий чат клуба.")
        return
    conf["livery_thread"] = msg.get("message_thread_id", 0)
    log_action(msg, None, "выбрал тему для ливрей")
    reply(msg, "🎨 Новые ливреи и «Ливрея недели» будут публиковаться в этой теме.")


def post_livery(chat_id, uid, it, head):
    fs = lv_photos(it)
    thread = settings.get(chat_id, {}).get("livery_thread")
    caption = f"{head}\n\n{livery_caption(uid, it)}\n\nВсе работы — в приложении клуба: «Пилоты» → «🎨 Мастерская ливрей»."
    if len(fs) > 1:
        payload = {"chat_id": chat_id, "media": [dict({"type": "photo", "media": f}, **({"caption": caption} if i == 0 else {}))
                                                 for i, f in enumerate(fs)]}
        method = "sendMediaGroup"
    else:
        payload, method = {"chat_id": chat_id, "photo": fs[0], "caption": caption}, "sendPhoto"
    if thread:
        payload["message_thread_id"] = thread
    return api(method, payload).get("ok")


def livery_menu(chat_id, text):
    kb = [[{"text": "➕ Новая ливрея", "callback_data": "lv:add"}],
          [{"text": "📂 Мои ливреи", "callback_data": "lv:list"}]]
    api("sendMessage", {"chat_id": chat_id, "text": text, "reply_markup": {"inline_keyboard": kb}})


def livery_cmd(msg):
    user = msg["from"]
    if msg["chat"]["type"] != "private":
        reply(msg, f"Ливреи загружаются в личке бота: напиши @{BOT_USERNAME} команду /livery.")
        return
    if not is_designer(user):
        reply(msg, "Загружать ливреи могут только ливрейщики клуба. Их назначает владелец.")
        return
    p = profiles.setdefault(user["id"], {})
    p.pop("await", None)
    p["username"] = user.get("username", "")
    n = len(p.get("liveries", []))
    livery_menu(msg["chat"]["id"], f"🎨 Твоя мастерская ливрей — {n} из {LIVERY_MAX} работ.\n"
                                   "Всё, что сюда загрузишь, видно в приложении клуба на странице «Мастерская ливрей».")


def item_menu(chat_id, it):
    i = it["id"]
    kb = [[{"text": "✏️ Описание", "callback_data": f"lv:et:{i}"}, {"text": "👤 Для кого", "callback_data": f"lv:ef:{i}"}],
          [{"text": "🖼 Заменить фото", "callback_data": f"lv:ep:{i}"}, {"text": "📢 В чат клуба", "callback_data": f"lv:pub:{i}"}],
          [{"text": "🗑 Удалить", "callback_data": f"lv:del:{i}"}]]
    likes = len(it.get("likes", {}))
    api("sendPhoto", {"chat_id": chat_id, "photo": lv_photos(it)[0], "reply_markup": {"inline_keyboard": kb},
                      "caption": f"«{it.get('t') or 'Без названия'}»\nДля кого: {it.get('for') or '—'}\n"
                                 f"Фото: {len(lv_photos(it))} · 🔥 {likes}"})


def livery_callback(cq):
    user, uid = cq["from"], cq["from"]["id"]
    chat_id = (cq.get("message") or {}).get("chat", {}).get("id", uid)
    answer = lambda text="", alert=False: api("answerCallbackQuery", {"callback_query_id": cq["id"], "text": text,
                                                                      "show_alert": alert})
    if not is_designer(user):
        answer("Это могут только ливрейщики клуба.", True)
        return
    p = profiles.setdefault(uid, {})
    p["username"] = user.get("username", "")
    items = p.setdefault("liveries", [])
    parts = cq["data"].split(":")
    act = parts[1]
    it = next((x for x in items if len(parts) > 2 and str(x["id"]) == parts[2]), None)
    if len(parts) > 2 and act not in ("epd",) and not it:
        answer("Эту ливрею уже удалили.", True)
        return
    if act == "add":
        if len(items) >= LIVERY_MAX:
            answer(f"В портфолио уже {LIVERY_MAX} ливрей — сначала удали старые в «📂 Мои ливреи».", True)
            return
        p["await"], p["lv_new"] = "livery", {"fs": []}
        api("sendMessage", {"chat_id": chat_id, "text": f"🖼 Пришли до {LIVERY_PHOTOS} скринов ливреи — можно альбомом "
                                                        "(лучше файлами, без сжатия). Потом нажми «✅ Готово»."})
    elif act in ("done", "epd"):
        new = p.get("lv_new") or {}
        if not new.get("fs"):
            answer("Сначала пришли хотя бы одно фото.", True)
            return
        if act == "done":
            p["await"] = "lv_title"
            api("sendMessage", {"chat_id": chat_id, "text": f"📝 Теперь опиши ливрею одним сообщением (до {LIVERY_TITLE_MAX} "
                                                            "символов): название, машина, идея — что хочешь рассказать."})
        else:
            it = next((x for x in items if str(x["id"]) == parts[2]), None)
            if not it:
                answer("Эту ливрею уже удалили.", True)
                return
            it["fs"] = new["fs"]
            it.pop("f", None)
            p.pop("lv_new", None)
            p.pop("await", None)
            api("sendMessage", {"chat_id": chat_id, "text": f"✅ Фото заменены ({len(it['fs'])} шт.)."})
    elif act == "list":
        if not items:
            api("sendMessage", {"chat_id": chat_id, "text": "Пока пусто. Нажми «➕ Новая ливрея» в /livery."})
        else:
            kb = [[{"text": f"🎨 {(x.get('t') or 'Без названия')[:40]}", "callback_data": f"lv:item:{x['id']}"}] for x in items[-30:]]
            api("sendMessage", {"chat_id": chat_id, "reply_markup": {"inline_keyboard": kb},
                                "text": f"📂 Твои ливреи: {len(items)}. Выбери ливрею — её можно изменить или удалить."})
    elif act == "item":
        item_menu(chat_id, it)
    elif act == "et":
        p["await"] = f"lv_et:{it['id']}"
        api("sendMessage", {"chat_id": chat_id, "text": f"✏️ Пришли новое описание (до {LIVERY_TITLE_MAX} символов)."})
    elif act == "ef":
        p["await"] = f"lv_ef:{it['id']}"
        api("sendMessage", {"chat_id": chat_id, "text": "👤 Для кого ливрея? Напиши ник пилота или «-», если ни для кого."})
    elif act == "ep":
        p["await"], p["lv_new"] = f"lv_ep:{it['id']}", {"fs": []}
        api("sendMessage", {"chat_id": chat_id, "text": f"🖼 Пришли новые фото (до {LIVERY_PHOTOS}), потом нажми «✅ Готово». "
                                                        "Старые фото заменятся."})
    elif act == "pub":
        if it.get("pub"):
            answer("Эта ливрея уже есть в чате клуба.", True)
            return
        if not club_chat() or not post_livery(club_chat(), uid, it, "🎨 Новая ливрея в мастерской клуба!"):
            answer("Не получилось опубликовать — бот должен быть в чате клуба.", True)
            return
        it["pub"] = 1
        answer("Опубликовано в чате клуба ✅")
        return
    elif act == "del":
        p["liveries"] = [x for x in items if x is not it]
        api("sendMessage", {"chat_id": chat_id, "text": "🗑 Ливрея удалена из портфолио."})
    elif act == "skip" and p.get("await") == "lv_for":
        livery_save(chat_id, uid, p, "")
    answer()


def livery_photo(msg, p):
    # Фото ливреи (их может прийти альбомом сразу несколько): копим и показываем одно сообщение со счётчиком
    new = p.setdefault("lv_new", {"fs": []})
    fs = new.setdefault("fs", [])
    if len(fs) >= LIVERY_PHOTOS:
        return
    fs.append(duel_image(msg)[0])
    done = f"lv:epd:{p['await'].split(':')[1]}" if p["await"].startswith("lv_ep:") else "lv:done"
    text = (f"🖼 Фото: {len(fs)} из {LIVERY_PHOTOS}. " + ("Можно прислать ещё. " if len(fs) < LIVERY_PHOTOS else "")
            + "Когда всё — нажми «✅ Готово».")
    markup = {"inline_keyboard": [[{"text": "✅ Готово", "callback_data": done}]]}
    if new.get("msg"):
        api("editMessageText", {"chat_id": msg["chat"]["id"], "message_id": new["msg"], "text": text, "reply_markup": markup})
    else:
        new["msg"] = api("sendMessage", {"chat_id": msg["chat"]["id"], "text": text, "reply_markup": markup}).get("result", {}).get("message_id")


def resolve_pilot(who):
    # «Для кого»: если это пилот из таблицы — его ник в игре, чтобы приложение открыло профиль
    who = (who or "").strip().lstrip("@")
    if who in ("-", "—"):
        return ""
    r = next((r for r in get_pilots() if who and who.lower() in (col(r, 1).lower(), col(r, 2).lower().lstrip("@"))), None)
    return col(r, 1) if r else who[:LIVERY_FOR_MAX]


def livery_text(msg, p):
    text = msg.get("text", "").strip()
    kind = p["await"]
    if kind == "lv_title":
        p["lv_new"]["t"] = text[:LIVERY_TITLE_MAX]
        p["await"] = "lv_for"
        api("sendMessage", {"chat_id": msg["chat"]["id"],
                            "text": "👤 Для кого эта ливрея? Напиши ник пилота (например, Fatcat или @fatcat).",
                            "reply_markup": {"inline_keyboard": [[{"text": "Ни для кого / для клуба", "callback_data": "lv:skip"}]]}})
        return
    if kind == "lv_for":
        livery_save(msg["chat"]["id"], msg["from"]["id"], p, text)
        return
    it = next((x for x in p.get("liveries", []) if str(x["id"]) == kind.split(":")[1]), None)
    p.pop("await", None)
    if not it:
        reply(msg, "Эту ливрею уже удалили.")
    elif kind.startswith("lv_et:"):
        it["t"] = text[:LIVERY_TITLE_MAX]
        reply(msg, "✅ Описание обновлено.")
    else:
        it["for"] = resolve_pilot(text)
        reply(msg, "✅ Сохранено." + (f" Ливрея для: {it['for']}" if it["for"] else " Ливрея ни для кого."))
        notify_livery_pilot(msg["from"]["id"], it)


def notify_livery_pilot(uid, it):
    # Пилоту, для которого нарисована ливрея, — сообщение с кнопкой «поставить в профиль»
    r = next((r for r in get_pilots() if it.get("for") and col(r, 1).lower() == it["for"].lower()), None)
    pilot = club_user_by_name(col(r, 2)) if r and col(r, 2) != "—" else None
    if not pilot or pilot["id"] == uid:
        return
    api("sendPhoto", {"chat_id": pilot["id"], "photo": lv_photos(it)[0],
                      "caption": f"🎨 {designer_name(uid)} нарисовал(а) для тебя ливрею «{it.get('t') or 'Без названия'}»!\n"
                                 "Её видно в приложении клуба в «Мастерской ливрей». Можно поставить это фото в свой профиль:",
                      "reply_markup": {"inline_keyboard": [[{"text": "🚗 Поставить в мой профиль",
                                                             "callback_data": f"lvcar:{uid}:{it['id']}"}]]}})


def livery_save(chat_id, uid, p, for_whom):
    new = p.pop("lv_new", None)
    p.pop("await", None)
    if not new or not new.get("fs"):
        api("sendMessage", {"chat_id": chat_id, "text": "Что-то пошло не так — начни заново: /livery"})
        return
    it = {"id": int(time.time() * 1000) % 10 ** 12, "fs": new["fs"], "t": new.get("t", ""),
          "for": resolve_pilot(for_whom), "d": msk_today().isoformat()}
    p.setdefault("liveries", []).append(it)
    api("sendMessage", {"chat_id": chat_id, "text": "✅ Ливрея добавлена в портфолио — она уже в приложении клуба.\n"
                                                    "Показать её всем в чате клуба? Ещё одну: /livery",
                        "reply_markup": {"inline_keyboard": [[{"text": "📢 Показать в чате клуба", "callback_data": f"lv:pub:{it['id']}"}]]}})
    notify_livery_pilot(uid, it)
    club = club_chat()
    for owner in (owner_ids(club) if club else set()):
        if owner != uid:
            api("sendPhoto", {"chat_id": owner, "photo": it["fs"][0],
                              "caption": "🆕 Новая ливрея в мастерской.\n" + livery_caption(uid, it)
                                         + "\nЕсли она неуместная — убери её.",
                              "reply_markup": {"inline_keyboard": [[{"text": "🗑 Убрать", "callback_data": f"lvrm:{uid}:{it['id']}"}]]}})


def livery_remove_callback(cq):
    # Владелец убирает чужую ливрею из мастерской
    _, uid, lid = cq["data"].split(":")
    answer = lambda text, alert=False: api("answerCallbackQuery", {"callback_query_id": cq["id"], "text": text, "show_alert": alert})
    if not is_owner(cq["from"]["id"]):
        answer("Убирать ливреи может только владелец клуба.", True)
        return
    p = profiles.get(int(uid), {})
    it = next((x for x in p.get("liveries", []) if str(x["id"]) == lid), None)
    if not it:
        answer("Эту ливрею уже убрали.")
        return
    p["liveries"] = [x for x in p["liveries"] if x is not it]
    msg = cq.get("message") or {}
    api("editMessageCaption", {"chat_id": msg.get("chat", {}).get("id"), "message_id": msg.get("message_id"),
                               "caption": (msg.get("caption") or "") + "\n\n❌ Убрано", "reply_markup": {"inline_keyboard": []}})
    api("sendMessage", {"chat_id": int(uid), "text": f"Владелец убрал ливрею «{it.get('t') or 'Без названия'}» из мастерской."})
    answer("Ливрея убрана")


def livery_car_callback(cq):
    # Пилот ставит ливрею, нарисованную для него, фото машины в профиль
    _, uid, lid = cq["data"].split(":")
    user = cq["from"]
    answer = lambda text, alert=False: api("answerCallbackQuery", {"callback_query_id": cq["id"], "text": text, "show_alert": alert})
    duid, it = find_livery(lid)
    r = find_pilot(user.get("username") or "") if user.get("username") else None
    if not it:
        answer("Эту ливрею уже удалили.", True)
        return
    if not r or col(r, 1).lower() != (it.get("for") or "").lower():
        answer("Поставить в профиль может только пилот, для которого нарисована ливрея.", True)
        return
    p = profiles.setdefault(user["id"], {})
    p.update({"car": lv_photos(it)[0], "v": int(time.time()), "username": user.get("username", "")})
    p.pop("pending", None)
    api("sendMessage", {"chat_id": user["id"], "text": "✅ Готово — эта ливрея теперь фото твоей машины в приложении клуба."})
    notify_new_car(user["id"], user, p)
    answer("Поставлено ✅")


# ---------- Заказы ливрей: пилот описывает, что хочет, бот передаёт ливрейщику ----------
def order_start(msg, tg="", duid=None):
    user = msg["from"]
    club = club_chat()
    if not club or user["id"] not in members.get(club, {}):
        reply(msg, "Заказывать ливреи могут участники клуба (бот должен знать тебя по чату клуба).")
        return
    if tg or duid:
        d = dict(members.get(club, {}).get(duid, {}), id=duid) if duid else club_user_by_name(tg)
        if not d or not is_designer(d):
            reply(msg, "Этот ливрейщик сейчас не принимает заказы. Список ливрейщиков: /order")
            return
        if d["id"] == user["id"]:
            reply(msg, "Себе заказать ливрею нельзя 🙂")
            return
        profiles.setdefault(user["id"], {})["await"] = f"order:{d['id']}"
        reply(msg, f"🎨 Заказ ливреи у {designer_name(d['id'])}.\nОпиши одним сообщением: какая машина, цвета, идея, "
                   f"к какому сроку (до {ORDER_TEXT_MAX} символов). Передумал — /cancel")
        return
    des = [u for u in members.get(club, {}).values() if is_designer(u) and u["id"] != user["id"]]
    if not des:
        reply(msg, "В клубе пока нет ливрейщиков.")
        return
    api("sendMessage", {"chat_id": msg["chat"]["id"], "text": "🎨 У кого заказать ливрею?",
                        "reply_markup": {"inline_keyboard": [[{"text": designer_name(u["id"]), "callback_data": f"lvo:to:{u['id']}"}]
                                                             for u in des[:20]]}})


def order_text(msg, p):
    to = int(p.pop("await").split(":")[1])
    text = msg.get("text", "").strip()[:ORDER_TEXT_MAX]
    user = msg["from"]
    orders = settings.setdefault(club_chat(), {}).setdefault("orders", [])
    o = {"id": int(time.time() * 1000) % 10 ** 12, "from": user["id"], "to": to, "text": text, "st": "new", "d": msk_today().isoformat()}
    orders.append(o)
    del orders[:-100]
    who = name_of(user) + (f" @{user['username']}" if user.get("username") else "")
    ok = api("sendMessage", {"chat_id": to, "text": f"📩 Новый заказ ливреи от {who}:\n\n{text}",
                             "reply_markup": {"inline_keyboard": [[{"text": "✅ Беру", "callback_data": f"lvo:ok:{o['id']}"},
                                                                   {"text": "❌ Не могу", "callback_data": f"lvo:no:{o['id']}"}]]}}).get("ok")
    if ok:
        reply(msg, f"✅ Заказ отправлен ливрейщику {designer_name(to)}. Бот напишет, когда он ответит.")
    else:
        u = members.get(club_chat(), {}).get(to, {})
        reply(msg, "Не получилось доставить заказ: ливрейщик ещё не открыл бота."
              + (f" Напиши ему сам: @{u['username']}" if u.get("username") else ""))


def order_callback(cq):
    _, act, oid = cq["data"].split(":")
    user = cq["from"]
    answer = lambda text="", alert=False: api("answerCallbackQuery", {"callback_query_id": cq["id"], "text": text, "show_alert": alert})
    if act == "to":
        cq_msg = {"from": user, "chat": (cq.get("message") or {}).get("chat", {"id": user["id"], "type": "private"})}
        order_start(dict(cq_msg, message_id=0), duid=int(oid))
        answer()
        return
    o = next((x for x in settings.get(club_chat(), {}).get("orders", []) if str(x["id"]) == oid), None)
    if not o or o["to"] != user["id"]:
        answer("Этот заказ не найден.", True)
        return
    if o["st"] != "new":
        answer("На этот заказ ты уже ответил(а).")
        return
    o["st"] = act
    who = name_of(user) + (f" @{user['username']}" if user.get("username") else "")
    api("sendMessage", {"chat_id": o["from"], "text": f"🎨 {who} взял(а) твой заказ ливреи! Обсудите детали в личке."
                        if act == "ok" else f"😔 {who} сейчас не может взять твой заказ ливреи. Попробуй другого ливрейщика: /order"})
    msg = cq.get("message") or {}
    api("editMessageText", {"chat_id": msg.get("chat", {}).get("id"), "message_id": msg.get("message_id"),
                            "text": (msg.get("text") or "") + ("\n\n✅ Ты взял(а) этот заказ" if act == "ok" else "\n\n❌ Отказ"),
                            "reply_markup": {"inline_keyboard": []}})
    answer("Готово")


# ---------- Лайки ливрей из приложения и «Ливрея недели» ----------
def webapp_user(init):
    # Проверка подписи данных Телеграма из мини-приложения: так лайк нельзя подделать
    try:
        pairs = dict(parse_qsl(init or "", keep_blank_values=True))
        got = pairs.pop("hash", "")
        check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
        if not got or not hmac.compare_digest(hmac.new(secret, check.encode(), hashlib.sha256).hexdigest(), got):
            return None
        if time.time() - int(pairs.get("auth_date", 0)) > 7 * 86400:
            return None
        return json.loads(pairs.get("user", "{}")) or None
    except Exception:
        return None


def like_livery(init, lid):
    user = webapp_user(init)
    if not user:
        return {"ok": False, "error": "Лайкать можно только из приложения в Телеграме"}
    duid, it = find_livery(lid)
    if not it:
        return {"ok": False, "error": "Ливрея не найдена"}
    if duid == user["id"]:
        return {"ok": False, "error": "Свои работы лайкать нельзя"}
    likes = it.setdefault("likes", {})
    key = str(user["id"])
    if key in likes:
        likes.pop(key)
    else:
        likes[key] = msk_today().isoformat()
    return {"ok": True, "n": len(likes), "liked": key in likes}


def livery_of_week(chat_id, now):
    # Понедельник: ливрея, собравшая больше всего лайков за прошлую неделю, — в чат клуба
    start, end = week_bounds(now.date(), back=1)
    a, b = start.isoformat(), end.isoformat()
    best = None
    for uid, p in profiles.items():
        for it in p.get("liveries", []):
            n = sum(1 for d in it.get("likes", {}).values() if a <= d <= b)
            if n and (not best or n > best[0]):
                best = (n, uid, it)
    if best:
        post_livery(chat_id, best[1], best[2], f"🏆 Ливрея недели {week_label(start, end)} — 🔥 {best[0]} за неделю!")


def liveries_json():
    chat_id = club_chat()
    out = []
    # Ливрейщики — и те, кто уже что-то загрузил, и назначенные, у кого страница пока пустая
    for uid in set(profiles) | set(members.get(chat_id, {})) | set(settings.get(chat_id, {}).get("designers", [])):
        p = profiles.get(uid, {})
        user = dict(members.get(chat_id, {}).get(uid, {}), id=uid)
        if not user.get("username"):
            user["username"] = p.get("username", "")
        if not is_designer(user):
            continue
        r = find_pilot(user["username"]) if user.get("username") else None
        out.append({"tg": (user.get("username") or "").lower(), "name": col(r, 1) if r else user.get("first_name", ""),
                    "items": [dict({k: it.get(k, "") for k in ("id", "t", "for", "d")}, n=len(lv_photos(it)),
                                   likes=len(it.get("likes", {}))) for it in reversed(p.get("liveries", []))]})
    return sorted(out, key=lambda x: (-len(x["items"]), x["name"].lower()))


def livery_photo_bytes(name):
    # /livery/<id> — первое фото, /livery/<id>-<номер> — остальные
    lid, _, k = str(name).partition("-")
    _, it = find_livery(lid)
    fs = lv_photos(it) if it else []
    k = int(k) if k.isdigit() else 0
    if k >= len(fs):
        return None
    key = (str(it["id"]), k)
    cached = _livery_cache.get(key)
    if cached and cached[0] == fs[k]:
        return cached[1]
    data = tg_file(fs[k])
    if data:
        _livery_cache[key] = (fs[k], data)
        while len(_livery_cache) > 80:
            _livery_cache.pop(next(iter(_livery_cache)))
    return data


# ---------- Мини-УТС: турниры без судей и организаторов ----------
# Любой пилот из таблицы создаёт турнир (/uts в ЛС бота или кнопка в приложении), остальные жмут «Участвую».
# Бот сам строит сетку (на вылет или с лузер-сеткой), зовёт пары, читает победителя по баллам со скрина
# «История заездов» (это обычный отчёт — дуэль идёт и в норму), даёт техпоражения и объявляет чемпиона.
# Турниры: settings[чат]["uts"] = {id: турнир}, история: settings[чат]["uts_hist"].
UTS_SIZES = [4, 8, 16, 32]
UTS_REG_MIN = 30        # «старт сразу»: столько минут ждём набора, потом отмена
UTS_REMIND_MIN = 15     # напоминание в ЛС до старта
UTS_CHECKIN_MIN = 5     # «Ты на месте?» до старта
UTS_PING_MIN = 20       # пара молчит — напоминаем
UTS_TECH_MIN = 30       # пара молчит — техническое поражение
UTS_RR_MAX_MIN = 180    # «каждый с каждым» длится не дольше
UTS_KEEP_DONE_H = 24    # законченный турнир ещё сутки виден в приложении (и можно позвать на реванш)
UTS_HIST = 60
UTS_START = {"now": "как наберёмся", "30": "через 30 мин", "60": "через 1 час", "120": "через 2 часа"}
UTS_SHORT = {"cancel": "отменить", "bye": "автопроходы", "rr": "каждый с каждым"}
UTS_TRACK = {"random": "случайная из популярных", "each": "новая на каждый матч", "fixed": "своя"}


def uts_all():
    return settings.setdefault(club_chat(), {}).setdefault("uts", {})


def uts_name(uid):
    return pilot_name(club_chat(), uid) or str(uid)


def uts_tag(uid):
    u = members.get(club_chat(), {}).get(uid, {})
    return f"@{u['username']}" if u.get("username") else uts_name(uid)


def msk_hm(ts):
    return (datetime.datetime.utcfromtimestamp(ts) + datetime.timedelta(hours=3)).strftime("%H:%M")


def uts_random_track():
    top = [r[0] for r in tracks_top(club_chat())] if club_chat() else []
    return random.choice(top) if top else ""


def uts_post(text, markup=None):
    # Всё о турнирах — в тему отчётов: туда же пилоты кидают скрины матчей
    chat_id = club_chat()
    payload = {"chat_id": chat_id, "text": text}
    if settings.get(chat_id, {}).get("duel_thread"):
        payload["message_thread_id"] = settings[chat_id]["duel_thread"]
    if markup:
        payload["reply_markup"] = markup
    return api("sendMessage", payload).get("result", {}).get("message_id")


def uts_dm(uids, text, markup=None):
    for uid in uids:
        payload = {"chat_id": uid, "text": text}
        if markup:
            payload["reply_markup"] = markup
        api("sendMessage", payload)


# --- сетка ---
def seed_order(size):
    # Классическая расстановка: 1–N, 2–(N-1)… — сильные (первые) номера получают автопроходы, если людей не хватает
    order = [1]
    while len(order) < size:
        m = len(order) * 2 + 1
        order = [x for s in order for x in (s, m - s)]
    return order


def uts_m(t, mid):
    return next((m for m in t["matches"] if m["id"] == mid), None)


def uts_new_match(t, mid, a, b, name, bo=1):
    m = {"id": mid, "a": a, "b": b, "name": name, "pa": None, "pb": None, "w": None, "l": None,
         "done": False, "bo": bo, "sa": 0, "sb": 0, "games": []}
    t["matches"].append(m)
    return m


def uts_round_name(r, k, de):
    if r == k:
        return "Финал верхней сетки" if de else "Финал"
    if r == k - 1:
        return "Полуфинал верхней сетки" if de else "Полуфинал"
    return f"1/{2 ** (k - r)} финала" + (" (верхняя сетка)" if de else "")


def uts_build_elim(t, seeds):
    n = len(seeds)
    size = 2
    while size < n:
        size *= 2
    k = size.bit_length() - 1
    slots = [seeds[i - 1] if i <= n else None for i in seed_order(size)]
    de = t["de"] and size >= 4
    win = lambda m: {"m": m["id"], "t": "w"}
    lose = lambda m: {"m": m["id"], "t": "l"}
    up = [[uts_new_match(t, f"U1-{i + 1}", {"p": slots[2 * i]}, {"p": slots[2 * i + 1]}, uts_round_name(1, k, de))
           for i in range(size // 2)]]
    for r in range(2, k + 1):
        prev = up[-1]
        up.append([uts_new_match(t, f"U{r}-{i + 1}", win(prev[2 * i]), win(prev[2 * i + 1]), uts_round_name(r, k, de))
                   for i in range(len(prev) // 2)])
    if de:
        lr = 1
        low = [uts_new_match(t, f"L1-{i + 1}", lose(up[0][2 * i]), lose(up[0][2 * i + 1]), "Нижняя сетка")
               for i in range(len(up[0]) // 2)]
        for j in range(2, k + 1):
            lr += 1
            uj = up[j - 1]
            # Проигравших сверху ставим «наоборот», чтобы пары из одной половины сетки не встречались сразу снова
            low = [uts_new_match(t, f"L{lr}-{i + 1}", win(low[i]), lose(uj[len(uj) - 1 - i]), "Нижняя сетка")
                   for i in range(len(low))]
            if j < k:
                lr += 1
                low = [uts_new_match(t, f"L{lr}-{i + 1}", win(low[2 * i]), win(low[2 * i + 1]), "Нижняя сетка")
                       for i in range(len(low) // 2)]
        low[0]["name"] = "Финал нижней сетки"
        uts_new_match(t, "GF", win(up[-1][0]), win(low[0]), "Гранд-финал", bo=t["bo"])
        t["final"], t["third"] = "GF", low[0]["id"]
    else:
        up[-1][0]["bo"] = t["bo"]
        t["final"] = up[-1][0]["id"]
        if k >= 2:
            uts_new_match(t, "B3", lose(up[-2][0]), lose(up[-2][1]), "Матч за 3-е место")
            t["third"] = "B3"


def uts_build_rr(t, seeds):
    for i in range(len(seeds)):
        for j in range(i + 1, len(seeds)):
            uts_new_match(t, f"R{i + 1}-{j + 1}", {"p": seeds[i]}, {"p": seeds[j]}, "Каждый с каждым")


def uts_slot(t, s):
    # (готов ли слот, кто в нём): игрок, победитель или проигравший другого матча
    if "p" in s:
        return True, s["p"]
    m = uts_m(t, s["m"])
    if not m or not m["done"]:
        return False, None
    return True, m["w"] if s["t"] == "w" else m["l"]


def uts_standings(t):
    # «Каждый с каждым»: победы, потом разница баллов
    table = {uid: [0, 0.0] for uid in t["seeds"]}
    for m in t["matches"]:
        if not m["id"].startswith("R"):
            continue
        for g in m["games"]:
            w, a, b = g
            if w in table:
                table[w][0] += 1
            if m["pa"] in table:
                table[m["pa"]][1] += a - b
            if m["pb"] in table:
                table[m["pb"]][1] += b - a
    return sorted(table, key=lambda u: (-table[u][0], -table[u][1], t["seeds"].index(u))), table


def uts_advance(t):
    # Протаскиваем по сетке автопроходы и открываем матчи, где оба соперника уже известны
    changed = True
    while changed:
        changed = False
        for m in t["matches"]:
            if m["done"]:
                continue
            ra, pa = uts_slot(t, m["a"])
            rb, pb = uts_slot(t, m["b"])
            m["pa"], m["pb"] = pa, pb
            if ra and rb and (pa is None or pb is None):
                m.update(done=True, w=pa if pb is None else pb, l=None, bye=True)
                changed = True
            elif ra and rb and not m.get("open"):
                uts_open(t, m, quiet=m["id"].startswith("R"))
    if t.get("fmt") == "rr" and not t.get("final"):
        if all(m["done"] for m in t["matches"]):
            order, _ = uts_standings(t)
            if len(order) >= 4:
                uts_new_match(t, "F", {"p": order[0]}, {"p": order[1]}, "Финал", bo=t["bo"])
                t["final"] = "F"
                t["rr_third"] = order[2]
                uts_advance(t)
                return
            t["final"] = "-"
    final = uts_m(t, t.get("final", ""))
    if t["state"] == "run" and (t.get("final") == "-" or (final and final["done"])):
        uts_finish(t)


def uts_open(t, m, quiet=False):
    m["open"] = time.time()
    m["track"] = uts_random_track() if t["track_mode"] == "each" else t.get("track", "")
    if quiet:  # «каждый с каждым»: все пары открываются сразу, список — одним сообщением при старте
        return
    bo = f" · до {(m['bo'] + 1) // 2} побед" if m["bo"] > 1 else ""
    m["msg"] = uts_post(f"⚔️ {t['title']} · {m['name']}{bo}\n{uts_tag(m['pa'])} vs {uts_tag(m['pb'])}"
                        + (f"\nТрасса: {m['track']}" if m.get("track") else "")
                        + "\n\nКатайте дуэль и киньте сюда скрин «История заездов» — победителя бот определит по баллам. "
                        f"На матч {UTS_TECH_MIN} мин.",
                        {"inline_keyboard": [[{"text": "🏳 Сдаться", "callback_data": f"ut:s:{t['id']}:{m['id']}"}]]})


def uts_result(t, m, winner, note=""):
    m.update(done=True, w=winner, l=m["pb"] if winner == m["pa"] else m["pa"])
    score = f" ({m['sa']}:{m['sb']})" if m["bo"] > 1 else ""
    uts_post(f"✅ {t['title']} · {m['name']}: побеждает {uts_name(winner)}{score}{note}")
    uts_advance(t)


def uts_game(t, m, winner, a, b):
    # Один заезд матча: a — баллы первого в паре, b — второго
    m["games"].append([winner, a, b])
    if winner == m["pa"]:
        m["sa"] += 1
    else:
        m["sb"] += 1
    need = (m["bo"] + 1) // 2
    if max(m["sa"], m["sb"]) >= need:
        uts_result(t, m, winner, f" — {a:g} / {b:g}" if m["bo"] == 1 else "")
    else:
        uts_post(f"🔁 {m['name']}: {uts_name(m['pa'])} {m['sa']} : {m['sb']} {uts_name(m['pb'])} — следующий заезд!")


def uts_on_report(chat_id, uid, opponent, items):
    # Новый скрин в теме отчётов: если это матч идущего турнира — засчитываем заезды по баллам
    if chat_id != club_chat():
        return
    opp = uid_by_game_name(chat_id, re.sub(r"^\s*vs\.?\s*", "", opponent or "", flags=re.I).strip())
    if not opp:
        return
    for t in uts_all().values():
        if t["state"] != "run":
            continue
        m = next((m for m in t["matches"] if not m["done"] and m.get("open") and {m["pa"], m["pb"]} == {uid, opp}), None)
        if not m:
            continue
        for it in items:
            if m["done"] or it[4] != "new":
                continue
            sc = score_pair(it[3])
            if not sc:
                continue
            if sc[0] == sc[1]:
                uts_post(f"🤝 {m['name']}: ничья по баллам ({sc[0]:g} / {sc[1]:g}) — переезд!")
                continue
            me_first = m["pa"] == uid
            a, b = (sc[0], sc[1]) if me_first else (sc[1], sc[0])
            uts_game(t, m, m["pa"] if a > b else m["pb"], a, b)
        return


def uts_finish(t):
    t["state"] = "done"
    t["ended"] = time.time()
    if t.get("fmt") == "rr" and t.get("final") == "-":
        order, _ = uts_standings(t)
        places = order[:3]
    else:
        f = uts_m(t, t["final"])
        third = uts_m(t, t.get("third", ""))
        places = [f["w"], f["l"]] + ([third["l"] if t.get("de") and third["id"].startswith("L") else third["w"]] if third else
                                     ([t["rr_third"]] if t.get("rr_third") else []))
    t["places"] = [p for p in places if p]
    medals = ["🥇", "🥈", "🥉"]
    uts_post(f"🏆 {t['title']} завершён!\n\n" + "\n".join(f"{medals[i]} {uts_name(p)}" for i, p in enumerate(t["places"]))
             + "\n\nСетка — в приложении клуба: «Пилоты» → «🏁 Турниры».",
             {"inline_keyboard": [[{"text": "🔁 Реванш тем же составом", "callback_data": f"ut:r:{t['id']}"}]]})
    hist = settings.setdefault(club_chat(), {}).setdefault("uts_hist", [])
    hist.append({"id": t["id"], "title": t["title"], "d": msk_today().isoformat(), "n": len(t["seeds"]), "places": t["places"]})
    del hist[:-UTS_HIST]


def uts_begin(t):
    seeds = t["players"][:]
    random.shuffle(seeds)
    t.update(seeds=seeds, state="run", began=time.time(), matches=[])
    t["fmt"] = "rr" if len(seeds) < t["size"] and t["short"] == "rr" else "elim"
    if t["fmt"] == "rr":
        uts_build_rr(t, seeds)
    else:
        uts_build_elim(t, seeds)
    if t.get("msg"):
        api("editMessageText", {"chat_id": club_chat(), "message_id": t["msg"], "text": uts_reg_text(t) + "\n\n▶️ Турнир начался!",
                                "reply_markup": {"inline_keyboard": []}})
    fmt = ("каждый с каждым" + (", потом финал" if len(seeds) >= 4 else "")) if t["fmt"] == "rr" else \
        ("с лузер-сеткой" if t["de"] and len(seeds) > 2 else "на вылет")
    uts_post(f"🏁 {t['title']} стартует! Участников: {len(seeds)}, формат: {fmt}.\n"
             + "Посев: " + ", ".join(f"{i + 1}. {uts_name(u)}" for i, u in enumerate(seeds))
             + "\nПары будут появляться здесь. Сетка — в приложении: «Пилоты» → «🏁 Турниры».")
    if t["fmt"] == "rr":
        uts_post(f"🔄 {t['title']}: каждый катает с каждым, в любом порядке. Пары:\n"
                 + "\n".join(f"• {uts_tag(m['a']['p'])} vs {uts_tag(m['b']['p'])}" for m in t["matches"])
                 + f"\n\nПосле каждой дуэли кидайте скрин сюда. На всё — {UTS_RR_MAX_MIN // 60} ч.")
    uts_advance(t)


def uts_cancel(t, why):
    if t.get("msg"):
        api("deleteMessage", {"chat_id": club_chat(), "message_id": t["msg"]})
    uts_dm(set(t["players"] + t["reserve"]), f"❌ Турнир «{t['title']}» отменён: {why}.")
    uts_all().pop(t["id"], None)


# --- регистрация ---
def uts_when(t):
    if t.get("start"):
        return f"в {msk_hm(t['start'])} МСК"
    return f"как только наберёмся (до {msk_hm(t['deadline'])} МСК)"


def uts_reg_text(t):
    track = t.get("track") or ("новая случайная на каждый матч" if t["track_mode"] == "each" else "любая")
    lines = [f"🏁 {t['title']}", f"Топ-{t['size']} · {'с лузер-сеткой' if t['de'] else 'на вылет'} · финал до {(t['bo'] + 1) // 2} побед",
             f"Трасса: {track}", f"Старт: {uts_when(t)}", f"Организатор: {uts_name(t['by'])}",
             f"Если не наберёмся: {UTS_SHORT[t['short']]}", "", f"Участники {len(t['players'])}/{t['size']}:"]
    lines += [f"{i + 1}. {uts_name(u)}" for i, u in enumerate(t["players"])] or ["пока никого"]
    if t["reserve"]:
        lines.append("Запасные: " + ", ".join(uts_name(u) for u in t["reserve"]))
    return "\n".join(lines)


def uts_reg_kb(t):
    return {"inline_keyboard": [[{"text": "✋ Участвую", "callback_data": f"ut:j:{t['id']}"},
                                 {"text": "🚪 Выйти", "callback_data": f"ut:x:{t['id']}"}],
                                [{"text": "⚙️ Изменить (организатор)", "callback_data": f"ut:e:{t['id']}"}]]}


def uts_refresh(t):
    if t.get("msg") and t["state"] == "reg":
        api("editMessageText", {"chat_id": club_chat(), "message_id": t["msg"], "text": uts_reg_text(t), "reply_markup": uts_reg_kb(t)})


def uts_busy(uid, but=None):
    return any(uid in x["players"] for x in uts_all().values() if x["state"] in ("reg", "run") and x is not but)


def uts_join(t, user):
    uid = user["id"]
    if t["state"] != "reg":
        return "Запись на этот турнир уже закрыта."
    if not can_have_profile(user):
        return "Участвовать могут только пилоты из таблицы клуба."
    if uid in t["players"] or uid in t["reserve"]:
        return "Ты уже записан(а)."
    if uts_busy(uid, t):
        return "Ты уже в другом турнире."
    remember(club_chat(), user)
    if len(t["players"]) < t["size"]:
        t["players"].append(uid)
        res = "✅ Ты в турнире!"
    else:
        t["reserve"].append(uid)
        res = "Мест нет — ты в листе ожидания. Освободится место — займёшь его."
    uts_refresh(t)
    if not t.get("start") and len(t["players"]) >= t["size"]:
        uts_begin(t)
    return res


def uts_leave(t, uid):
    if t["state"] != "reg":
        return "Турнир уже идёт — выйти нельзя (можно сдаться в своём матче)."
    if uid in t["reserve"]:
        t["reserve"].remove(uid)
    elif uid in t["players"]:
        t["players"].remove(uid)
        if t["reserve"]:
            nxt = t["reserve"].pop(0)
            t["players"].append(nxt)
            uts_dm([nxt], f"✅ В турнире «{t['title']}» освободилось место — ты в основном составе!")
    else:
        return "Тебя и так нет в списке."
    uts_refresh(t)
    return "Ты вышел(ла) из турнира."


def uts_create(uid, d, players=None):
    now = time.time()
    if d["start"] == "now":
        start, deadline = None, now + UTS_REG_MIN * 60
    elif d["start"] in UTS_START:
        start = now + int(d["start"]) * 60
        deadline = start
    else:
        start = deadline = uts_parse_time(d["start"])
    tid = str(int(now * 1000) % 10 ** 9)
    t = {"id": tid, "title": d["title"], "by": uid, "size": d["size"], "de": d["de"], "bo": d["bo"], "short": d["short"],
         "track_mode": d["track_mode"], "track": d["track"] if d["track_mode"] == "fixed" else
         (uts_random_track() if d["track_mode"] == "random" else ""), "start": start, "deadline": deadline,
         "created": now, "players": [], "reserve": [], "checkin": [], "state": "reg", "matches": []}
    uts_all()[tid] = t
    for p in players or [uid]:
        if not uts_busy(p, t) and len(t["players"]) < t["size"]:
            t["players"].append(p)
    t["msg"] = uts_post(uts_reg_text(t), uts_reg_kb(t))
    if not start and len(t["players"]) >= t["size"]:
        uts_begin(t)
    return t


def uts_parse_time(text):
    mt = re.match(r"^\s*(\d{1,2})[:.](\d{2})\s*$", text or "")
    if not mt or int(mt.group(1)) > 23 or int(mt.group(2)) > 59:
        return None
    now = msk_now()
    target = now.replace(hour=int(mt.group(1)), minute=int(mt.group(2)), second=0, microsecond=0)
    if target <= now:
        target += datetime.timedelta(days=1)
    return time.time() + (target - now).total_seconds()


# --- мастер создания и изменения турнира (в ЛС бота) ---
def uts_new_draft():
    return {"size": 4, "de": True, "start": "now", "track_mode": "random", "track": "", "bo": 3, "short": "bye",
            "title": "Мини-УТС"}


def uts_draft_text(d):
    when = {"keep": "без изменений"}.get(d["start"]) or UTS_START.get(d["start"]) or f"в {d['start']} МСК"
    track = d["track"] if d["track_mode"] == "fixed" else UTS_TRACK[d["track_mode"]]
    return ("🛠 Изменение турнира" if d.get("edit") else "🏁 Новый мини-УТС") + \
        f"\n\nНазвание: {d['title']}\nРазмер: топ-{d['size']}\nЛузер-сетка: {'да' if d['de'] else 'нет'}\n" \
        f"Старт: {when}\nТрасса: {track}\nФинал: до {(d['bo'] + 1) // 2} побед\nЕсли не наберёмся: {UTS_SHORT[d['short']]}\n\n" \
        "Нажимай на кнопки, чтобы поменять. «Каждый с каждым» — если людей меньше, чем мест, все катаются со всеми."


def uts_draft_kb(d):
    when = {"keep": "как было"}.get(d["start"]) or UTS_START.get(d["start"]) or d["start"]
    rows = [[{"text": f"👥 Топ-{d['size']}", "callback_data": "ut:d:size"},
             {"text": f"🔻 Лузер-сетка: {'да' if d['de'] else 'нет'}", "callback_data": "ut:d:de"}],
            [{"text": f"⏰ {when}", "callback_data": "ut:d:start"}, {"text": "✏️ Своё время", "callback_data": "ut:d:time"}],
            [{"text": f"🗺 {UTS_TRACK[d['track_mode']]}", "callback_data": "ut:d:track"},
             {"text": "✏️ Своя трасса", "callback_data": "ut:d:mytrack"}],
            [{"text": f"🏆 Финал до {(d['bo'] + 1) // 2}", "callback_data": "ut:d:bo"},
             {"text": f"⚠️ Неполный: {UTS_SHORT[d['short']]}", "callback_data": "ut:d:short"}],
            [{"text": "✏️ Название", "callback_data": "ut:d:title"}]]
    if d.get("edit"):
        rows.append([{"text": "👤 Убрать участника", "callback_data": "ut:d:kick"},
                     {"text": "🗑 Удалить турнир", "callback_data": "ut:d:del"}])
    rows.append([{"text": "💾 Сохранить" if d.get("edit") else "✅ Создать", "callback_data": "ut:d:ok"},
                 {"text": "✖️ Закрыть", "callback_data": "ut:d:no"}])
    return {"inline_keyboard": rows}


def uts_show_draft(chat_id, p):
    d = p["uts_draft"]
    if d.get("msg"):
        api("editMessageText", {"chat_id": chat_id, "message_id": d["msg"], "text": uts_draft_text(d), "reply_markup": uts_draft_kb(d)})
    else:
        d["msg"] = api("sendMessage", {"chat_id": chat_id, "text": uts_draft_text(d), "reply_markup": uts_draft_kb(d)}
                       ).get("result", {}).get("message_id")


def uts_cmd(msg):
    user = msg["from"]
    if msg["chat"]["type"] != "private":
        api("sendMessage", {"chat_id": msg["chat"]["id"], "text": "Турнир создаётся в личке бота — нажми кнопку ниже.",
                            **({"message_thread_id": msg["message_thread_id"]} if msg.get("message_thread_id") else {}),
                            "reply_markup": {"inline_keyboard": [[{"text": "🏁 Создать мини-УТС",
                                                                   "url": f"https://t.me/{BOT_USERNAME}?start=uts"}]]}})
        return
    if not club_chat() or not settings.get(club_chat(), {}).get("duel_thread"):
        reply(msg, "Турниры заработают, когда в чате клуба будет включён учёт дуэлей (/duelhere).")
        return
    if not can_have_profile(user):
        reply(msg, "Создавать турниры могут пилоты из таблицы клуба.")
        return
    if uts_busy(user["id"]):
        reply(msg, "Ты уже в турнире — сначала доиграй или выйди из него.")
        return
    p = profiles.setdefault(user["id"], {})
    p.pop("await", None)
    p["uts_draft"] = uts_new_draft()
    uts_show_draft(msg["chat"]["id"], p)


def uts_can_edit(t, uid):
    return t["by"] == uid or get_level(club_chat(), uid) >= 2


def uts_draft_callback(cq, act):
    user, uid = cq["from"], cq["from"]["id"]
    chat_id = (cq.get("message") or {}).get("chat", {}).get("id", uid)
    p = profiles.setdefault(uid, {})
    d = p.get("uts_draft")
    if not d:
        return "Это окно устарело — набери /uts ещё раз."
    t = uts_all().get(d.get("edit", ""))
    if act in ("kick", "del") and not t:
        return "Это можно только у созданного турнира."
    if d.get("edit") and (not t or t["state"] != "reg"):
        p.pop("uts_draft", None)
        return "Турнир уже начался или удалён — менять его нельзя."
    if act == "size":
        d["size"] = UTS_SIZES[(UTS_SIZES.index(d["size"]) + 1) % len(UTS_SIZES)]
    elif act == "de":
        d["de"] = not d["de"]
    elif act == "start":
        opts = (["keep"] if d.get("edit") else []) + list(UTS_START)
        d["start"] = opts[(opts.index(d["start"]) + 1) % len(opts)] if d["start"] in opts else opts[0]
    elif act == "track":
        opts = list(UTS_TRACK)
        d["track_mode"] = opts[(opts.index(d["track_mode"]) + 1) % len(opts)]
        if d["track_mode"] == "fixed" and not d["track"]:
            d["track_mode"] = "random"
    elif act == "bo":
        d["bo"] = 1 if d["bo"] == 3 else 3
    elif act == "short":
        opts = list(UTS_SHORT)
        d["short"] = opts[(opts.index(d["short"]) + 1) % len(opts)]
    elif act in ("time", "mytrack", "title"):
        p["await"] = "uts_" + act
        api("sendMessage", {"chat_id": chat_id, "text": {"time": "⏰ Напиши время старта по Москве, например 22:00.",
                                                         "mytrack": "🗺 Напиши название трассы.",
                                                         "title": "✏️ Напиши название турнира (до 40 символов)."}[act]})
        return ""
    elif act == "kick":
        kb = [[{"text": f"❌ {uts_name(u)}", "callback_data": f"ut:k:{t['id']}:{u}"}] for u in t["players"] + t["reserve"] if u != uid]
        api("sendMessage", {"chat_id": chat_id, "text": "Кого убрать из турнира?" if kb else "Кроме тебя, в турнире пока никого.",
                            **({"reply_markup": {"inline_keyboard": kb}} if kb else {})})
        return ""
    elif act == "del":
        uts_cancel(t, "организатор удалил его")
        p.pop("uts_draft", None)
        api("sendMessage", {"chat_id": chat_id, "text": "🗑 Турнир удалён."})
        return ""
    elif act == "no":
        p.pop("uts_draft", None)
        api("editMessageText", {"chat_id": chat_id, "message_id": d.get("msg"), "text": "Окно закрыто.", "reply_markup": {"inline_keyboard": []}})
        return ""
    elif act == "ok":
        p.pop("uts_draft", None)
        if t:
            uts_apply_edit(t, d)
            api("sendMessage", {"chat_id": chat_id, "text": "💾 Турнир изменён, участникам пришло уведомление."})
        else:
            if uts_busy(uid):
                return "Ты уже в турнире."
            t = uts_create(uid, d)
            api("sendMessage", {"chat_id": chat_id, "text": f"✅ Турнир «{t['title']}» создан и опубликован в чате клуба. "
                                                            "Ты уже в списке. Изменить: кнопка «⚙️ Изменить» под анонсом."})
        return ""
    uts_show_draft(chat_id, p)
    return ""


def uts_draft_text_input(msg, p):
    kind, text = p.pop("await"), msg.get("text", "").strip()
    d = p.get("uts_draft")
    if not d:
        return
    if kind == "uts_time":
        if uts_parse_time(text) is None:
            reply(msg, "Не понял время. Напиши так: 22:00")
            p["await"] = kind
            return
        d["start"] = re.sub(r"\.", ":", text.strip())
    elif kind == "uts_mytrack":
        d["track"], d["track_mode"] = text[:40], "fixed"
    else:
        d["title"] = text[:40]
    d.pop("msg", None)
    uts_show_draft(msg["chat"]["id"], p)


def uts_apply_edit(t, d):
    t.update(title=d["title"], de=d["de"], bo=d["bo"], short=d["short"], track_mode=d["track_mode"])
    if d["track_mode"] == "fixed":
        t["track"] = d["track"]
    elif d["track_mode"] == "random" and not t.get("track"):
        t["track"] = uts_random_track()
    elif d["track_mode"] == "each":
        t["track"] = ""
    if d["start"] != "keep":
        now = time.time()
        if d["start"] == "now":
            t["start"], t["deadline"] = None, now + UTS_REG_MIN * 60
        elif d["start"] in UTS_START:
            t["start"] = t["deadline"] = now + int(d["start"]) * 60
        else:
            t["start"] = t["deadline"] = uts_parse_time(d["start"])
        t.pop("reminded", None)
        t.pop("checking", None)
    t["size"] = d["size"]
    while len(t["players"]) > t["size"]:
        t["reserve"].insert(0, t["players"].pop())
    while len(t["players"]) < t["size"] and t["reserve"]:
        t["players"].append(t["reserve"].pop(0))
    uts_dm([u for u in t["players"] + t["reserve"] if u != t["by"]], f"🛠 Турнир «{t['title']}» изменён:\n\n{uts_reg_text(t)}")
    uts_refresh(t)
    if not t.get("start") and len(t["players"]) >= t["size"]:
        uts_begin(t)


def uts_callback(cq):
    parts = cq["data"].split(":")
    user, uid = cq["from"], cq["from"]["id"]
    answer = lambda text="", alert=False: api("answerCallbackQuery", {"callback_query_id": cq["id"], "text": text, "show_alert": alert})
    act = parts[1]
    if act == "d":
        res = uts_draft_callback(cq, parts[2])
        answer(res, bool(res))
        return
    t = uts_all().get(parts[2]) if len(parts) > 2 else None
    if not t:
        answer("Этого турнира уже нет.", True)
        return
    if act == "j":
        answer(uts_join(t, user), True)
    elif act == "x":
        answer(uts_leave(t, uid), True)
    elif act == "c":
        if uid in t["players"] and uid not in t["checkin"]:
            t["checkin"].append(uid)
        answer("👍 Отлично, ты на месте!", True)
    elif act == "e":
        if not uts_can_edit(t, uid):
            answer("Менять турнир может только организатор.", True)
            return
        if t["state"] != "reg":
            answer("Турнир уже идёт — менять его нельзя.", True)
            return
        p = profiles.setdefault(uid, {})
        p["uts_draft"] = {"edit": t["id"], "size": t["size"], "de": t["de"], "start": "keep", "track_mode": t["track_mode"],
                          "track": t.get("track", ""), "bo": t["bo"], "short": t["short"], "title": t["title"]}
        uts_show_draft(uid, p)
        answer("Настройки турнира — в личке бота.")
    elif act == "k":
        if not uts_can_edit(t, uid) or t["state"] != "reg":
            answer("Нельзя.", True)
            return
        who = int(parts[3])
        uts_leave(t, who)
        uts_dm([who], f"Организатор убрал тебя из турнира «{t['title']}».")
        answer(f"{uts_name(who)} убран(а) из турнира.")
    elif act == "s":
        m = uts_m(t, parts[3])
        if not m or m["done"] or uid not in (m["pa"], m["pb"]):
            answer("Это не твой матч.", True)
            return
        uts_result(t, m, m["pb"] if uid == m["pa"] else m["pa"], f" — {uts_name(uid)} сдался(ась)")
        answer("Ты сдался(ась).")
    elif act == "r":
        if uid not in t.get("seeds", []):
            answer("Реванш может позвать только участник турнира.", True)
            return
        if uts_busy(uid):
            answer("Ты уже в другом турнире.", True)
            return
        d = {"size": t["size"], "de": t["de"], "start": "now", "track_mode": t["track_mode"], "track": t.get("track", ""),
             "bo": t["bo"], "short": t["short"], "title": t["title"] + " · реванш"}
        others = [u for u in t["seeds"] if u != uid]
        nt = uts_create(uid, d, [uid] + others)
        uts_dm([u for u in nt["players"] if u != uid], f"🔁 {uts_name(uid)} позвал(а) на реванш «{t['title']}» — ты уже записан(а). "
                                                       "Не можешь — нажми «Выйти» под анонсом в чате.")
        answer("Реванш создан!", True)
    else:
        answer()


# --- таймеры: напоминания, чек-ин, автоотмена, техпоражения ---
def uts_tick(now):
    if not club_chat():
        return False
    active = False
    for t in list(uts_all().values()):
        active = True
        if t["state"] == "reg":
            start = t.get("start")
            if start and not t.get("reminded") and now >= start - UTS_REMIND_MIN * 60 and start - t["created"] > (UTS_REMIND_MIN + 5) * 60:
                t["reminded"] = 1
                uts_dm(t["players"], f"⏰ Турнир «{t['title']}» начнётся через {UTS_REMIND_MIN} мин ({msk_hm(start)} МСК).")
            if start and not t.get("checking") and now >= start - UTS_CHECKIN_MIN * 60 and start - t["created"] > UTS_CHECKIN_MIN * 120:
                t["checking"] = 1
                uts_dm(t["players"], f"✋ Турнир «{t['title']}» через {UTS_CHECKIN_MIN} мин. Ты на месте? Нажми кнопку — "
                                     "иначе место отдадим запасному.",
                       {"inline_keyboard": [[{"text": "✅ Я на месте", "callback_data": f"ut:c:{t['id']}"}]]})
            if now >= t["deadline"]:
                uts_deadline(t)
        elif t["state"] == "run":
            uts_timers(t, now)
        elif now - t.get("ended", now) > UTS_KEEP_DONE_H * 3600:
            uts_all().pop(t["id"], None)
    return active


def uts_deadline(t):
    if t.get("checking"):
        gone = [u for u in t["players"] if u not in t["checkin"]]
        for u in gone:
            t["players"].remove(u)
        while len(t["players"]) < t["size"] and t["reserve"]:
            t["players"].append(t["reserve"].pop(0))
        if gone:
            uts_dm(gone, f"Ты не отметился(ась) в турнире «{t['title']}» — место отдали другому.")
    n = len(t["players"])
    if n >= t["size"] or (n >= 3 and t["short"] != "cancel"):
        uts_begin(t)
    else:
        uts_cancel(t, f"за отведённое время набралось {n} из {t['size']}")


def uts_timers(t, now):
    for m in t["matches"]:
        if m["done"] or not m.get("open") or m["id"].startswith("R"):
            continue
        if not m.get("pinged") and now - m["open"] > UTS_PING_MIN * 60:
            m["pinged"] = 1
            uts_post(f"⏳ {m['name']}: {uts_tag(m['pa'])} и {uts_tag(m['pb'])}, ждём скрин! Через {UTS_TECH_MIN - UTS_PING_MIN} мин — техническое поражение.")
        elif now - m["open"] > UTS_TECH_MIN * 60:
            # Скрина нет — проходит тот, кто выше в посеве
            winner = min((m["pa"], m["pb"]), key=lambda u: t["seeds"].index(u))
            uts_result(t, m, winner, " — техническая победа (скрина не было)")
            return
    if t.get("fmt") == "rr" and not t.get("final") and now - t["began"] > UTS_RR_MAX_MIN * 60:
        for m in t["matches"]:
            if not m["done"]:
                m.update(done=True, skip=True)
        uts_post(f"⏱ {t['title']}: время кругового этапа вышло, несыгранные матчи не считаются.")
        uts_advance(t)


# --- для приложения ---
def uts_json():
    chat = club_chat()
    if not chat:
        return {"active": [], "hist": [], "trophies": {}}
    tag = lambda u: (members.get(chat, {}).get(u, {}).get("username") or "").lower()
    who = lambda u: {"name": uts_name(u), "tg": tag(u)} if u else None
    active = []
    for t in sorted(uts_all().values(), key=lambda x: x["created"], reverse=True):
        v = {k: t.get(k) for k in ("id", "title", "size", "de", "bo", "short", "track", "track_mode", "start", "deadline", "state", "fmt")}
        v.update(by=uts_name(t["by"]), players=[who(u) for u in t["players"]], reserve=[who(u) for u in t["reserve"]],
                 places=[who(u) for u in t.get("places", [])],
                 matches=[{"id": m["id"], "name": m["name"], "a": who(m["pa"]), "b": who(m["pb"]), "sa": m["sa"], "sb": m["sb"],
                           "done": m["done"], "w": who(m["w"]), "bye": bool(m.get("bye")), "open": bool(m.get("open")),
                           "track": m.get("track", ""), "bo": m["bo"]} for m in t["matches"]])
        if t.get("fmt") == "rr" and t["state"] != "reg":
            order, table = uts_standings(t)
            v["table"] = [[uts_name(u), table[u][0], round(table[u][1], 1)] for u in order]
        active.append(v)
    hist = settings.get(chat, {}).get("uts_hist", [])
    trophies = {}
    for h in hist:
        for i, u in enumerate(h["places"][:3]):
            key = tag(u) or uts_name(u)
            trophies.setdefault(key, [0, 0, 0, uts_name(u)])[i] += 1  # [золото, серебро, бронза, ник в игре]
    return {"active": active, "trophies": trophies,
            "hist": [{"title": h["title"], "d": h["d"], "n": h["n"], "places": [uts_name(u) for u in h["places"]]} for h in reversed(hist[-20:])]}


def uts_week_champion(chat_id, start, end):
    a, b = start.isoformat(), end.isoformat()
    wins = {}
    for h in settings.get(chat_id, {}).get("uts_hist", []):
        if a <= h["d"] <= b and h["places"]:
            wins[h["places"][0]] = wins.get(h["places"][0], 0) + 1
    if not wins:
        return ""
    best = max(wins, key=lambda u: wins[u])
    return f"🏁 Чемпион недели по мини-УТС — {uts_name(best)}: {wins[best]} " + ("победа" if wins[best] == 1 else "победы" if wins[best] < 5 else "побед")


def uts_api(init, tid, act):
    user = webapp_user(init)
    if not user:
        return {"ok": False, "error": "Записаться из приложения можно, только когда оно открыто в Телеграме"}
    t = uts_all().get(str(tid))
    if not t:
        return {"ok": False, "error": "Турнира уже нет"}
    if not user.get("username"):  # бот знает @ник по чату клуба
        user["username"] = members.get(club_chat(), {}).get(user.get("id"), {}).get("username", "")
    res = uts_join(t, user) if act == "join" else uts_leave(t, user["id"])
    return {"ok": True, "text": res}


# ---------- «Ищу дуэль» и вызовы на дуэль ----------
# Всё публикуется в теме отчётов (туда же потом кидают скрин). Хранится в settings[клуб]["dz"].
LFG_MIN = 30     # объявление «ищу дуэль» висит столько минут
CALL_MIN = 120   # вызов ждёт ответа столько минут
CALL_MAX = 3     # сколько вызовов без ответа может висеть у одного пилота


def duel_board():
    b = settings.setdefault(club_chat(), {}).setdefault("dz", {})
    b.setdefault("lfg", {})
    b.setdefault("calls", {})
    return b


def dz_edit(mid, text):
    if mid:
        api("editMessageText", {"chat_id": club_chat(), "message_id": mid, "text": text,
                                "reply_markup": {"inline_keyboard": []}})


def dz_pair(a, b):
    uts_post(f"⚔️ Пара: {uts_tag(a)} vs {uts_tag(b)} — катайте! Скрин «История заездов» потом сюда.")
    uts_dm([a], f"⚔️ {uts_name(b)} готов(а) катать с тобой дуэли! Скрин потом — в тему отчётов клуба.")
    uts_dm([b], f"⚔️ {uts_name(a)} готов(а) катать с тобой дуэли! Скрин потом — в тему отчётов клуба.")


def lfg_start(user):
    # Возвращает (получилось ли, текст для пилота)
    uid = user["id"]
    if not club_chat():
        return False, "Бот ещё не знает тему отчётов клуба."
    if not can_have_profile(user):
        return False, "Искать дуэль через бота могут только пилоты из таблицы клуба."
    remember(club_chat(), user)
    b = duel_board()
    if str(uid) in b["lfg"]:
        return False, "Ты уже ищешь дуэль — объявление висит в теме отчётов."
    other = next((k for k in b["lfg"] if k != str(uid)), None)
    if other:  # кто-то уже ищет — сразу пара
        o = b["lfg"].pop(other)
        dz_edit(o.get("msg"), f"✅ {uts_name(int(other))} нашёл(ла) соперника: {uts_name(uid)}.")
        dz_pair(int(other), uid)
        return True, f"⚔️ Соперник нашёлся сразу: {uts_name(int(other))}! Катайте."
    mid = uts_post(f"🔎 {uts_tag(uid)} ищет дуэль прямо сейчас. Кто готов — жми кнопку.\n"
                   f"(объявление висит {LFG_MIN} мин)",
                   {"inline_keyboard": [[{"text": "⚔️ Я готов(а)", "callback_data": f"dz:go:{uid}"}],
                                        [{"text": "✖️ Уже не ищу", "callback_data": f"dz:x:{uid}"}]]})
    b["lfg"][str(uid)] = {"t": time.time(), "msg": mid}
    return True, (f"🔎 Объявил в теме отчётов, что ты ищешь дуэль. Кто откликнется — бот напишет. "
                  f"Объявление висит {LFG_MIN} мин.")


def lfg_join(user, uid):
    b = duel_board()
    o = b["lfg"].get(str(uid))
    if not o:
        return "Этот пилот уже нашёл соперника или перестал искать."
    if user["id"] == uid:
        return "Это твоё объявление 🙂 Отменить — кнопкой «Уже не ищу»."
    if not can_have_profile(user):
        return "Дуэли в зачёт катают только пилоты из таблицы клуба."
    remember(club_chat(), user)
    b["lfg"].pop(str(uid))
    mine = b["lfg"].pop(str(user["id"]), None)  # если и сам искал — своё объявление тоже закрываем
    if mine:
        dz_edit(mine.get("msg"), f"✅ {uts_name(user['id'])} нашёл(ла) соперника: {uts_name(uid)}.")
    dz_edit(o.get("msg"), f"✅ {uts_name(uid)} нашёл(ла) соперника: {uts_name(user['id'])}.")
    dz_pair(uid, user["id"])
    return "⚔️ Договорились! Катайте, скрин потом — в тему отчётов."


def lfg_stop(user, uid):
    b = duel_board()
    if user["id"] != uid:
        return "Снять объявление может только тот, кто его дал."
    o = b["lfg"].pop(str(uid), None)
    if o:
        dz_edit(o.get("msg"), f"✖️ {uts_name(uid)} уже не ищет дуэль.")
    return "Объявление снято."


def call_start(user, target):
    uid = user["id"]
    if not club_chat():
        return False, "Бот ещё не знает тему отчётов клуба."
    if not can_have_profile(user):
        return False, "Вызывать на дуэль могут только пилоты из таблицы клуба."
    if not target or not can_have_profile(target):
        return False, "Этого пилота нет в таблице клуба (или бот ещё не видел его в чате)."
    if target["id"] == uid:
        return False, "Себя вызвать не получится 🙂"
    remember(club_chat(), user)
    b = duel_board()
    mine = [c for c in b["calls"].values() if c["a"] == uid]
    if any(c["b"] == target["id"] for c in mine):
        return False, f"Ты уже вызвал(а) {uts_name(target['id'])} — ждём ответа."
    if len(mine) >= CALL_MAX:
        return False, f"У тебя уже {CALL_MAX} вызова без ответа — подожди, пока ответят."
    cid = str(int(time.time() * 1000) % 10 ** 10)
    while cid in b["calls"]:
        cid = str(int(cid) + 1)
    kb = {"inline_keyboard": [[{"text": "✅ Принять", "callback_data": f"dz:ok:{cid}"},
                               {"text": "⏳ Не сейчас", "callback_data": f"dz:no:{cid}"}]]}
    mid = uts_post(f"⚔️ {uts_tag(uid)} вызывает {uts_tag(target['id'])} на дуэль!\n"
                   f"Ответить может только {uts_name(target['id'])} (вызов действует {CALL_MIN // 60} ч).", kb)
    b["calls"][cid] = {"a": uid, "b": target["id"], "t": time.time(), "msg": mid}
    uts_dm([target["id"]], f"⚔️ {uts_name(uid)} вызывает тебя на дуэль!", kb)
    return True, f"⚔️ Вызов отправлен: {uts_name(target['id'])} получит его в чате и в ЛС."


def call_answer(cq, cid, ok):
    b = duel_board()
    c = b["calls"].get(cid)
    if not c:
        return "Этот вызов уже неактуален."
    if cq["from"]["id"] != c["b"]:
        return f"Ответить на вызов может только {uts_name(c['b'])}."
    b["calls"].pop(cid)
    a, t = c["a"], c["b"]
    text = (f"✅ {uts_name(t)} принял(а) вызов {uts_name(a)} — катайте! Скрин потом сюда." if ok
            else f"⏳ {uts_name(t)} сейчас не может принять вызов {uts_name(a)}.")
    dz_edit(c.get("msg"), text)
    msg = cq.get("message") or {}
    if (msg.get("chat") or {}).get("id", 0) > 0:  # нажали в ЛС — правим и это сообщение
        api("editMessageText", {"chat_id": msg["chat"]["id"], "message_id": msg["message_id"], "text": text,
                                "reply_markup": {"inline_keyboard": []}})
    uts_dm([a], f"✅ {uts_name(t)} принял(а) твой вызов — катайте!" if ok
           else f"⏳ {uts_name(t)} сейчас не может принять твой вызов. Попробуй позже.")
    return "✅ Принято! Катайте." if ok else "Ок, ответил(а) «не сейчас»."


def dz_callback(cq):
    _, act, arg = (cq["data"].split(":") + ["", ""])[:3]
    if act == "go":
        res = lfg_join(cq["from"], int(arg))
    elif act == "x":
        res = lfg_stop(cq["from"], int(arg))
    elif act in ("ok", "no"):
        res = call_answer(cq, arg, act == "ok")
    else:
        res = ""
    api("answerCallbackQuery", {"callback_query_id": cq["id"], "text": res, "show_alert": bool(res)})


def dz_tick(now):
    b = settings.get(club_chat(), {}).get("dz") if club_chat() else None
    if not b:
        return False
    changed = False
    for k, v in list(b.get("lfg", {}).items()):
        if now - v["t"] > LFG_MIN * 60:
            b["lfg"].pop(k)
            dz_edit(v.get("msg"), f"⌛ {uts_name(int(k))} искал(а) дуэль, но никто не откликнулся.")
            changed = True
    for k, c in list(b.get("calls", {}).items()):
        if now - c["t"] > CALL_MIN * 60:
            b["calls"].pop(k)
            dz_edit(c.get("msg"), f"⌛ Вызов {uts_name(c['a'])} → {uts_name(c['b'])} остался без ответа.")
            changed = True
    return changed


def dz_command(msg, cmd, args):
    # /lfg или «ищу дуэль» — найти соперника; /call @ник (или ответом) — вызвать на дуэль. Работает и в ЛС бота.
    is_group = msg["chat"]["type"] != "private"
    if cmd == "lfg":
        ok, text = lfg_start(msg["from"])
        if not is_group or not ok:
            reply(msg, text)
        return
    target = (msg.get("reply_to_message") or {}).get("from")
    if not target and args:
        target = club_user_by_name(args[0])
    if not target:
        reply(msg, "Кого вызываем? Напиши /call @ник или ответь командой на сообщение пилота.")
        return
    ok, text = call_start(msg["from"], target)
    if not is_group or not ok:
        reply(msg, text)


def duel_api(init, act, to=""):
    user = webapp_user(init)
    if not user:
        return {"ok": False, "error": "Это работает, только когда приложение открыто в Телеграме"}
    if not user.get("username"):
        user["username"] = members.get(club_chat(), {}).get(user.get("id"), {}).get("username", "")
    if act == "lfg":
        ok, text = lfg_start(user)
    elif act == "call":
        ok, text = call_start(user, club_user_by_name(to or ""))
    else:
        return {"ok": False, "error": "неизвестное действие"}
    return {"ok": True, "text": text, "done": ok}


# ---------- Рейтинг Эло ----------
# Считается по дуэлям, которые идут в норму, по порядку дат. Дуэль между двумя пилотами клуба записана у обоих —
# считаем её один раз. Хранится только то, что бот помнит (10 недель).
ELO_START, ELO_K = 1000, 24


def elo_games(chat_id, names, until=None):
    games = {}  # (дата, пилот A, пилот B, счёт A, счёт B) → [результат A, сколько раз]
    disp = {}
    for uid in club_uids(chat_id):
        me = pilot_name(chat_id, uid).strip()
        if not me or me.lower() not in names:
            continue
        disp[me.lower()] = me
        side = {}
        for d in counted(chat_id, uid, names):
            if until and d["d"] >= until:
                continue
            sc = score_pair(d["s"]) or (0.0, 0.0)
            opp = d["o"].strip()
            disp.setdefault(opp.lower(), opp)
            res = {"win": 1.0, "loss": 0.0}.get(d["r"], 0.5)
            if me.lower() < opp.lower():
                key = (d["d"], me.lower(), opp.lower(), sc[0], sc[1])
            else:
                key, res = (d["d"], opp.lower(), me.lower(), sc[1], sc[0]), 1 - res
            side[key] = side.get(key, 0) + 1
            games.setdefault(key, [res, 0])
        for key, n in side.items():
            games[key][1] = max(games[key][1], n)  # у второго пилота та же дуэль — не удваиваем
    return games, disp


def elo_table(chat_id, names=None, until=None):
    names = club_names() if names is None else names
    games, disp = elo_games(chat_id, names, until)
    rating, played = {}, {}
    for key in sorted(games):
        res, n = games[key]
        a, b = key[1], key[2]
        for _ in range(n):
            ra, rb = rating.get(a, ELO_START), rating.get(b, ELO_START)
            delta = ELO_K * (res - 1 / (1 + 10 ** ((rb - ra) / 400)))
            rating[a], rating[b] = ra + delta, rb - delta
            played[a], played[b] = played.get(a, 0) + 1, played.get(b, 0) + 1
    return {k: [disp.get(k, k), r, played[k]] for k, r in rating.items()}


def elo_json(chat_id, names=None):
    names = club_names() if names is None else names
    now = elo_table(chat_id, names)
    before = elo_table(chat_id, names, until=week_bounds()[0].isoformat())
    rows = [[v[0], round(v[1]), v[2], round(v[1] - (before[k][1] if k in before else ELO_START))] for k, v in now.items()]
    return sorted(rows, key=lambda r: (-r[1], r[0].lower()))  # [ник, рейтинг, дуэлей, за неделю]


# ---------- Ачивки ----------
# Однажды полученная ачивка остаётся навсегда (settings[клуб]["ach"]: uid → {код: дата}).
ACH = {
    "first": ("🏁", "Первая дуэль", "первая дуэль в зачёт"),
    "d50": ("🔥", "50 дуэлей", "50 дуэлей в зачёт за 10 недель"),
    "d100": ("💯", "100 дуэлей", "100 дуэлей в зачёт за 10 недель"),
    "w5": ("⚡", "5 побед подряд", "5 побед подряд в дуэлях"),
    "w10": ("🌪", "10 побед подряд", "10 побед подряд в дуэлях"),
    "s195": ("🚀", "195+ баллов", "195 баллов и больше в дуэли"),
    "s200": ("👑", "200 баллов", "идеальные 200 баллов в дуэли"),
    "norm": ("✅", "Норма", "выполнил(а) недельную норму"),
    "norm3": ("📅", "3 нормы подряд", "норма 3 недели подряд"),
    "rival": ("🤝", "Заклятый соперник", "10 дуэлей с одним и тем же пилотом"),
    "king": ("🗺", "Король карты", "больше всех побед на одной из карт"),
    "uts": ("🏆", "Чемпион мини-УТС", "выиграл(а) мини-УТС"),
    "elo1": ("🥇", "Первый в рейтинге", "первое место в рейтинге Эло"),
}


def ach_earned(chat_id, uid, names, ctx):
    lst = counted(chat_id, uid, names)
    got = set()
    if lst:
        got.add("first")
    if len(lst) >= 50:
        got.add("d50")
    if len(lst) >= 100:
        got.add("d100")
    run = best = 0
    for d in lst:
        run = run + 1 if d["r"] == "win" else 0
        best = max(best, run)
    if best >= 5:
        got.add("w5")
    if best >= 10:
        got.add("w10")
    top = max((own_score(d["s"]) for d in lst), default=0)
    if top >= 195:
        got.add("s195")
    if top >= 200:
        got.add("s200")
    norm = get_norm(chat_id)
    if lst and any(duels_in(chat_id, uid, *week_bounds(back=i)) >= norm for i in range(DUEL_KEEP_DAYS // 7 + 1)):
        got.add("norm")
    if norm_streak(chat_id, uid, norm) >= 3:
        got.add("norm3")
    opp = {}
    for d in lst:
        opp[d["o"].strip().lower()] = opp.get(d["o"].strip().lower(), 0) + 1
    if any(n >= 10 for n in opp.values()):
        got.add("rival")
    me = pilot_name(chat_id, uid).strip().lower()
    if me in ctx["kings"]:
        got.add("king")
    if uid in ctx["uts"]:
        got.add("uts")
    if me and me == ctx["elo1"]:
        got.add("elo1")
    return got


def ach_update(chat_id):
    # Проверяет всех пилотов и объявляет новые ачивки в теме отчётов. Первый запуск — молча (без спама старыми).
    conf = settings.setdefault(chat_id, {})
    first_run = "ach" not in conf
    ach = conf.setdefault("ach", {})
    names = club_names()
    if not names:  # таблица пилотов не прочиталась — не раздаём ачивки за дуэли с кем попало
        return []
    elo = elo_table(chat_id, names)
    ranked = [k for k, v in sorted(elo.items(), key=lambda kv: -kv[1][1]) if v[2] >= 10]  # лидер — от 10 дуэлей
    ctx = {"kings": {v[0].strip().lower() for v in map_kings(chat_id, names).values()},
           "uts": {h["places"][0] for h in conf.get("uts_hist", []) if h.get("places")},
           "elo1": ranked[0] if ranked else ""}
    new = []
    for uid in club_uids(chat_id):
        if not pilot_name(chat_id, uid):
            continue
        have = ach.setdefault(str(uid), {})
        for code in sorted(ach_earned(chat_id, uid, names, ctx), key=list(ACH).index):
            if code not in have:
                have[code] = msk_today().isoformat()
                new.append((uid, code))
    if new and not first_run:
        by = {}
        for uid, code in new:
            by.setdefault(uid, []).append(code)
        lines = [f"{mention_uid(chat_id, uid)} — " + ", ".join(f"{ACH[c][0]} {ACH[c][1]}" for c in codes)
                 for uid, codes in by.items()]
        post_to_reports(chat_id, "🏅 Новые ачивки!\n" + "\n".join(lines) + "\n\nВсе значки — в профиле пилота в приложении клуба.")
    return new


def mention_uid(chat_id, uid):
    u = members.get(chat_id, {}).get(uid, {})
    return f"@{u['username']}" if u.get("username") else pilot_name(chat_id, uid)


def ach_json(chat_id):
    out = {}
    for uid, have in settings.get(chat_id, {}).get("ach", {}).items():
        who = pilot_name(chat_id, int(uid)).strip().lower()
        if who:
            out[who] = sorted([[c, d] for c, d in have.items() if c in ACH], key=lambda x: list(ACH).index(x[0]))
    return out


DM_TEST_TEXT = ("✅ Тест: бот может писать вам в ЛС.\n"
                "Сюда по понедельникам будет приходить список не выполнивших норму с кнопками варна, "
                "а также сигналы о возможной накрутке дуэлей.")


def private_dmtest(msg, args):
    # /dmtest в ЛС бота — проверка, что сообщения админам доходят. /dmtest all — разослать всем админам клуба.
    uid = msg["from"]["id"]
    chats = [c for c, conf in settings.items() if "duel_thread" in conf]
    if not chats or get_level(chats[0], uid) < 2:
        reply(msg, "Проверка ЛС доступна только администраторам клуба.")
        return
    chat_id = chats[0]
    if not (args and args[0].lower() in ("all", "все")):
        reply(msg, DM_TEST_TEXT + "\n\n/dmtest all — проверить, доходят ли сообщения всем админам.")
        return
    ok, fail = [], []
    for a in get_admins(chat_id):
        user = a["user"]
        r = api("sendMessage", {"chat_id": user["id"], "text": DM_TEST_TEXT + f"\n(проверку запустил {name_of(msg['from'])})"})
        (ok if r.get("ok") else fail).append(name_of(user) + (f" @{user['username']}" if user.get("username") else ""))
    text = "📨 Проверка ЛС админов.\nДоходит: " + (", ".join(ok) or "никому")
    if fail:
        text += f"\nНе доходит: {', '.join(fail)} — им нужно открыть @{BOT_USERNAME} и нажать Start."
    reply(msg, text)


def backup_state(now):
    # Копия всей базы в отдельную строку bot_state: backup_mon … backup_sun, то есть хранится неделя копий
    try:
        r = requests.post(f"{SB_URL}/rest/v1/bot_state", headers=dict(SB_HEADERS, Prefer="resolution=merge-duplicates"),
                          json={"id": "backup_" + now.strftime("%a").lower(), "data": state_dict()}, timeout=20)
        if r.status_code >= 300:
            print(f"Ошибка резервной копии: HTTP {r.status_code} {r.text[:200]}")
    except Exception as e:
        print("Ошибка резервной копии:", e)


def scheduler_tick(now):
    url = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
    if url and time.time() - _auto["ping"] > 600:
        # Бесплатный Render усыпляет бота после 15 минут тишины. Стучимся к себе сами, чтобы он не спал.
        _auto["ping"] = time.time()
        try:
            requests.get(url + "/", timeout=20)
        except Exception as e:
            print("Самопинг не прошёл:", e)
    with LOCK:
        load_state()
        if not DB_OK:
            return
        week = week_bounds(now.date())[0].isoformat()
        changed = False
        for chat_id, conf in list(settings.items()):
            if "duel_thread" not in conf:
                continue
            if now.weekday() == REMIND_DAY and now.hour >= REMIND_HOUR and conf.get("reminded") != week:
                conf["reminded"] = week
                friday_reminder(chat_id, now)
                changed = True
            if now.weekday() == NUDGE_DAY and now.hour >= NUDGE_HOUR and conf.get("nudged") != week:
                conf["nudged"] = week
                thursday_nudge(chat_id, now)
                changed = True
            if now.weekday() == SUMMARY_DAY and now.hour >= SUMMARY_HOUR and conf.get("summarized") != week:
                conf["summarized"] = week
                monday_summary(chat_id, now)
                livery_of_week(chat_id, now)
                changed = True
        if uts_tick(time.time()):  # турниры: напоминания, чек-ин, автоотмена, техпоражения
            changed = True
        if dz_tick(time.time()):  # «ищу дуэль» и вызовы без ответа
            changed = True
        if club_chat() and ach_update(club_chat()):  # новые ачивки
            changed = True
        if changed:
            save_state()
        if time.time() - _auto["backup"] > 6 * 3600:
            _auto["backup"] = time.time()
            backup_state(now)


def scheduler_loop():
    while True:
        try:
            scheduler_tick(msk_now())
        except Exception as e:
            print("Ошибка планировщика:", repr(e))
        time.sleep(60)  # раз в минуту: таймеры турниров (остальное срабатывает по своим отметкам)


HELP_TEXT = """📖 Команды бота

👥 Для всех:
/help — это сообщение
/all или слово «созвать» — отметить всех, кого знает бот
/profile (/me) — карточка пилота, уровень и должности (ответом или /profile @ник — чужая)
/stats — статистика сообщений (ответом — чужая)
/top — топ-10 по сообщениям
/roster — участники: должность, сообщения за день, неделю и всё время
/team — состав клуба по должностям
/staff — модераторы и админы
/rules — правила чата
/warns — свои предупреждения (ответом — чужие)
/report причина — ответом на сообщение: позвать модерацию
/norm (или «норма») — твои дуэли за неделю; /norm all — все; ответом — чужие
/myprofile — в ЛС бота: фото своей машины (обязательно в ливрее клуба) и подпись для приложения
/app — кнопка с таблицами клуба

📸 Дуэли: кидай скрины «История заездов» в тему отчётов — бот сам посчитает
дуэли за неделю (пн–вс), повторы со старых скринов не засчитываются.
Дуэль с участником клуба засчитывается обоим с первого скрина — второму сдавать её не нужно.
В норму идут только дуэли с участниками клуба и только если у тебя не меньше 100.1 балла
(100 / 100 — это пустой заезд лидером на 0, такие не считаются).
/recheck — ответом на свой скрин: перечитать, если ИИ не смог (модераторы — любой)

🛡 Модератор (уровень 1+):
/warn причина — предупреждение, на {limit}-м бан
/unwarn — снять предупреждения
/mute 30m причина — мут (30, 30m, 2h, 1d; по умолчанию {mute} мин.)
/unmute — снять мут
/kick причина — выгнать (сможет вернуться)
/del — удалить сообщение
/modlog — журнал последних действий

⭐ Администратор (уровень 2+, все админы Телеграма):
/ban 1d причина — бан, срок по желанию
/unban — разбан
/pin, /unpin — закрепить / открепить
/promote [уровень] — повысить: 1 модератор, 2 администратор
/demote [уровень] — понизить
/setrole должность — выдать должность в клубе (можно несколько)
/unrole [должность] — снять одну или все
/setrules текст — свои правила для /rules
/setwelcome текст — приветствие новичков ({{name}} — имя)
/welcome on|off — включить или выключить приветствие
/duelhere — написать в теме отчётов: бот начнёт считать там дуэли (off — выключить)
/setnorm 20 — норма дуэлей в неделю
/maplogo — только владелец, в ЛС бота: логотипы карт для рейтинга карт в приложении
/designer @ник — только владелец: назначить ливрейщика (/designer @ник off — снять)
/livery — ливрейщикам, в ЛС бота: загрузить ливрею в «Мастерскую ливрей» приложения
/liveryhere — написать в нужной теме: туда бот будет публиковать новые ливреи и «Ливрею недели»
/order — в ЛС бота: заказать ливрею у ливрейщика клуба
/uts — мини-УТС: создать турнир (топ-4…32, с лузер-сеткой или на вылет), запись — кнопкой «Участвую»
/lfg или «ищу дуэль» — бот объявит, что ты ищешь соперника, и сведёт пару
/call @ник (или «вызов» ответом) — вызвать пилота на дуэль: он примет или ответит «не сейчас»

🤖 Сам бот: в пятницу 18:00 напоминает тем, у кого меньше половины нормы, в понедельник 10:00 подводит
итоги недели. Админам в ЛС приходит список не выполнивших норму с кнопками варна и сигналы о возможной
накрутке. Чтобы получать ЛС, открой бота и нажми Start. Проверка: /dmtest в ЛС бота (/dmtest all — всем админам).

Цель команды — ответ на сообщение, @ник или упоминание.
Модерация понимает и слова без слэша: бан, мут, кик, варн, пред, разбан, размут, разварн.
Владелец группы — уровень 3, может назначать администраторов.""".format(limit=WARN_LIMIT, mute=MUTE_DEFAULT_MIN)


# ---------- Разбор команд ----------
# Команда → минимальный уровень. Остальные команды доступны всем.
ADMIN_CMDS = {
    "warn": (1, cmd_warn), "unwarn": (1, cmd_unwarn), "warns": (0, cmd_warns),
    "mute": (1, cmd_mute), "unmute": (1, cmd_unmute), "kick": (1, cmd_kick),
    "del": (1, cmd_del), "modlog": (1, cmd_modlog),
    "ban": (2, cmd_ban), "unban": (2, cmd_unban), "pin": (2, cmd_pin), "unpin": (2, cmd_unpin),
    "promote": (2, cmd_promote), "demote": (2, cmd_demote),
    "setrole": (2, cmd_setrole), "unrole": (2, cmd_unrole),
    "setrules": (2, cmd_setrules), "setwelcome": (2, cmd_setwelcome), "welcome": (2, cmd_welcome),
    "setnorm": (2, cmd_setnorm), "duelhere": (2, cmd_duelhere), "recheck": (0, cmd_recheck),
    "liveryhere": (2, cmd_liveryhere),
}
SLASH_ALIASES = {"me": "profile", "mystats": "stats", "members": "roster", "roles": "team",
                 "app": "start", "admins": "staff", "mods": "staff", "duels": "norm"}
# Слова без слэша. Модерацию понимаем в начале любого сообщения (только от модераторов),
# остальное — только если сообщение состоит из одного этого слова, чтобы бот не влезал в разговор.
MOD_WORDS = {"бан": "ban", "разбан": "unban", "кик": "kick", "мут": "mute", "размут": "unmute",
             "варн": "warn", "пред": "warn", "разварн": "unwarn"}
SOLO_WORDS = {"варны": "warns", "правила": "rules", "профиль": "profile", "админы": "staff",
              "должности": "team", "журнал": "modlog", "репорт": "report", "норма": "norm"}


def parse_command(text):
    # Возвращает (команда, аргументы, написана ли словом без слэша)
    parts = text.split()
    if not parts:
        return None, [], False
    first = parts[0].lower()
    if first[0] in "/!":
        name, _, bot = first[1:].partition("@")
        if bot and bot != BOT_USERNAME:
            return None, [], False  # команда другому боту
        return SLASH_ALIASES.get(name, name), parts[1:], False
    if first in MOD_WORDS:
        return MOD_WORDS[first], parts[1:], True
    if first in SOLO_WORDS and len(parts) == 1:
        return SOLO_WORDS[first], [], True
    return None, [], False


def run_admin(msg, cmd, args, is_word):
    need, handler = ADMIN_CMDS[cmd]
    my = get_level(msg["chat"]["id"], msg["from"]["id"]) if need else 0
    if my < need:
        if not is_word:  # на слова от обычных участников молчим: это просто разговор
            reply(msg, f"Нужен уровень «{LEVELS[need]}» или выше. Кто есть кто — /staff")
        return
    handler(msg, args, my)


# ---------- Основная обработка ----------
def handle(msg):
    chat = msg["chat"]
    chat_id = chat["id"]
    is_group = chat["type"] != "private"

    # Участники: вошедшие и вышедшие
    for u in msg.get("new_chat_members", []):
        remember(chat_id, u)
        welcome(chat_id, u)
    if "left_chat_member" in msg:
        members.get(chat_id, {}).pop(msg["left_chat_member"]["id"], None)
    service = "new_chat_members" in msg or "left_chat_member" in msg
    if is_group and "from" in msg and not service:
        remember(chat_id, msg["from"])
        count_activity(msg)
    if is_group:
        # Запоминаем и тех, на чьё сообщение ответили, и тех, кого отметили без @ника
        remember(chat_id, (msg.get("reply_to_message") or {}).get("from"))
        for ent in msg.get("entities", []):
            if ent.get("type") == "text_mention":
                remember(chat_id, ent.get("user"))

    if is_group and "from" in msg and is_duel_report(msg):
        handle_duel_report(msg)
        return
    if not is_group and "from" in msg and duel_image(msg)[0]:
        profile_photo_message(msg)
        return

    text = msg.get("text", "")
    if not text or "from" not in msg:
        return
    if not is_group and profile_text_message(msg):
        return
    lower = text.lower()
    cmd, args, is_word = parse_command(text)

    if cmd == "myprofile" or (cmd == "start" and not is_group and args[:1] == ["profile"]):
        myprofile(msg)
        return
    if cmd == "maplogo":
        maplogo_menu(msg)
        return
    if cmd == "designer":
        cmd_designer(msg, args)
        return
    if cmd == "livery" or (cmd == "start" and not is_group and args[:1] == ["livery"]):
        livery_cmd(msg)
        return
    if cmd == "uts" or (cmd == "start" and not is_group and args[:1] == ["uts"]):
        uts_cmd(msg)
        return
    if cmd == "lfg" or lower.strip(" !.") in ("ищу дуэль", "ищу соперника") or (cmd == "start" and not is_group and args[:1] == ["lfg"]):
        dz_command(msg, "lfg", args)
        return
    words = lower.split()
    if cmd == "call" or (is_group and words[:1] == ["вызов"] and ((len(words) == 1 and msg.get("reply_to_message"))
                                                                  or (len(words) == 2 and words[1].startswith("@")))):
        dz_command(msg, "call", words[1:] if not cmd else args)
        return
    if cmd == "order" or (cmd == "start" and not is_group and args and args[0].startswith("order_")):
        if is_group:
            reply(msg, f"Ливрею заказывают в личке бота: напиши @{BOT_USERNAME} команду /order.")
        else:
            order_start(msg, args[0][6:] if cmd == "start" else (args[0] if args else ""))
        return

    if cmd in ADMIN_CMDS:
        if is_group:
            run_admin(msg, cmd, args, is_word)
        return
    if cmd == "help":
        reply(msg, HELP_TEXT)
        return
    if cmd == "profile":
        reply_profile(msg)
        return
    if cmd == "top":
        reply_top(msg)
        return
    if cmd == "stats":
        reply_mystats(msg)
        return

    if not is_group:
        if cmd == "dmtest":
            private_dmtest(msg, args)
            return
        if cmd == "start":
            send(chat_id, "Привет! Нажми кнопку ниже, чтобы открыть таблицы клуба.\n"
                          "Оформить свой профиль (фото машины в ливрее клуба): /myprofile")
        return

    if cmd == "roster":
        reply_roster(msg)
    elif cmd == "team":
        reply_team(msg)
    elif cmd == "staff":
        reply_staff(msg)
    elif cmd == "rules":
        reply_rules(msg)
    elif cmd == "report":
        reply_report(msg)
    elif cmd == "norm":
        reply_norm(msg, args)
    elif cmd == "normall":
        reply_norm(msg, ["all"])
    elif cmd == "all" or "созвать" in lower:
        call_everyone(msg)
    elif cmd == "start" or f"@{BOT_USERNAME}" in lower:
        send(chat_id, "Таблицы клуба: правила, расписание, пилоты и сводка.",
             reply_to=msg["message_id"], group=True)


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        if self.path.startswith(("/api/like", "/api/uts", "/api/duel")):
            # Лайк ливреи или запись на турнир из мини-приложения. Тело — JSON простым текстом (без лишнего CORS-запроса)
            with LOCK:
                try:
                    req = json.loads(body or b"{}")
                    load_state()
                    if self.path.startswith("/api/uts"):
                        res = uts_api(req.get("init"), req.get("id"), req.get("act"))
                    elif self.path.startswith("/api/duel"):
                        res = duel_api(req.get("init"), req.get("act"), req.get("to"))
                    else:
                        res = like_livery(req.get("init"), req.get("id"))
                    if res.get("ok"):
                        save_state()
                except Exception as e:
                    print("Ошибка лайка:", repr(e))
                    res = {"ok": False, "error": "ошибка бота"}
            out = json.dumps(res, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(out)
            return
        # Сразу отвечаем Телеграму, чтобы он не слал то же сообщение повторно
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")
        with LOCK:  # сообщения обрабатываем по одному, чтобы не затереть базу
            try:
                update = json.loads(body or b"{}")
                load_state()
                if "chat_member" in update:
                    handle_chat_member(update["chat_member"])
                elif "message" in update:
                    handle(update["message"])
                elif "callback_query" in update:
                    handle_callback(update["callback_query"])
            except Exception as e:
                print("Ошибка:", repr(e))
            save_state()

    def do_GET(self):
        if self.path.startswith(("/photo/", "/maplogo/", "/livery/")):
            # Фото машины пилота (/photo/<@ник>), логотип карты (/maplogo/<трасса>) или ливрея (/livery/<id>) для приложения
            kind = self.path.split("/", 2)[1]
            name = self.path.split("/", 2)[2].split("?")[0]
            try:
                data = {"maplogo": map_logo, "livery": livery_photo_bytes}.get(kind, profile_photo)(unquote(name))
            except Exception as e:
                print("Ошибка /photo:", repr(e))
                data = None
            self.send_response(200 if data else 404)
            self.send_header("Access-Control-Allow-Origin", "*")
            if data:
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            if data:
                self.wfile.write(data)
            return
        if self.path.startswith("/duels"):
            # Дуэли для мини-приложения (вкладка «Пилоты»): по @нику — сколько за эту и прошлую неделю
            try:
                body = json.dumps(duels_json(), ensure_ascii=False).encode()
            except Exception as e:
                print("Ошибка /duels:", repr(e))
                body = b"{}"
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"bot is running")

    def log_message(self, *args):
        pass


PUBLIC_MENU = [
    ("help", "Все команды"), ("all", "Отметить всех"), ("profile", "Карточка пилота"),
    ("stats", "Статистика сообщений"), ("top", "Топ по сообщениям"), ("roster", "Участники"),
    ("team", "Состав по должностям"), ("staff", "Модераторы и админы"), ("rules", "Правила чата"),
    ("warns", "Предупреждения"), ("report", "Позвать модерацию (ответом)"),
    ("norm", "Норма дуэлей за неделю"), ("normall", "Норма дуэлей у всех"),
    ("recheck", "Перечитать скрин (ответом)"), ("lfg", "Ищу дуэль: найти соперника"),
    ("call", "Вызвать на дуэль: /call @ник"), ("myprofile", "Мой профиль: фото машины в ливрее клуба"),
    ("app", "Таблицы клуба"),
]
ADMIN_MENU = PUBLIC_MENU + [
    ("warn", "Предупреждение"), ("mute", "Мут: /mute 30m причина"), ("unmute", "Снять мут"),
    ("kick", "Выгнать"), ("ban", "Бан: /ban 1d причина"), ("unban", "Разбан"), ("unwarn", "Снять варны"),
    ("del", "Удалить сообщение"), ("pin", "Закрепить"), ("unpin", "Открепить"),
    ("promote", "Повысить уровень"), ("demote", "Понизить уровень"),
    ("setrole", "Выдать должность"), ("unrole", "Снять должность"),
    ("setrules", "Задать правила"), ("setwelcome", "Задать приветствие"),
    ("welcome", "Приветствие вкл/выкл"), ("modlog", "Журнал модерации"),
    ("duelhere", "Считать дуэли в этой теме"), ("setnorm", "Норма дуэлей в неделю"),
]


def setup_webhook():
    # Render сам сообщает адрес сервиса. Подписываемся на сообщения и на вход/выход участников,
    # чтобы не настраивать это вручную ссылкой setWebhook.
    url = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
    if url:
        res = api("setWebhook", {"url": url + "/", "allowed_updates": ["message", "chat_member", "callback_query"]})
        print("Вебхук:", "настроен" if res.get("ok") else res)
    info = api("getWebhookInfo", {}).get("result", {})
    if info.get("last_error_message"):
        print("Последняя ошибка вебхука у Телеграма:", info["last_error_message"])
    me = api("getMe", {}).get("result", {})
    if me and not me.get("can_read_all_group_messages"):
        print("Внимание: у бота включена приватность. Если он не админ группы, он не видит обычные "
              "сообщения и не запоминает участников. BotFather → /setprivacy → Disable или сделай бота админом.")
    # Меню команд при вводе «/»: участникам — общие, админам — ещё и модерация
    as_cmds = lambda menu: [{"command": c, "description": d} for c, d in menu]
    api("setMyCommands", {"commands": as_cmds(PUBLIC_MENU)})
    api("setMyCommands", {"commands": as_cmds(ADMIN_MENU), "scope": {"type": "all_chat_administrators"}})
    print("База данных:", "подключена" if DB_OK else "НЕ подключена, смотри ошибку выше")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    setup_webhook()
    DUEL_ASYNC = True
    threading.Thread(target=duel_worker, daemon=True).start()
    threading.Thread(target=scheduler_loop, daemon=True).start()
    if SHEET_HOOK:
        # При запуске сразу обновляем таблицу «KHR — дуэли», не дожидаясь нового отчёта
        threading.Thread(target=lambda: [sync_sheet(c) for c in list(duels)], daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
