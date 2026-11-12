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
from urllib.parse import unquote
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
    "Не придумывай строки, которых не видно, и не пропускай видимые."
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
    for e in s["errors"]:
        lines.append("⚠️ " + e)
    status = "✅ норма выполнена" if week >= norm else f"осталось {norm - week}"
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
        a, d, k = add_duels(chat_id, user["id"], res.get("opponent"), res["rows"], group, s["items"], alerts)
        s["added"] += a
        s["dup"] += d
        s["skip"] += k
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
        norm, pilots = DUEL_NORM_DEFAULT, {}
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
        # Профили: есть ли фото машины (v — версия, чтобы сайт подхватил новое фото) и подпись
        prof = {(p.get("username") or "").lower(): {"photo": p.get("v", 0) if p.get("car") else 0,
                                                    "about": p.get("about", "")}
                for p in profiles.values() if p.get("username") and (p.get("car") or p.get("about"))}
        return {"week": week_label(start, end), "prev_week": week_label(pstart, pend), "norm": norm,
                "pilots": pilots, "profiles": prof, "tracks": tracks_json()}


def tracks_json():
    # Популярные карты для приложения: за эту неделю и за всё время (по чату клуба)
    chat_id = club_chat()
    if not chat_id:
        return {"week": [], "all": []}
    fresh = chat_id not in tracks
    start, end = week_bounds()
    out = {"week": tracks_top(chat_id, start, end), "all": tracks_top(chat_id)}
    if fresh:
        save_state()  # счётчик «за всё время» только что заполнили из сданных дуэлей — запоминаем
    return out


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
                             "Пока меньше половины:\n" + "\n".join(f"{mention(p)} — {n}/{norm}" for n, p in behind)
                    + "\n\nСкрины «История заездов» кидайте сюда.")


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
    post_to_reports(chat_id, text)
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
            if now.weekday() == SUMMARY_DAY and now.hour >= SUMMARY_HOUR and conf.get("summarized") != week:
                conf["summarized"] = week
                monday_summary(chat_id, now)
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
        time.sleep(300)


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
        if self.path.startswith(("/photo/", "/maplogo/")):
            # Фото машины пилота (/photo/<@ник>) или логотип карты (/maplogo/<трасса>) для приложения
            is_logo = self.path.startswith("/maplogo/")
            name = self.path.split("/", 2)[2].split("?")[0]
            try:
                data = map_logo(unquote(name)) if is_logo else profile_photo(unquote(name))
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
    ("recheck", "Перечитать скрин (ответом)"), ("myprofile", "Мой профиль: фото машины в ливрее клуба"),
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
