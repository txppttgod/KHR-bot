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
    global DB_OK, members, stats, warns, staff, roles, settings, modlog, duels
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
    warns = {}
    for k, n in d.get("warns", {}).items():
        c, u = k.split("_")
        warns[(int(c), int(u))] = n


def save_state():
    if not DB_OK:
        return
    db_save({
        "members": str_keys(members),
        "stats": str_keys(stats),
        "staff": str_keys(staff),
        "roles": str_keys(roles),
        "settings": str_keys(settings, 1),
        "modlog": str_keys(modlog, 1),
        "duels": str_keys(duels),
        "warns": {f"{c}_{u}": n for (c, u), n in warns.items()},
    })


members, stats, warns, staff, roles, settings, modlog, duels = {}, {}, {}, {}, {}, {}, {}, {}
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


def duels_in(chat_id, uid, start, end):
    a, b = start.isoformat(), end.isoformat()
    return sum(1 for d in duels.get(chat_id, {}).get(uid, []) if a <= d["d"] <= b)


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


def gemini_read(image, mime):
    body = {
        "contents": [{"parts": [
            {"text": DUEL_PROMPT},
            {"inline_data": {"mime_type": mime, "data": base64.b64encode(image).decode()}},
        ]}],
        "generationConfig": {"temperature": 0, "responseMimeType": "application/json",
                             "responseSchema": DUEL_SCHEMA},
    }
    last = 0
    for model in gemini_models()[:3]:
        for attempt in range(2):
            try:
                r = requests.post(f"{GEMINI_API}/models/{model}:generateContent",
                                  headers={"x-goog-api-key": GEMINI_KEY}, json=body, timeout=45)
            except Exception as e:
                print(f"Gemini {model}: нет ответа ({e.__class__.__name__}), пробую следующую")
                r = None
                break
            if r.status_code in (429, 500, 503) and attempt == 0:
                time.sleep(8 if r.status_code == 429 else 3)  # лимит бесплатного ключа или перегрузка
                continue
            break
        if r is None:
            last = 503
            continue
        last = r.status_code
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


def add_duels(chat_id, uid, opponent, rows, group):
    # Возвращает (новых, повторов, вне срока)
    mine = duels.setdefault(chat_id, {}).setdefault(uid, [])
    have = {(d["d"], d["o"].lower(), d["s"]) for d in mine}
    today = msk_today()
    oldest = today - datetime.timedelta(days=DUEL_KEEP_DAYS)
    added = dup = skip = 0
    opp = re.sub(r"^\s*vs\.?\s*", "", opponent or "", flags=re.I).strip() or "?"
    for row in rows:
        try:
            day = datetime.date.fromisoformat(str(row.get("date", "")).strip())
        except ValueError:
            skip += 1
            continue
        if day > today + datetime.timedelta(days=1) or day < oldest:
            skip += 1
            continue
        key = (day.isoformat(), opp.lower(), norm_score(row.get("score")))
        if key in have:
            dup += 1
            continue
        have.add(key)
        mine.append({"d": key[0], "o": opp, "r": row.get("result", ""), "s": key[2],
                     "t": (row.get("track") or "")[:40], "g": group})
        added += 1
    mine.sort(key=lambda d: d["d"])
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
    if s["dup"]:
        extra.append(f"повторы (уже были): {s['dup']}")
    if s["skip"]:
        extra.append(f"вне срока или без даты: {s['skip']}")
    if extra:
        lines.append(" · ".join(extra).capitalize())
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
        s = _albums[key] = {"added": 0, "dup": 0, "skip": 0, "bad": 0, "errors": [], "reply": None,
                            "uid": user["id"], "t": time.time()}
    for k in [k for k, v in _albums.items() if time.time() - v["t"] > 3600]:
        del _albums[k]

    if err:
        if err not in s["errors"]:
            s["errors"].append(err)
    elif not res.get("is_duel_history") or not res.get("rows"):
        s["bad"] += 1
    else:
        a, d, k = add_duels(chat_id, user["id"], res.get("opponent"), res["rows"], group)
        s["added"] += a
        s["dup"] += d
        s["skip"] += k
        print(f"Дуэли {name_of(user)}: {res.get('opponent')} +{a}, повторов {d}, вне срока {k}")

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
    msg = cq.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    if not data.startswith("du:") or not chat_id:
        api("answerCallbackQuery", {"callback_query_id": cq["id"]})
        return
    _, uid, group = data.split(":", 2)
    uid = int(uid)
    clicker = cq["from"]
    if clicker["id"] != uid and get_level(chat_id, clicker["id"]) < 1:
        api("answerCallbackQuery", {"callback_query_id": cq["id"], "show_alert": True,
                                    "text": "Отменить отчёт может только его автор или модератор."})
        return
    mine = duels.get(chat_id, {}).get(uid, [])
    removed = sum(1 for d in mine if d.get("g") == group)
    if uid in duels.get(chat_id, {}):
        duels[chat_id][uid] = [d for d in mine if d.get("g") != group]
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


def sheet_payload(chat_id):
    names = {u["id"]: u for u in members.get(chat_id, {}).values()}
    duel_rows = []
    for uid, lst in duels.get(chat_id, {}).items():
        u = names.get(uid, {})
        for d in lst:
            day = datetime.date.fromisoformat(d["d"])
            duel_rows.append([week_label(*week_bounds(day)), day.strftime("%d.%m.%Y"), u.get("first_name", str(uid)),
                              "@" + u["username"] if u.get("username") else "", d["o"],
                              RESULT_RU.get(d["r"], d["r"]), d["s"], d.get("t", "")])
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
        "duels_head": ["Неделя", "Дата", "Пилот", "Телеграм", "Соперник", "Результат", "Счёт", "Трасса"],
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
/app — кнопка с таблицами клуба

📸 Дуэли: кидай скрины «История заездов» в тему отчётов — бот сам посчитает
дуэли за неделю (пн–вс), повторы со старых скринов не засчитываются.
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

    text = msg.get("text", "")
    if not text or "from" not in msg:
        return
    lower = text.lower()
    cmd, args, is_word = parse_command(text)

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
        if cmd == "start":
            send(chat_id, "Привет! Нажми кнопку ниже, чтобы открыть таблицы клуба.")
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
    ("recheck", "Перечитать скрин (ответом)"), ("app", "Таблицы клуба"),
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
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
