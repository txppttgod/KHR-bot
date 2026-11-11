# Проверки bot.py с поддельными Telegram, Supabase и Gemini — без интернета и без ключей.
# Запуск из корня репозитория: python tests/test_bot.py (GitHub запускает их сам при каждом изменении).
import sys, types, json, os

calls = []
STATUS = {}      # user_id -> статус в чате
DB = {"data": {}}

class Resp:
    def __init__(self, data, code=200):
        self._d, self.status_code, self.text, self.encoding = data, code, json.dumps(data), "utf-8"
    def json(self):
        return self._d

CSV = {"text": "№,Имя,Телеграм\n1,Эрнест,@ernest,5,2,10,Пилот\n"}
GEM = {"answer": None, "code": 200, "calls": 0, "fail": {}, "used": [], "think": []}
MODELS = ["gemini-2.5-flash", "gemini-3-flash", "gemini-3-flash-lite", "gemini-3.1-flash-image", "gemini-3-pro",
          "gemini-3.1-flash-preview"]

def fake_get(url, **kw):
    if "generativelanguage" in url:
        return Resp({"models": [{"name": "models/" + m, "supportedGenerationMethods": ["generateContent"]} for m in MODELS]})
    if "/file/bot" in url:
        r = Resp(None); r.content = b"\xff\xd8fakejpeg"; return r
    if "supabase" in url:
        return Resp([{"data": json.loads(json.dumps(DB["data"]))}])
    r = Resp(None); r.text = CSV["text"]; return r

def fake_patch(url, json=None, **kw):
    DB["data"] = json["data"]; return Resp(None, 204)

def fake_post(url, json=None, **kw):
    if "generativelanguage" in url:
        GEM["calls"] += 1
        assert json["contents"][0]["parts"][1]["inline_data"]["data"]
        model = url.split("/models/")[1].split(":")[0]
        GEM["used"].append(model)
        GEM["think"].append(json["generationConfig"].get("thinkingConfig"))
        if GEM["fail"].get(model) == "nothink" and "thinkingConfig" in json["generationConfig"]:
            return Resp({"error": "bad thinking"}, 400)
        if GEM["fail"].get(model) == "timeout":
            raise TimeoutError("read timed out")
        if isinstance(GEM["fail"].get(model), int):
            return Resp({"error": "x"}, GEM["fail"][model])
        return Resp({"candidates": [{"content": {"parts": [{"text": __import__("json").dumps(GEM["answer"])}]}}]}, GEM["code"])
    method = url.rsplit("/", 1)[1]
    if method == "sendMessage":
        calls.append((method, json))
        return Resp({"ok": True, "result": {"message_id": 9000 + len(calls)}})
    calls.append((method, json))
    if method == "getChatMember":
        return Resp({"ok": True, "result": {"status": STATUS.get(json["user_id"], "member")}})
    if method == "getChatAdministrators":
        return Resp({"ok": True, "result": [{"status": s, "user": {"id": u, "first_name": f"A{u}"}}
                                            for u, s in STATUS.items() if s in ("creator", "administrator")]})
    if method in ("getWebhookInfo", "getMe"):
        return Resp({"ok": True, "result": {"url": "", "can_read_all_group_messages": True}})
    if method == "getFile":
        return Resp({"ok": True, "result": {"file_path": "photos/f.jpg"}})
    if method == "getChatMemberCount":
        return Resp({"ok": True, "result": 10})
    return Resp({"ok": True, "result": True})

req = types.ModuleType("requests")
req.get, req.post, req.patch = fake_get, fake_post, fake_patch
sys.modules["requests"] = req
os.environ.update(BOT_TOKEN="1:x", SUPABASE_URL="https://x.supabase.co", SUPABASE_KEY="sb_secret_x",
                  GEMINI_API_KEY="g")

BOT_PY = "bot.py" if os.path.exists("bot.py") else os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bot.py")
src = open(BOT_PY, encoding="utf-8").read()
bot = types.ModuleType("bot")
exec(compile(src, "bot.py", "exec"), bot.__dict__)

CHAT = -1001
OWNER, ADMIN, MOD, USER, USER2 = 1, 2, 3, 4, 5
STATUS.update({OWNER: "creator", ADMIN: "administrator"})
names = {OWNER: ("Эрнест", "ernest"), ADMIN: ("Ник", "moxitowo"), MOD: ("Даня", "danya"),
         USER: ("Иван", "ivan"), USER2: ("Петя", "")}
mid = [100]

def u(uid):
    n, un = names[uid]
    d = {"id": uid, "first_name": n, "is_bot": False}
    if un: d["username"] = un
    return d

def send(uid, text, reply_to=None, **extra):
    mid[0] += 1
    m = {"message_id": mid[0], "chat": {"id": CHAT, "type": "supergroup"}, "from": u(uid), "text": text}
    if reply_to: m["reply_to_message"] = {"message_id": 1, "from": u(reply_to), "text": "старое"}
    m.update(extra)
    calls.clear()
    bot.load_state(); bot.handle(m); bot.save_state()
    return [(c, p) for c, p in calls if c not in ("getChatMember", "getChatAdministrators")]

def said(res, cut=100000):
    return " || ".join(p.get("text", "")[:cut].replace("\n", " / ") if c == "sendMessage" else f"<{c}>" for c, p in res)

checks = []
def check(name, cond, res):
    checks.append(("OK  " if cond else "FAIL") + " " + name + "  ->  " + said(res, 130))

for x in (USER, USER2, MOD, ADMIN): send(x, "привет")
r = send(USER, "/warn", reply_to=USER2); check("user cannot warn", "уровень" in said(r), r)
r = send(USER, "бан", reply_to=USER2); check("plain word from user ignored", r == [], r)
r = send(ADMIN, "/promote @danya"); check("admin promotes to mod", "Модератор" in said(r), r)
r = send(MOD, "мут 30m флуд", reply_to=USER); check("mod mutes via word", any(c == "restrictChatMember" for c, _ in r) and "30 мин" in said(r), r)
r = send(MOD, "/ban", reply_to=USER); check("mod cannot ban", "Администратор" in said(r), r)
r = send(MOD, "/warn @ivan спам"); check("warn by @username", "1 из 3" in said(r), r)
r = send(MOD, "/warn", reply_to=ADMIN); check("cannot warn higher level", "Нельзя" in said(r), r)
r = send(USER, "/warns"); check("own warns", "1 из 3" in said(r) and "спам" in said(r), r)
r = send(ADMIN, "/ban 1d токсик", reply_to=USER); check("temp ban", any(c == "banChatMember" and "until_date" in p for c, p in r) and "на 1 дн. Причина" in said(r), r)
r = send(ADMIN, "/setrole @danya Настройщик"); check("setrole", "Настройщик" in said(r), r)
r = send(ADMIN, "/setrole @danya Бухгалтер"); check("second role", "Настройщик, Бухгалтер" in said(r), r)
r = send(USER2, "/team"); check("team lists roles", "Настройщик" in said(r) and "Даня" in said(r), r)
r = send(USER2, "/staff"); check("staff list", "Владелец" in said(r) and "Модератор" in said(r) and "Даня" in said(r), r)
r = send(OWNER, "/setrules 1. Не флудить\n2. Катать УТС"); check("setrules", "сохранены" in said(r), r)
r = send(USER2, "правила"); check("rules word", "Не флудить" in said(r), r)
r = send(USER2, "правила чата где?"); check("rules word inside sentence ignored", r == [], r)
r = send(USER2, "/report оскорбления", reply_to=MOD); check("report tags staff", "Жалоба" in said(r) and "оскорбления" in said(r), r)
r = send(MOD, "/modlog"); check("modlog", "мут на 30 мин" in said(r) and "бан на 1 дн" in said(r), r)
r = send(ADMIN, "/demote @danya"); check("demote", "Участник" in said(r), r)
r = send(USER2, "/profile @danya"); check("profile shows roles", "Должности: Настройщик, Бухгалтер" in said(r), r)
r = send(USER2, "/help"); check("help", "Модератор" in said(r), r)
r = send(USER2, "/all"); check("call everyone", "Общий сбор" in said(r), r)
r = send(OWNER, "/kick", reply_to=USER2); check("kick", any(c == "unbanChatMember" for c, _ in r), r)
mid[0] += 1
calls.clear(); bot.load_state()
bot.handle({"message_id": mid[0], "chat": {"id": CHAT, "type": "supergroup"}, "from": u(USER2),
            "new_chat_members": [u(USER2)]}); bot.save_state()
check("welcome on join", any("Добро пожаловать в клуб, Петя" in (p or {}).get("text", "") for _, p in calls), calls)
r = send(OWNER, "/welcome off"); check("welcome off", "выключено" in said(r), r)
r = send(USER2, "/start@other_bot"); check("other bot command ignored", r == [], r)
check("state persisted", "staff" in DB["data"] and "roles" in DB["data"] and "modlog" in DB["data"], [])
calls.clear(); bot.setup_webhook()
menus = [p for c, p in calls if c == "setMyCommands"]
check("command menus", len(menus) == 2 and all(len(m["commands"]) <= 100 and all(len(x["description"]) <= 256 for x in m["commands"]) for m in menus), [])
# ---------- дуэли ----------
import datetime as dt
today = bot.msk_today()
mon = today - dt.timedelta(days=today.weekday())
iso = lambda d: d.isoformat()
def photo(uid, thread=83379, group=None):
    mid[0] += 1
    m = {"message_id": mid[0], "chat": {"id": CHAT, "type": "supergroup"}, "from": u(uid),
         "message_thread_id": thread, "photo": [{"file_id": "small"}, {"file_id": "big"}]}
    if group: m["media_group_id"] = group
    calls.clear(); bot.load_state(); bot.handle(m); bot.save_state()
    return [(c, p) for c, p in calls if c not in ("getChatMember", "getChatAdministrators", "getFile")]
def ans(opp, rows):
    GEM["answer"] = {"is_duel_history": True, "opponent": opp,
                     "rows": [{"date": d, "result": r, "score": s} for d, r, s in rows]}

r = photo(USER2); check("no thread set -> ignored", r == [] and GEM["calls"] == 0, r)
mid[0] += 1
calls.clear(); bot.load_state()
bot.handle({"message_id": mid[0], "chat": {"id": CHAT, "type": "supergroup"}, "from": u(OWNER),
            "message_thread_id": 83379, "text": "/duelhere"}); bot.save_state()
check("duelhere", any("считает дуэли" in (p or {}).get("text", "") for _, p in calls), calls)
r = photo(USER, thread=1); check("other thread ignored", r == [], r)
ans("vs Fatcat", [(iso(mon), "loss", "176.9 / 178.8"), (iso(mon), "win", "180,1/170.2"),
                  (iso(mon - dt.timedelta(days=3)), "win", "1 / 2"), ("2020-01-01", "win", "3/4")])
r = photo(USER2)
check("report lists each duel", f"{mon:%d.%m} vs Fatcat — Поражение (176.9 / 178.8) → эта неделя" in said(r)
      and "→ неделя " in said(r) and "→ вне срока" in said(r), r)
check("first report counts this week + old",
                        "Засчитано новых дуэлей: 3" in said(r) and "1 из 20" not in said(r) and "2 из 20" in said(r)
                        and "Вне срока" in said(r) and r[0][1].get("message_thread_id") == 83379, r)
ans("Fatcat", [(iso(mon), "loss", "176.9/178.8"), (iso(mon), "win", "180.1 / 170.2"), (iso(mon), "draw", "5/5")])
r = photo(USER2, group="alb1"); check("dups skipped", "Засчитано новых дуэлей: 1" in said(r) and "повторы (уже были): 2".capitalize() in said(r) and "3 из 20" in said(r), r)
ans("MXT", [(iso(mon), "win", "9/8"), (iso(mon), "win", "10/8")])
r = photo(USER2, group="alb1"); check("album edits one message", r and r[0][0] == "editMessageText" and "Засчитано новых дуэлей: 3" in r[0][1]["text"] and "5 из 20" in r[0][1]["text"], r)
GEM["answer"] = {"is_duel_history": False, "rows": []}
r = photo(USER2); check("not a duel screen", "Не похоже" in said(r), r)
r = send(USER2, "/norm"); check("norm own", "5 из 20" in said(r) and "vs MXT" in said(r), r)
# отмена альбома: чужой участник не может, автор может
def cb(uid, data):
    calls.clear(); bot.load_state()
    bot.handle_callback({"id": "q", "from": u(uid), "data": data,
                         "message": {"message_id": 777, "chat": {"id": CHAT}, "text": "отчёт"}}); bot.save_state()
    return calls
denied = lambda c, word: any(p.get("show_alert") and word in p.get("text", "") for m, p in c if m == "answerCallbackQuery") \
    and not any(m == "editMessageText" for m, _ in c)
c = cb(USER, f"du:{USER2}:alb1"); check("member cannot undo others", denied(c, "только администраторы"), c)
c = cb(USER2, f"du:{USER2}:alb1"); check("member cannot undo own", denied(c, "только администраторы"), c)
STATUS[MOD] = "administrator"
c = cb(ADMIN, f"du:{MOD}:x"); check("admin cannot undo other admin", denied(c, "только владелец"), c)
c = cb(ADMIN, f"du:{OWNER}:x"); check("admin cannot undo owner", denied(c, "только владелец"), c)
STATUS[MOD] = "member"
c = cb(ADMIN, f"du:{ADMIN}:x"); check("admin undoes own", any(m == "editMessageText" for m, _ in c), c)
c = cb(OWNER, f"du:{ADMIN}:x"); check("owner undoes admin", any(m == "editMessageText" for m, _ in c), c)
c = cb(ADMIN, f"du:{USER2}:alb1"); check("admin undoes member album", any("снято дуэлей 3" in p.get("text", "") and "2 из 20" in p.get("text", "") for m, p in c if m == "editMessageText"), c)
r = send(ADMIN, "/setnorm 2"); check("setnorm", "2 дуэлей" in said(r), r)
r = send(USER, "/norm all"); check("norm all", "Выполнили: 1" in said(r) and "Петя" in said(r) and "Эрнест" in said(r), r)
r = send(USER, "норма"); check("norm word", "0 из 2" in said(r), r)
p = bot.sheet_payload(CHAT); check("sheet payload", len(p["duels"]) == 3 and p["norm"][0][2] == 2, [])
j = bot.duels_json(); check("duels json", j["norm"] == 2 and j["pilots"] == {}, [("sendMessage", {"text": str(j)})])
bot.load_state(); bot.members[CHAT][USER2]["username"] = "petya"; bot.save_state()
j = bot.duels_json(); check("duels json by username", j["pilots"].get("petya", {}).get("week") == 2
                            and j["pilots"]["petya"]["prev"] == 1, [("sendMessage", {"text": str(j)})])
check("duels persisted", str(USER2) in DB["data"]["duels"][str(CHAT)], [])
bot.time.sleep = lambda s: None
order = bot.gemini_models()
check("model order", order == ["gemini-3-flash", "gemini-2.5-flash", "gemini-3.1-flash-preview",
                               "gemini-3-flash-lite", "gemini-flash-latest"], [])
# первая модель не ответила, вторая перегружена (503) — бот берёт третью
GEM["fail"] = {"gemini-3-flash": "timeout", "gemini-2.5-flash": 503}; GEM["used"] = []
ans("Kot", [(iso(today), "win", "1/2")])
r = photo(USER2); check("fallback after timeout and 503", "Засчитано новых дуэлей: 1" in said(r)
                        and GEM["used"] == ["gemini-3-flash", "gemini-2.5-flash", "gemini-2.5-flash",
                                            "gemini-3.1-flash-preview"], r)
# режим размышлений: 3.x — низкий, 2.5 — выключен; если модель его не принимает — повтор без него
check("thinking config", bot.thinking_config("gemini-3.8-flash") == {"thinkingLevel": "low"}
      and bot.thinking_config("gemini-2.5-flash") == {"thinkingBudget": 0}
      and bot.thinking_config("gemini-flash-latest") == {"thinkingLevel": "low"}, [])
bot._cooldown.clear(); GEM["fail"] = {"gemini-3-flash": "nothink"}; GEM["used"] = []; GEM["think"] = []
ans("Kot", [(iso(today), "win", "7/8")])
r = photo(USER2); check("retry without thinking on 400", "Засчитано новых дуэлей: 1" in said(r)
                        and GEM["used"] == ["gemini-3-flash", "gemini-3-flash"]
                        and GEM["think"] == [{"thinkingLevel": "low"}, None], r)
# модель упёрлась в лимит (429) — следующий скрин её пропускает
bot._cooldown.clear(); GEM["fail"] = {"gemini-3-flash": 429}; GEM["used"] = []
ans("Kot", [(iso(today), "win", "11/12")]); photo(USER2)
GEM["fail"] = {}; GEM["used"] = []
ans("Kot", [(iso(today), "win", "13/14")])
r = photo(USER2); check("cooldown skips limited model", GEM["used"] == ["gemini-2.5-flash"] and "Засчитано новых дуэлей: 1" in said(r), r)
# всё перегружено — понятное сообщение; потом /recheck ответом на скрин
bot._cooldown.clear(); GEM["fail"] = {m: 503 for m in order}
r = photo(USER2); check("all overloaded", "перегружен" in said(r) and "/recheck" in said(r), r)
failed_mid = mid[0]
bot._cooldown.clear(); GEM["fail"] = {}
ans("Kot", [(iso(today), "win", "3/4"), (iso(today), "win", "1/2")])
shot = {"message_id": failed_mid, "from": u(USER2), "message_thread_id": 83379, "photo": [{"file_id": "big"}],
        "chat": {"id": CHAT, "type": "supergroup"}}
r = send(USER, "/recheck", reply_to=None, reply_to_message=shot)
check("stranger cannot recheck", "только модератор" in said(r), r)
r = send(USER2, "/recheck", reply_to_message=shot)
check("author rechecks", "Засчитано новых дуэлей: 1" in said(r) and "Повторы (уже были): 1" in said(r), r)
r = send(USER2, "/recheck"); check("recheck needs reply", "Ответь /recheck" in said(r), r)

# ---------- подтверждение соперником и накрутка ----------
CSV["text"] = "№,Имя,Телеграм\n1,Ernest,@ernest,0,,,Пилот\n2,Petya,@petya,0,,,Пилот\n"
bot._cache["time"] = 0; bot._cooldown.clear(); GEM["fail"] = {}; names[USER2] = ("Петя", "petya")
send(USER2, "привет"); send(OWNER, "привет")
bot.load_state(); petya_before = bot.duels_in(CHAT, USER2, *bot.week_bounds())
ans("Petya", [(iso(today), "win", "10.5 / 9.5")]); r = photo(OWNER); owner_group = str(mid[0])
check("credited to opponent", "Засчитано новых дуэлей: 1" in said(r) and "засчитана и сопернику" in said(r) and "✔" not in said(r), r)
bot.load_state()
credit = [d for d in bot.duels[CHAT][USER2] if d["o"] == "Ernest"]
check("opponent got the duel", bot.duels_in(CHAT, USER2, *bot.week_bounds()) == petya_before + 1
      and credit and credit[0]["r"] == "loss" and credit[0]["s"] == "9.5 / 10.5" and credit[0]["by"] == OWNER, [])
ans("Ernest", [(iso(today), "loss", "9.5 / 10.5")]); r = photo(USER2)
check("opponent's own report not counted twice", "Засчитано новых дуэлей: 0" in said(r)
      and "уже засчитана по отчёту соперника ✔" in said(r)
      and bot.duels_in(CHAT, USER2, *bot.week_bounds()) == petya_before + 1, r)
bot.load_state()
check("both records marked", any(d.get("c") for d in bot.duels[CHAT][OWNER]) and any(d.get("c") for d in bot.duels[CHAT][USER2] if d["o"] == "Ernest"), [])
check("sheet shows confirmation", any(row[-1] == "✔ сдали оба" for row in bot.sheet_payload(CHAT)["duels"]), [])
# зачёт сопернику в обратную сторону — тот же счёт без разворота тоже считается той же дуэлью
ans("Ernest", [(iso(today), "win", "20 / 19")]); photo(USER2)
ans("Petya", [(iso(today), "loss", "20 / 19")]); r = photo(OWNER)
check("same-orientation score is same duel", "Засчитано новых дуэлей: 0" in said(r), r)
# отмена отчёта снимает и зачёт сопернику
ans("Petya", [(iso(today), "draw", "33 / 33"), (iso(today), "win", "34 / 30")]); photo(OWNER); g2 = str(mid[0])
bot.load_state(); mid_count = bot.duels_in(CHAT, USER2, *bot.week_bounds())
cb(OWNER, f"du:{OWNER}:{g2}"); bot.load_state()
check("undo removes credit", bot.duels_in(CHAT, USER2, *bot.week_bounds()) == mid_count - 2, [])
# проверка ЛС
mid[0] += 1; calls.clear(); bot.load_state()
bot.handle({"message_id": mid[0], "chat": {"id": ADMIN, "type": "private"}, "from": u(ADMIN), "text": "/dmtest"}); bot.save_state()
check("dmtest self", any(c == "sendMessage" and "Тест: бот может писать вам в ЛС" in p["text"] for c, p in calls), [])
mid[0] += 1; calls.clear(); bot.load_state()
bot.handle({"message_id": mid[0], "chat": {"id": ADMIN, "type": "private"}, "from": u(ADMIN), "text": "/dmtest all"}); bot.save_state()
check("dmtest all", sum(1 for c, p in calls if c == "sendMessage" and p["chat_id"] in (OWNER, ADMIN) and "проверку запустил" in p["text"]) == 2
      and any("Доходит:" in p["text"] for c, p in calls if c == "sendMessage"), [])
mid[0] += 1; calls.clear(); bot.load_state()
bot.handle({"message_id": mid[0], "chat": {"id": USER, "type": "private"}, "from": u(USER), "text": "/dmtest"})
check("dmtest only admins", any("только администраторам" in p.get("text", "") for c, p in calls if c == "sendMessage"), [])
ans("Zed", [(iso(today), "win", "50 / 40"), (iso(today), "win", "51 / 41")]); photo(USER2)
r = photo(ADMIN)
dms = [p for c, p in r if c == "sendMessage" and p["chat_id"] in (OWNER, ADMIN)]
check("cheat alert to admins in DM", len(dms) == 2 and all("накрутка" in p["text"] and "совпало 2" in p["text"] and "t.me/c/1/83379/" in p["text"] for p in dms), r)

# ---------- напоминание, итоги недели, варны из ЛС, резервная копия ----------
import datetime as _dt
send(ADMIN, "/setnorm 20")
fri = _dt.datetime.combine(mon + _dt.timedelta(days=4), _dt.time(18, 30))
calls.clear(); bot.scheduler_tick(fri)
posts = [p for c, p in calls if c == "sendMessage"]
check("friday reminder", len(posts) == 1 and "Пока меньше половины" in posts[0]["text"] and posts[0].get("message_thread_id") == 83379
      and "@ernest — " in posts[0]["text"] and "@petya" not in posts[0]["text"], [("sendMessage", p) for p in posts])
check("backup saved", any(c == "bot_state" and p["id"].startswith("backup_") for c, p in calls), [])
calls.clear(); bot.scheduler_tick(fri + _dt.timedelta(hours=1))
check("friday reminder once", not any(c == "sendMessage" for c, _ in calls), [])
nmon = _dt.datetime.combine(mon + _dt.timedelta(days=7), _dt.time(10, 5))
calls.clear(); bot.scheduler_tick(nmon)
posts = [p for c, p in calls if c == "sendMessage"]
summary = [p for p in posts if "Итоги недели" in p["text"]]
dmw = [p for p in posts if p["chat_id"] in (OWNER, ADMIN) and "Не выполнили норму" in p["text"]]
check("monday summary", len(summary) == 1 and "❌" in summary[0]["text"], [("sendMessage", p) for p in posts])
check("warn list in admin DM", len(dmw) == 2 and dmw[0]["reply_markup"]["inline_keyboard"], [("sendMessage", p) for p in posts])
btn = [b[0] for b in dmw[0]["reply_markup"]["inline_keyboard"] if f":{USER2}:" in b[0]["callback_data"]][0]
def press(uid, data):
    calls.clear(); bot.load_state()
    bot.handle_callback({"id": "q", "from": u(uid), "data": data,
                         "message": {"message_id": 5, "chat": {"id": uid}, "reply_markup": dmw[0]["reply_markup"]}})
    bot.save_state(); return calls
before = DB["data"].get("warns", {}).get(f"{CHAT}_{USER2}", 0)
c = press(USER, btn["callback_data"]); check("member cannot warn from DM", any(p.get("show_alert") for m, p in c if m == "answerCallbackQuery"), c)
c = press(ADMIN, btn["callback_data"])
check("admin warns from DM", DB["data"]["warns"].get(f"{CHAT}_{USER2}") == before + 1
      and any(m == "sendMessage" and "норма дуэлей" in p["text"] for m, p in c), c)
c = press(OWNER, btn["callback_data"])
check("no double warn", DB["data"]["warns"].get(f"{CHAT}_{USER2}") == before + 1 and any("уже выдан" in p.get("text", "") for m, p in c if m == "answerCallbackQuery"), c)
calls.clear(); bot.scheduler_tick(nmon + _dt.timedelta(hours=2))
check("summary once", not any(c == "sendMessage" for c, _ in calls), [])

# ---------- профиль участника в ЛС: фото машины, подпись, модерация ----------
def dm(uid, **fields):
    mid[0] += 1
    m = {"message_id": mid[0], "chat": {"id": uid, "type": "private"}, "from": u(uid)}
    m.update(fields)
    calls.clear(); bot.load_state(); bot.handle(m); bot.save_state()
    return list(calls)
def tap(uid, data, chat=None, **msg):
    calls.clear(); bot.load_state()
    bot.handle_callback({"id": "q", "from": u(uid), "data": data,
                         "message": dict({"message_id": 9, "chat": {"id": chat or uid}}, **msg)})
    bot.save_state(); return list(calls)
texts = lambda c: " || ".join((p or {}).get("text", "") or (p or {}).get("caption", "") for m, p in c
                              if m in ("sendMessage", "sendPhoto", "answerCallbackQuery"))
c = dm(USER, text="/myprofile"); check("profile only for table pilots", "таблице пилотов" in texts(c), [])
c = dm(USER2, text="/myprofile")
check("profile menu", "ливрее клуба KHR" in texts(c) and any(m == "sendMessage" and p.get("reply_markup") for m, p in c), [])
c = dm(USER2, photo=[{"file_id": "early"}]); check("photo without request gets hint", "/myprofile" in texts(c), [])
c = tap(USER2, "pf:car"); check("car prompt mentions club livery", "обязательно должна быть в ливрее клуба KHR" in texts(c), [])
c = dm(USER2, photo=[{"file_id": "s"}, {"file_id": "car1"}])
check("photo preview with confirm", any(m == "sendPhoto" and p["photo"] == "car1" and "pf:ok" in json.dumps(p["reply_markup"]) for m, p in c), [])
c = tap(USER2, "pf:ok")
admin_photos = [p for m, p in c if m == "sendPhoto" and p["chat_id"] in (OWNER, ADMIN)]
check("car saved and admins notified", "Готово" in texts(c) and len(admin_photos) == 2
      and admin_photos[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"].startswith(f"pfdel:{USER2}:"), [])
j = bot.duels_json(); check("profile in app data", j["profiles"].get("petya", {}).get("photo", 0) > 0, [("sendMessage", {"text": str(j.get("profiles"))})])
check("photo served by bot", bot.profile_photo("@Petya") == b"\xff\xd8fakejpeg" and bot.profile_photo("nobody") is None, [])
tap(USER2, "pf:about"); c = dm(USER2, text="Тандем-пилот, люблю Ebisu")
check("about saved", "Подпись сохранена" in texts(c) and bot.duels_json()["profiles"]["petya"]["about"] == "Тандем-пилот, люблю Ebisu", [])
c = dm(USER2, text="x" * 150); check("plain text after about is not captured", "Подпись" not in texts(c), [])
tap(USER2, "pf:about"); c = dm(USER2, text="x" * 150)
check("about cut to 100", "обрезал" in texts(c) and len(bot.duels_json()["profiles"]["petya"]["about"]) == 100, [])
c = tap(USER2, "pf:view"); check("view profile", any(m == "sendPhoto" and "Petya" in p["caption"] for m, p in c), [])
del_data = admin_photos[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
c = tap(USER, del_data, chat=USER, caption="фото"); check("member cannot remove photo", any(p.get("show_alert") for m, p in c if m == "answerCallbackQuery"), [])
c = tap(ADMIN, del_data, chat=ADMIN, caption="фото")
check("admin removes photo", bot.profile_photo("petya") is None and any(m == "sendMessage" and p["chat_id"] == USER2 and "ливрее клуба" in p["text"] for m, p in c)
      and any(m == "editMessageCaption" for m, _ in c), [])
c = tap(OWNER, del_data, chat=OWNER, caption="фото"); check("remove twice is harmless", "уже заменили или убрали" in texts(c), [])
r = send(USER2, "/myprofile"); check("group points to DM", any("start=profile" in json.dumps(p.get("reply_markup", {})) for c2, p in r), r)
print("\n".join(checks))
failed = sum(c.startswith("FAIL") for c in checks)
print("FAILED:", failed, "of", len(checks))
if failed:
    sys.exit(1)
