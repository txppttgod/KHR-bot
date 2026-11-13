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
# В старых проверках дуэли с выдуманными соперниками и маленькими баллами — правила нормы проверяются отдельно ниже
bot.MIN_SCORE, bot.CLUB_ONLY = 0, False

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

# ---------- правила нормы: не меньше 100.1 балла и соперник из клуба ----------
bot.MIN_SCORE, bot.CLUB_ONLY = 100.1, True
wk = bot.week_bounds()
bot.load_state(); own0, petya0 = bot.duels_in(CHAT, OWNER, *wk), bot.duels_in(CHAT, USER2, *wk)
ans("Petya", [(iso(today), "win", "150.5 / 0"), (iso(today), "draw", "100 / 100"), (iso(today), "loss", "99.9 / 130")])
r = photo(OWNER); bot.load_state()
check("low scores not counted", "Засчитано новых дуэлей: 1" in said(r) and "не в норму: меньше 100.1 балла" in said(r)
      and bot.duels_in(CHAT, OWNER, *wk) == own0 + 1, r)
check("opponent counted only where he scored", "засчитана и сопернику" in said(r) and "сопернику не в норму" in said(r)
      and bot.duels_in(CHAT, USER2, *wk) == petya0 + 1, r)
ans("Stranger", [(iso(today), "win", "180 / 170")]); r = photo(OWNER); bot.load_state()
check("only club opponents", "соперник не из клуба" in said(r) and "Засчитано новых дуэлей: 0" in said(r)
      and bot.duels_in(CHAT, OWNER, *wk) == own0 + 1, r)
check("100.1 exactly counts", not bot.why_not_counted({"s": "100.1 / 0", "o": "Petya"}) and bot.why_not_counted({"s": "100.0 / 0", "o": "Petya"}), [])
r = send(OWNER, "/norm"); check("norm shows why", "✗ не в норму" in said(r), r)
check("sheet shows why", any(row[-1].startswith("не в норму") for row in bot.sheet_payload(CHAT)["duels"]), [])

# ---------- «дюп»: скрин недоезженной дуэли, потом скрин той же дуэли целиком ----------
bot.load_state(); own1, petya1 = bot.duels_in(CHAT, OWNER, *wk), bot.duels_in(CHAT, USER2, *wk)
GEM["answer"] = {"is_duel_history": True, "opponent": "Petya",
                 "rows": [{"date": iso(today), "result": "win", "score": "171.3 / 52.4", "track": "Ebisu West"}]}
photo(OWNER)
GEM["answer"] = {"is_duel_history": True, "opponent": "Petya",
                 "rows": [{"date": iso(today), "result": "win", "score": "171.3 / 140.8", "track": "Ebisu West"}]}
r = photo(OWNER); bot.load_state()
check("partial then full duel counted once", "Засчитано новых дуэлей: 0" in said(r) and "счёт обновлён" in said(r)
      and bot.duels_in(CHAT, OWNER, *wk) == own1 + 1 and bot.duels_in(CHAT, USER2, *wk) == petya1 + 1, r)
check("score updated for both", any(d["s"] == "171.3 / 140.8" for d in bot.duels[CHAT][OWNER])
      and any(d["s"] == "140.8 / 171.3" and d.get("by") == OWNER for d in bot.duels[CHAT][USER2]), [])
GEM["answer"] = {"is_duel_history": True, "opponent": "Ernest",
                 "rows": [{"date": iso(today), "result": "loss", "score": "140.8 / 171.3", "track": "Ebisu West"}]}
r = photo(USER2); bot.load_state()
check("opponent's full screen not counted again", "Засчитано новых дуэлей: 0" in said(r) and bot.duels_in(CHAT, USER2, *wk) == petya1 + 1, r)
GEM["answer"] = {"is_duel_history": True, "opponent": "Petya",
                 "rows": [{"date": iso(today), "result": "win", "score": "171.3 / 120.5", "track": "Springstone"}]}
r = photo(OWNER); bot.load_state()
check("different track is a different duel", "Засчитано новых дуэлей: 1" in said(r), r)
GEM["answer"] = {"is_duel_history": True, "opponent": "Petya",
                 "rows": [{"date": iso(today), "result": "win", "score": "130.2 / 100", "track": "Ebisu East"},
                          {"date": iso(today), "result": "win", "score": "130.2 / 115.5", "track": "Ebisu East"}]}
r = photo(OWNER)
check("two rows of one screen stay separate", "Засчитано новых дуэлей: 2" in said(r), r)

# ---------- популярные карты ----------
mp = lambda rows: {r[0]: r[1] for r in rows}
bot.load_state(); bot.tracks.clear(); bot.save_state(); bot.load_state()
check("all-time map counter filled from saved duels", mp(bot.tracks_top(CHAT)).get("Ebisu West") == 1, [])
GEM["answer"] = {"is_duel_history": True, "opponent": "Petya",
                 "rows": [{"date": iso(today), "result": "win", "score": "120.1 / 110.2", "track": "Fuji 2"},
                          {"date": iso(today), "result": "win", "score": "125.5 / 111.1", "track": "fuji  2"}]}
photo(OWNER); fuji_group = str(mid[0]); bot.load_state()
week_maps = mp(bot.tracks_top(CHAT, *wk)); all_maps = mp(bot.tracks_top(CHAT))
check("maps of the week count each duel once", week_maps.get("Fuji 2") == 2, [("sendMessage", {"text": str(week_maps)})])
check("map names merged regardless of case and spaces", all_maps.get("Fuji 2") == 2 and "fuji  2" not in all_maps, [("sendMessage", {"text": str(all_maps)})])
j = bot.duels_json()
check("maps in app data", ["Fuji 2", 2] in [r[:2] for r in j["tracks"]["week"]] and any(r[0] == "Fuji 2" for r in j["tracks"]["all"]), [("sendMessage", {"text": str(j["tracks"])})])
cb(OWNER, f"du:{OWNER}:{fuji_group}"); bot.load_state()
check("undo removes maps too", "Fuji 2" not in mp(bot.tracks_top(CHAT)) and "Fuji 2" not in mp(bot.tracks_top(CHAT, *wk)), [])

# ---------- логотипы карт: только владелец, через ЛС бота ----------
c = dm(ADMIN, text="/maplogo"); check("only owner sets map logos", "только владелец" in texts(c), [])
c = dm(OWNER, text="/maplogo")
menu = [p for m, p in c if m == "sendMessage" and p.get("reply_markup")]
check("map list for owner", menu and any("Ebisu West" in b[0]["text"] for b in menu[0]["reply_markup"]["inline_keyboard"]), [])
idx = next(i for i, b in enumerate(menu[0]["reply_markup"]["inline_keyboard"]) if "Ebisu West" in b[0]["text"])
c = tap(ADMIN, f"ml:{idx}"); check("admin cannot press logo buttons", any(p.get("show_alert") for m, p in c if m == "answerCallbackQuery"), [])
c = tap(OWNER, f"ml:{idx}"); check("asks for logo picture", "Пришли картинку-логотип" in texts(c) and "Ebisu West" in texts(c), [])
c = dm(OWNER, photo=[{"file_id": "s"}, {"file_id": "logo1"}]); bot.load_state()
row = next(r for r in bot.tracks_top(CHAT) if r[0] == "Ebisu West")
check("logo saved", "Логотип для карты «Ebisu West» поставлен" in texts(c) and row[2] > 0 and row[3] == "ebisu west", [])
check("logo served by bot", bot.map_logo("ebisu west") == b"\xff\xd8fakejpeg" and bot.map_logo("nope") is None, [])
check("logo in app data", any(r[0] == "Ebisu West" and r[2] > 0 for r in bot.duels_json()["tracks"]["all"]), [])
c = dm(OWNER, text="/maplogo"); menu = [p for m, p in c if m == "sendMessage" and p.get("reply_markup")]
check("logo marked in list", any(b[0]["text"].startswith("🖼 Ebisu West") for b in menu[0]["reply_markup"]["inline_keyboard"]), [])
tap(OWNER, f"ml:{idx}"); c = tap(OWNER, f"mlx:{idx}"); bot.load_state()
check("logo removed", "убран" in texts(c) and bot.map_logo("ebisu west") is None, [])
c = dm(OWNER, photo=[{"file_id": "x"}]); check("after removal photo is not taken as logo", "Логотип" not in texts(c), [])

# ---------- статистика пилота, награды недели, короли карт, серии, соперничества ----------
bot.load_state(); j = bot.duels_json(); st = j["pilots"]["ernest"]["stats"]
check("pilot stats in app data", st["n"] >= 5 and st["w"] >= 5 and st["best"] == 171.3 and st["map"]
      and st["rivals"][0][0] == "Petya", [("sendMessage", {"text": str(st)})])
aw = j["awards"]["week"]
check("award: most active", aw["active"][0] == "Ernest", [("sendMessage", {"text": str(aw)})])
check("award: record", aw["record"][:2] == ["Ernest", 171.3], [("sendMessage", {"text": str(aw)})])
check("award: closest duel only where both really drove", aw["closest"][:2] == ["Ernest", "Petya"] and aw["closest"][4] == 14.7,
      [("sendMessage", {"text": str(aw)})])
check("no sniper below minimum duels", "sniper" not in aw, [])
bot.AWARD_MIN = 3
aw3 = bot.week_awards(CHAT, *wk)
check("award: sniper", aw3["sniper"][:2] == ["Ernest", 100], [("sendMessage", {"text": str(aw3)})])
bot.AWARD_MIN = 10
check("map king", j["tracks"]["kings"].get("ebisu west", [""])[0] == "Ernest", [("sendMessage", {"text": str(j["tracks"]["kings"])})])
riv = j["rivalries"][0]
mine_vs = [d for d in bot.counted(CHAT, OWNER, bot.club_names()) if d["o"] == "Petya"]
theirs_vs = [d for d in bot.counted(CHAT, USER2, bot.club_names()) if d["o"] == "Ernest"]
check("rivalry counts each duel once", riv[:2] == ["Ernest", "Petya"] and max(len(mine_vs), len(theirs_vs)) <= riv[4] < len(mine_vs) + len(theirs_vs)
      and riv[2] + riv[3] <= riv[4] and riv[2] > riv[3], [("sendMessage", {"text": str(j["rivalries"])})])
check("streak", bot.norm_streak(CHAT, OWNER, 1) == 1 and bot.norm_streak(CHAT, OWNER, 50) == 0
      and "streak" in j["pilots"]["ernest"], [])

# ---------- четверг: напоминание в ЛС тем, кто не добрал норму ----------
thu = _dt.datetime.combine(mon + _dt.timedelta(days=3), _dt.time(18, 30))
bot.load_state(); bot.settings[CHAT].pop("nudged", None); bot.save_state()
calls.clear(); bot.scheduler_tick(thu)
nudges = [p for c, p in calls if c == "sendMessage" and "осталось" in p["text"] and p["chat_id"] in (OWNER, USER2)]
check("thursday DM nudge", len(nudges) == 2 and "из 20 дуэлей" in nudges[0]["text"], [("sendMessage", p) for c, p in calls if c == "sendMessage"])
calls.clear(); bot.scheduler_tick(thu + _dt.timedelta(hours=1))
check("nudge once a week", not any(c == "sendMessage" and "осталось" in p["text"] for c, p in calls), [])

# ---------- понедельник: награды в итогах и список неактивных админам ----------
calls.clear(); bot.load_state(); bot.monday_summary(CHAT, _dt.datetime.combine(mon + _dt.timedelta(days=7), _dt.time(10, 5)))
posts = [p for c, p in calls if c == "sendMessage"]
check("awards in monday summary", any("🏆 Награды недели" in p["text"] and "⚔️ Дуэль недели — Ernest vs Petya" in p["text"] for p in posts),
      [("sendMessage", p) for p in posts])
check("active pilots not in idle list", not any("Две недели подряд" in p["text"] and ("Ernest" in p["text"] or "Petya" in p["text"]) for p in posts),
      [("sendMessage", p) for p in posts])
calls.clear(); bot.monday_summary(CHAT, _dt.datetime.combine(mon + _dt.timedelta(days=28), _dt.time(10, 5)))
idle = [p for c, p in calls if c == "sendMessage" and "Две недели подряд" in p["text"]]
check("idle pilots to admins in DM", len(idle) == 2 and "Ernest" in idle[0]["text"] and all(p["chat_id"] in (OWNER, ADMIN) for p in idle),
      [("sendMessage", p) for c, p in calls if c == "sendMessage"])

# ---------- плохо читаемый скрин: просим прислать файлом ----------
GEM["answer"] = {"is_duel_history": True, "opponent": "Petya", "unreadable": True,
                 "rows": [{"date": iso(today), "result": "win", "score": "140.4 / 120.1", "track": "Dominion 2"}]}
r = photo(OWNER); check("blurry screen asks for file", "Плохо читается" in said(r) and "файлом" in said(r), r)
GEM["answer"]["unreadable"] = False; GEM["answer"]["rows"][0]["score"] = "141.4 / 120.1"
r = photo(OWNER); check("clear screen has no warning", "Плохо читается" not in said(r), r)
check("tiny compressed photo detected", bot.small_photo({"photo": [{"width": 90, "height": 40}, {"width": 960, "height": 443}]})
      and not bot.small_photo({"photo": [{"width": 1280, "height": 591}]}) and not bot.small_photo({"document": {}}), [])

# ---------- мастерская ливрей: владелец назначает ливрейщика, тот грузит ливреи в ЛС ----------
import hmac, hashlib, time, urllib.parse
btns = lambda c: [b for m, p in c for row in (p or {}).get("reply_markup", {}).get("inline_keyboard", []) for b in row]
c = dm(USER, text="/livery"); check("livery only for designers", "только ливрейщики" in texts(c), [])
c = dm(ADMIN, text="/designer @ivan"); check("only owner appoints designers", "только владелец" in texts(c), [])
r = send(OWNER, "/designer @nobody"); check("unknown designer", "Не нашёл" in said(r), r)
r = send(OWNER, "/designer @ivan")
check("owner appoints designer", "теперь ливрейщик" in said(r) and any(p["chat_id"] == USER and "/livery" in p["text"] for c2, p in r if c2 == "sendMessage"), r)
c = dm(USER, text="/livery"); check("designer menu", "мастерская ливрей" in texts(c).lower() and any(b["callback_data"] == "lv:add" for b in btns(c)), [])
c = tap(USER, "lv:add"); check("asks for livery photos", "до 5 скринов" in texts(c), [])
c = dm(USER, photo=[{"file_id": "s"}, {"file_id": "lv1"}])
check("photo counter with done button", "Фото: 1 из 5" in texts(c) and any(b["callback_data"] == "lv:done" for b in btns(c)), [])
c = dm(USER, photo=[{"file_id": "lv1b"}])
check("album photos update one message", any(m == "editMessageText" and "Фото: 2 из 5" in p["text"] for m, p in c), [])
c = tap(USER, "lv:done"); check("asks for description", "опиши ливрею" in texts(c).lower(), [])
c = dm(USER, text="Неоновая KHR на S15"); check("asks for whom", "Для кого" in texts(c), [])
c = dm(USER, text="@petya"); bot.load_state()
check("livery saved with publish button", "добавлена в портфолио" in texts(c) and any(b["callback_data"].startswith("lv:pub:") for b in btns(c)), [])
car_btn = next((b["callback_data"] for m, p in c if m == "sendPhoto" and p["chat_id"] == USER2 for b in btns([(m, p)])), "")
owner_btn = next((b["callback_data"] for m, p in c if m == "sendPhoto" and p["chat_id"] == OWNER for b in btns([(m, p)])), "")
check("pilot gets livery made for him", car_btn.startswith("lvcar:"), [])
check("owner notified about new livery", owner_btn.startswith("lvrm:"), [])
des = [d for d in bot.liveries_json() if d["tg"] == "ivan"]
it0 = des[0]["items"][0] if des else {}
check("livery in app data", it0.get("t") == "Неоновая KHR на S15" and it0.get("for") == "Petya" and it0.get("n") == 2
      and "fs" not in it0 and "f" not in it0, [("sendMessage", {"text": str(des)})])
lid = it0.get("id")
check("livery photos served", bot.livery_photo_bytes(lid) == b"\xff\xd8fakejpeg" and bot.livery_photo_bytes(f"{lid}-1") == b"\xff\xd8fakejpeg"
      and bot.livery_photo_bytes(f"{lid}-2") is None and bot.livery_photo_bytes(1) is None, [])
check("designers in duels json", any(d["tg"] == "ivan" for d in bot.duels_json()["designers"]), [])
# пилот ставит ливрею в профиль
c = tap(ADMIN, car_btn); check("only that pilot can use livery as car", any(p.get("show_alert") for m, p in c if m == "answerCallbackQuery"), [])
c = tap(USER2, car_btn); bot.load_state()
check("livery set as car photo", bot.profiles[USER2].get("car") == "lv1" and any(m == "sendPhoto" and "новое фото машины" in p["caption"] for m, p in c), [])
# публикация в чат клуба, в выбранной теме
r = send(ADMIN, "/liveryhere", message_thread_id=555); check("liveryhere", "в этой теме" in said(r), r)
c = tap(USER, f"lv:pub:{lid}")
check("livery posted to club chat", any(m == "sendMediaGroup" and p["chat_id"] == CHAT and len(p["media"]) == 2 and p.get("message_thread_id") == 555
                                          for m, p in c), [])
c = tap(USER, f"lv:pub:{lid}"); check("posted only once", any(p.get("show_alert") for m, p in c if m == "answerCallbackQuery"), [])
# вторая ливрея — ни для кого
tap(USER, "lv:add"); dm(USER, photo=[{"file_id": "lv2"}]); tap(USER, "lv:done"); dm(USER, text="Ливрея клуба")
c = tap(USER, "lv:skip"); bot.load_state()
items = [d for d in bot.liveries_json() if d["tg"] == "ivan"][0]["items"]
check("livery for nobody", "добавлена" in texts(c) and items[0]["t"] == "Ливрея клуба" and items[0]["for"] == "", [])
lid2 = items[0]["id"]
c = dm(USER, text="обычный текст"); check("plain text after save not captured", "добавлена" not in texts(c), [])
# редактирование
c = tap(USER, "lv:list"); check("livery list", any(b["callback_data"] == f"lv:item:{lid}" for b in btns(c)), [])
c = tap(USER, f"lv:item:{lid}"); check("livery item menu", any(b["callback_data"] == f"lv:et:{lid}" for b in btns(c)), [])
tap(USER, f"lv:et:{lid}"); c = dm(USER, text="Неон S15 v2"); bot.load_state()
check("edit description", "обновлено" in texts(c) and bot.find_livery(lid)[1]["t"] == "Неон S15 v2", [])
tap(USER, f"lv:ef:{lid}"); c = dm(USER, text="-"); bot.load_state()
check("edit for whom", bot.find_livery(lid)[1]["for"] == "", [])
tap(USER, f"lv:ep:{lid}"); dm(USER, photo=[{"file_id": "lv9"}]); c = tap(USER, f"lv:epd:{lid}"); bot.load_state()
check("replace photos", "заменены" in texts(c) and bot.find_livery(lid)[1]["fs"] == ["lv9"], [])
# лайки из приложения: только с настоящей подписью Телеграма
def init_for(uid, bad=False):
    pairs = {"auth_date": str(int(time.time())), "user": json.dumps({"id": uid, "first_name": "x"})}
    secret = hmac.new(b"WebAppData", b"1:x", hashlib.sha256).digest()
    h = hmac.new(secret, "\n".join(f"{k}={v}" for k, v in sorted(pairs.items())).encode(), hashlib.sha256).hexdigest()
    return urllib.parse.urlencode(dict(pairs, hash="0" * 64 if bad else h))
bot.load_state(); res = bot.like_livery(init_for(USER2), lid); bot.save_state()
check("like", res == {"ok": True, "n": 1, "liked": True}, [("sendMessage", {"text": str(res)})])
check("fake like rejected", not bot.like_livery(init_for(USER2, bad=True), lid)["ok"] and not bot.like_livery("", lid)["ok"], [])
check("own livery not liked", not bot.like_livery(init_for(USER), lid)["ok"], [])
res = bot.like_livery(init_for(USER2), lid); res2 = bot.like_livery(init_for(USER2), lid); bot.save_state()
check("like toggles", res["n"] == 0 and not res["liked"] and res2["n"] == 1, [])
check("likes in app data", [d for d in bot.liveries_json() if d["tg"] == "ivan"][0]["items"][1]["likes"] == 1, [])
calls.clear(); bot.load_state(); bot.livery_of_week(CHAT, _dt.datetime.combine(mon + _dt.timedelta(days=7), _dt.time(10, 5)))
check("livery of the week", any(m == "sendPhoto" and "Ливрея недели" in p["caption"] and p.get("message_thread_id") == 555 for m, p in calls),
      [(m, p) for m, p in calls if m in ("sendPhoto", "sendMessage")])
calls.clear(); bot.livery_of_week(CHAT, _dt.datetime.combine(mon + _dt.timedelta(days=21), _dt.time(10, 5)))
check("no livery of the week without likes", not any(m in ("sendPhoto", "sendMediaGroup") for m, p in calls), [])
# заказ ливреи
c = dm(USER2, text="/order"); check("order lists designers", any(b["callback_data"] == f"lvo:to:{USER}" for b in btns(c)), [])
c = tap(USER2, f"lvo:to:{USER}"); check("order asks details", "Опиши" in texts(c), [])
c = dm(USER2, text="S15, чёрно-красная, к пятнице")
ok_btn = next((b["callback_data"] for m, p in c if m == "sendMessage" and p["chat_id"] == USER for b in btns([(m, p)]) if b["callback_data"].startswith("lvo:ok:")), "")
check("order delivered to designer", ok_btn and "Заказ отправлен" in texts(c) and any("к пятнице" in p.get("text", "") for m, p in c if p.get("chat_id") == USER), [])
c = tap(MOD, ok_btn); check("only that designer answers order", any(p.get("show_alert") for m, p in c if m == "answerCallbackQuery"), [])
c = tap(USER, ok_btn); check("designer takes order", any(p.get("chat_id") == USER2 and "взял(а) твой заказ" in p.get("text", "") for m, p in c), [])
c = tap(USER, ok_btn); check("order answered once", "уже ответил" in texts(c), [])
c = dm(USER2, text="/start order_ivan"); check("order deep link from app", "Опиши" in texts(c), [])
c = dm(USER2, text="Ещё одна")
no_btn = next((b["callback_data"] for m, p in c for b in btns([(m, p)]) if b["callback_data"].startswith("lvo:no:")), "")
c = tap(USER, no_btn); check("designer declines order", any(p.get("chat_id") == USER2 and "не может" in p.get("text", "") for m, p in c), [])
c = dm(USER, text="/start order_ivan"); check("cannot order from yourself", "Себе" in texts(c), [])
# владелец убирает ливрею
c = tap(ADMIN, owner_btn); check("only owner removes liveries", any(p.get("show_alert") for m, p in c if m == "answerCallbackQuery"), [])
c = tap(OWNER, owner_btn, caption="ливрея"); bot.load_state()
check("owner removes livery", bot.find_livery(lid)[1] is None and any(p.get("chat_id") == USER and "убрал ливрею" in p.get("text", "") for m, p in c), [])
c = tap(USER, f"lv:del:{lid2}"); bot.load_state()
check("livery deleted", "удалена" in texts(c) and not [d for d in bot.liveries_json() if d["tg"] == "ivan"][0]["items"], [])
c = tap(ADMIN, "lv:add"); check("non-designer cannot press livery buttons", any(p.get("show_alert") for m, p in c if m == "answerCallbackQuery"), [])
r = send(OWNER, "/designer"); check("designer list", "Иван" in said(r), r)
r = send(OWNER, "/designer @ivan off"); bot.load_state()
check("designer removed", "Снял" in said(r) and not any(d["tg"] == "ivan" for d in bot.liveries_json()), r)
CSV["text"] = "№,Имя,Телеграм\n1,Ernest,@ernest,0,,,Пилот\n2,Petya,@petya,0,,,Дизайнер ливрей\n"; bot._cache["time"] = 0
check("designer by table role", bot.is_designer(u(USER2)) and not bot.is_designer(u(USER)), [])
CSV["text"] = "№,Имя,Телеграм\n1,Ernest,@ernest,0,,,Пилот\n2,Petya,@petya,0,,,Пилот\n"; bot._cache["time"] = 0

# ---------- мини-УТС: турниры без судей ----------
CSV["text"] = ("№,Имя,Телеграм\n1,Ernest,@ernest,0,,,Пилот\n2,Petya,@petya,0,,,Пилот\n3,Ivan,@ivan,0,,,Пилот\n"
               "4,Danya,@danya,0,,,Пилот\n5,Nik,@moxitowo,0,,,Пилот\n"); bot._cache["time"] = 0
names[6] = ("Гость", "guest")
bot.random.seed(7)
gname = {OWNER: "Ernest", USER2: "Petya", USER: "Ivan", MOD: "Danya", ADMIN: "Nik"}
def T(tid):
    bot.load_state(); return bot.uts_all().get(tid)
def M(tid, mid):
    return next(m for m in T(tid)["matches"] if m["id"] == mid)
def tick(dt_min):
    calls.clear(); bot.load_state(); bot.uts_tick(time.time() + dt_min * 60); bot.save_state(); return list(calls)
sc = [150.0]
def shot(uid, opp, wins):
    # Скрин «История заездов»: по строке на заезд, True — заезд выиграл тот, кто кидает скрин
    rows = []
    for w in wins:
        sc[0] += 1.3
        a, b = (round(sc[0] + 10, 1), round(sc[0], 1)) if w else (round(sc[0], 1), round(sc[0] + 10, 1))
        rows.append({"date": iso(today), "result": "win" if w else "loss", "score": f"{a} / {b}"})
    GEM["answer"] = {"is_duel_history": True, "opponent": gname[opp], "rows": rows}
    return photo(uid)
posts = lambda c: " || ".join(p.get("text", "") for m, p in c if m == "sendMessage" and p.get("chat_id") == CHAT)

r = send(USER, "/uts"); check("uts in group points to DM", any("start=uts" in json.dumps(p.get("reply_markup", {})) for c2, p in r), r)
c = dm(6, text="/uts"); check("uts only for table pilots", "пилоты из таблицы" in texts(c), [])
c = dm(USER, text="/uts"); check("uts wizard", "Новый мини-УТС" in texts(c) and any(b["callback_data"] == "ut:d:ok" for b in btns(c)), [])
c = tap(USER, "ut:d:size"); check("wizard size", any(m == "editMessageText" and "топ-8" in p["text"] for m, p in c), [])
tap(USER, "ut:d:size"); tap(USER, "ut:d:size"); c = tap(USER, "ut:d:size")
check("wizard size cycles", any(m == "editMessageText" and "Размер: топ-4" in p["text"] for m, p in c), [])
tap(USER, "ut:d:title"); c = dm(USER, text="Ночной УТС"); check("wizard title", "Название: Ночной УТС" in texts(c), [])
tap(USER, "ut:d:time"); c = dm(USER, text="25:00"); check("wizard bad time", "Не понял время" in texts(c), [])
c = dm(USER, text="23:30"); check("wizard own time", "Старт: в 23:30 МСК" in texts(c), [])
c = tap(USER, "ut:d:start"); check("wizard start back to now", any(m == "editMessageText" and "как наберёмся" in p["text"] for m, p in c), [])
c = tap(USER, "ut:d:ok")
ann = [p for m, p in c if m == "sendMessage" and p.get("chat_id") == CHAT]
check("tournament announced in chat", ann and "Ночной УТС" in ann[0]["text"] and ann[0].get("message_thread_id") == 83379
      and any(b["callback_data"].startswith("ut:j:") for b in btns([("x", ann[0])])) and "1. Ivan" in ann[0]["text"], [("sendMessage", a) for a in ann])
bot.load_state(); tid = next(iter(bot.uts_all()))
check("creator registered", T(tid)["players"] == [USER] and T(tid)["state"] == "reg", [])
c = tap(OWNER, f"ut:j:{tid}"); check("join", "Ты в турнире" in texts(c) and any(m == "editMessageText" and "2. Ernest" in p["text"] for m, p in c), [])
c = tap(OWNER, f"ut:j:{tid}"); check("join twice", "уже записан" in texts(c), [])
c = tap(6, f"ut:j:{tid}"); check("guest cannot join", "только пилоты из таблицы" in texts(c), [])
tap(USER2, f"ut:j:{tid}"); c = tap(USER2, f"ut:x:{tid}"); check("leave", "вышел" in texts(c) and USER2 not in T(tid)["players"], [])
c = dm(OWNER, text="/uts"); check("one tournament at a time", "уже в турнире" in texts(c), [])
c = tap(ADMIN, f"ut:e:{tid}"); check("admin can edit too", "Изменение турнира" in texts(c), [])
dm(ADMIN, text="/myprofile")  # закрыли чужое окно настроек — не мешает
c = tap(MOD, f"ut:e:{tid}"); check("only organizer edits", any(p.get("show_alert") for m, p in c if m == "answerCallbackQuery"), [])
res = bot.uts_api(init_for(USER2), tid, "join"); bot.save_state()
check("join from app", res["ok"] and USER2 in T(tid)["players"], [("sendMessage", {"text": str(res)})])
check("fake app join rejected", not bot.uts_api(init_for(USER2, bad=True), tid, "join")["ok"], [])
c = tap(MOD, f"ut:j:{tid}")
t = T(tid)
check("full tournament starts", t["state"] == "run" and "стартует" in posts(c) and "⚔️" in posts(c), [(m, p) for m, p in c if m == "sendMessage"])
check("double elimination bracket for 4", sorted(m["id"] for m in t["matches"]) == ["GF", "L1-1", "L2-1", "U1-1", "U1-2", "U2-1"]
      and M(tid, "GF")["bo"] == 3, [("sendMessage", {"text": str([m["id"] for m in t["matches"]])})])
m1 = M(tid, "U1-1"); m2 = M(tid, "U1-2")
norm0 = bot.duels_in(CHAT, m1["pa"], *wk)
c = shot(m1["pa"], m1["pb"], [True]); bot.load_state()
check("screenshot decides match by score", M(tid, "U1-1")["w"] == m1["pa"] and "побеждает" in posts(c)
      and bot.duels_in(CHAT, m1["pa"], *wk) == norm0 + 1, [(m, p) for m, p in c if m == "sendMessage"])
c = shot(m1["pb"], m1["pa"], [False]); check("opponent's same screenshot changes nothing", "побеждает" not in posts(c), [])
c = tap(6, f"ut:s:{tid}:U1-2"); check("only players can surrender", any(p.get("show_alert") for m, p in c if m == "answerCallbackQuery"), [])
c = tap(m2["pb"], f"ut:s:{tid}:U1-2")
check("surrender", M(tid, "U1-2")["w"] == m2["pa"] and "сдался" in posts(c), [])
uf, l1 = M(tid, "U2-1"), M(tid, "L1-1")
check("upper final and lower bracket open", uf["open"] and l1["open"] and {l1["pa"], l1["pb"]} == {m1["pb"], m2["pb"]}, [])
GEM["answer"] = {"is_duel_history": True, "opponent": gname[uf["pb"]], "rows": [{"date": iso(today), "result": "draw", "score": "177.7 / 177.7"}]}
c = photo(uf["pa"]); check("tie means re-ride", "ничья" in posts(c) and not M(tid, "U2-1")["done"], [])
c = shot(uf["pb"], uf["pa"], [True]); check("winner by score from either side", M(tid, "U2-1")["w"] == uf["pb"], [])
c = tick(21); check("match reminder", "ждём скрин" in posts(c), [])
c = tick(31)
l1 = M(tid, "L1-1")
check("technical win to higher seed", l1["done"] and l1["w"] == min((l1["pa"], l1["pb"]), key=T(tid)["seeds"].index) and "техническая" in posts(c), [])
l2 = M(tid, "L2-1")
check("lower final: lower winner vs upper final loser", {l2["pa"], l2["pb"]} == {l1["w"], uf["pa"]}, [])
shot(l2["pa"], l2["pb"], [True])
gf = M(tid, "GF")
check("grand final", {gf["pa"], gf["pb"]} == {uf["pb"], l2["pa"]}, [])
c = shot(gf["pa"], gf["pb"], [True])
check("final best of 3 continues", not M(tid, "GF")["done"] and "следующий заезд" in posts(c), [])
c = shot(gf["pb"], gf["pa"], [False])
t = T(tid)
check("champion and places", t["state"] == "done" and t["places"] == [gf["pa"], gf["pb"], l2["pb"]] and "🥇" in posts(c)
      and any(b["callback_data"] == f"ut:r:{tid}" for b in btns(c)), [(m, p) for m, p in c if m == "sendMessage"])
j = bot.duels_json()["uts"]
check("tournament in app data", j["hist"] and j["hist"][0]["title"] == "Ночной УТС" and j["active"][0]["state"] == "done"
      and sum(v[0] for v in j["trophies"].values()) == 1, [("sendMessage", {"text": str(j)[:300]})])
check("week champion", "Чемпион недели по мини-УТС" in bot.uts_week_champion(CHAT, *wk), [])
c = tap(gf["pa"], f"ut:r:{tid}"); rem = [x for x in bot.uts_all().values() if x["id"] != tid] if T(tid) else []
check("rematch with same players starts", rem and rem[0]["state"] == "run" and sorted(rem[0]["players"]) == sorted(t["seeds"]), [])
bot.load_state(); bot.uts_all().pop(rem[0]["id"], None); bot.save_state()

# автоотмена, «каждый с каждым», автопроходы, старт по времени, чек-ин, запасные
def create(uid, taps=(), title=None):
    dm(uid, text="/uts")
    for x in taps:
        tap(uid, f"ut:d:{x}")
    c = tap(uid, "ut:d:ok"); bot.load_state()
    return max(bot.uts_all().values(), key=lambda x: x["created"])["id"], c
tid2, _ = create(OWNER, ["size"])
c = tick(31); check("auto cancel when nobody joined", T(tid2) is None and any(m == "deleteMessage" for m, p in c)
                     and any(p.get("chat_id") == OWNER and "отменён" in p.get("text", "") for m, p in c), [])
tid3, _ = create(OWNER, ["size", "short"])
for x in (USER, USER2, MOD):
    tap(x, f"ut:j:{tid3}")
c = tick(31); t3 = T(tid3)
check("round robin when not full", t3["fmt"] == "rr" and len(t3["matches"]) == 6 and "каждый катает с каждым" in posts(c), [])
for m in T(tid3)["matches"][:2]:
    shot(m["pa"], m["pb"], [True])
for m in T(tid3)["matches"][2:]:
    tap(m["pb"], f"ut:s:{tid3}:{m['id']}")
t3 = T(tid3); f = next((m for m in t3["matches"] if m["id"] == "F"), None)
check("round robin final for top 2", f and f["open"] and f["bo"] == 3, [])
tap(f["pb"], f"ut:s:{tid3}:F"); t3 = T(tid3)
check("round robin finished", t3["state"] == "done" and len(t3["places"]) == 3 and t3["places"][0] == f["pa"], [])
bot.load_state(); bot.uts_all().clear(); bot.save_state()
tid4, _ = create(OWNER, ["size"])
for x in (USER, USER2, MOD, ADMIN):
    tap(x, f"ut:j:{tid4}")
tick(31); t4 = T(tid4)
check("byes when not full", t4["fmt"] == "elim" and sum(1 for m in t4["matches"] if m.get("bye") and m["id"].startswith("U1")) == 3
      and t4["seeds"][0] in (M(tid4, "U2-1")["pa"], M(tid4, "U2-1")["pb"]), [])
bot.load_state(); bot.uts_all().clear(); bot.save_state()
tid5, c = create(OWNER, ["start", "start"])
check("scheduled start", T(tid5)["start"] and "Старт: в " in posts(c), [])
for x in (USER, USER2, MOD, ADMIN):
    tap(x, f"ut:j:{tid5}")
check("reserve when full", T(tid5)["reserve"] == [ADMIN] and T(tid5)["state"] == "reg", [])
c = tick(46); check("reminder before start", sum(1 for m, p in c if m == "sendMessage" and "начнётся через 15" in p.get("text", "")) == 4, [])
c = tick(56); check("check-in asked", sum(1 for m, p in c if any(b["callback_data"] == f"ut:c:{tid5}" for b in btns([(m, p)]))) == 4, [])
for x in (OWNER, USER, USER2):
    tap(x, f"ut:c:{tid5}")
c = tick(61); t5 = T(tid5)
check("no check-in -> reserve takes the place", t5["state"] == "run" and MOD not in t5["seeds"] and ADMIN in t5["seeds"]
      and any(p.get("chat_id") == MOD and "не отметился" in p.get("text", "") for m, p in c), [])
bot.load_state(); bot.uts_all().clear(); bot.save_state()
# изменение и удаление организатором
tid6, _ = create(USER)
tap(OWNER, f"ut:j:{tid6}"); tap(USER2, f"ut:j:{tid6}")
tap(USER, f"ut:e:{tid6}"); tap(USER, "ut:d:size"); c = tap(USER, "ut:d:ok")
check("organizer edits tournament", T(tid6)["size"] == 8 and any(p.get("chat_id") == OWNER and "изменён" in p.get("text", "") for m, p in c), [])
tap(USER, f"ut:e:{tid6}"); c = tap(USER, "ut:d:kick")
check("kick list", any(b["callback_data"] == f"ut:k:{tid6}:{OWNER}" for b in btns(c)), [])
c = tap(USER, f"ut:k:{tid6}:{OWNER}"); check("organizer kicks player", OWNER not in T(tid6)["players"], [])
c = tap(USER, "ut:d:del"); check("organizer deletes tournament", T(tid6) is None and any(m == "deleteMessage" for m, p in c), [])
CSV["text"] = "№,Имя,Телеграм\n1,Ernest,@ernest,0,,,Пилот\n2,Petya,@petya,0,,,Пилот\n"; bot._cache["time"] = 0
# ---------- «Ищу дуэль», вызовы, темп нормы, Эло, ачивки ----------
CSV["text"] = ("№,Имя,Телеграм\n1,Ernest,@ernest,0,,,Пилот\n2,Petya,@petya,0,,,Пилот\n3,Ivan,@ivan,0,,,Пилот\n"
               "4,Danya,@danya,0,,,Пилот\n"); bot._cache["time"] = 0
bot.load_state(); bot.settings[CHAT].pop("dz", None); bot.save_state()
check("pace hint", bot.pace(8, 3) == "это по 3 дуэли в день" and bot.pace(5, 1) == "все 5 — сегодня" and bot.pace(0, 3) == ""
      and bot.pace(21, 4) == "это по 6 дуэлей в день" and bot.pace(1, 7) == "это по 1 дуэль в день", [])
r = send(USER, "ищу дуэль")
check("lfg posts in reports thread", any(m == "sendMessage" and p.get("message_thread_id") == 83379 and "ищет дуэль" in p["text"]
      and any(b["callback_data"] == f"dz:go:{USER}" for b in btns([(m, p)])) for m, p in r)
      and not any(p.get("reply_parameters") or p.get("reply_to_message_id") for m, p in r), r)
r = send(USER, "/lfg"); check("lfg twice", "уже ищешь" in said(r), r)
r = send(USER, "ищу дуэль сегодня вечером?"); check("lfg phrase inside sentence ignored", r == [], r)
c = tap(6, f"dz:go:{USER}"); check("guest cannot answer lfg", "только пилоты из таблицы" in texts(c), [])
c = tap(OWNER, f"dz:x:{USER}"); check("only author removes lfg", "только тот, кто" in texts(c), [])
c = tap(OWNER, f"dz:go:{USER}"); bot.load_state()
check("lfg answered -> pair", "Договорились" in texts(c) and any(m == "editMessageText" and "нашёл(ла) соперника: Ernest" in p["text"] for m, p in c)
      and "Пара: @ivan vs @ernest" in posts(c) and any(p.get("chat_id") == USER and "Ernest готов" in p.get("text", "") for m, p in c)
      and not bot.settings[CHAT]["dz"]["lfg"], [])
c = tap(MOD, f"dz:go:{USER}"); check("lfg already taken", "уже нашёл" in texts(c), [])
c = dm(USER, text="/lfg"); c = dm(MOD, text="/start lfg")
check("two lfg -> instant pair", "нашёлся сразу: Ivan" in texts(c) and "Пара: @ivan vs @danya" in posts(c), [])
c = dm(6, text="/lfg"); check("guest lfg rejected", "только пилоты из таблицы" in texts(c), [])
dm(MOD, text="/lfg"); c = tap(MOD, f"dz:x:{MOD}"); check("lfg removed by author", "снято" in texts(c), [])
dm(MOD, text="/lfg"); calls.clear(); bot.load_state(); bot.dz_tick(time.time() + 31 * 60); bot.save_state()
check("lfg expires", any(m == "editMessageText" and "никто не откликнулся" in p["text"] for m, p in calls), [])
# вызовы
r = send(USER, "/call @ernest")
ok_btn = next((b["callback_data"] for b in btns(r) if b["callback_data"].startswith("dz:ok:")), "")
check("call posted with buttons", ok_btn and "@ivan вызывает @ernest" in said(r) and any(p.get("chat_id") == OWNER and "вызывает тебя" in p.get("text", "") for m, p in r), r)
r = send(USER, "/call @ernest"); check("call twice", "уже вызвал" in said(r), r)
r = send(USER, "/call @ivan"); check("call yourself", "Себя" in said(r), r)
r = send(USER, "/call"); check("call needs target", "Кого вызываем" in said(r), r)
c = tap(MOD, ok_btn); check("only target answers call", "Ответить на вызов может только Ernest" in texts(c), [])
c = tap(OWNER, ok_btn)
check("call accepted", any(m == "editMessageText" and "принял(а) вызов Ivan" in p["text"] for m, p in c)
      and sum(1 for m, p in c if m == "editMessageText") == 2
      and any(p.get("chat_id") == USER and "принял(а) твой вызов" in p.get("text", "") for m, p in c), [])
c = tap(OWNER, ok_btn); check("call answered once", "неактуален" in texts(c), [])
r = send(MOD, "вызов", reply_to=USER); no_btn = next((b["callback_data"] for b in btns(r) if b["callback_data"].startswith("dz:no:")), "")
check("call by word in reply", "@danya вызывает @ivan" in said(r) and no_btn, r)
r = send(MOD, "вызов принят", reply_to=OWNER); check("call word inside sentence ignored", r == [], r)
c = tap(USER, no_btn); check("call declined", any(p.get("chat_id") == MOD and "не может принять" in p.get("text", "") for m, p in c), [])
send(MOD, "/call @ernest"); calls.clear(); bot.load_state(); bot.dz_tick(time.time() + 121 * 60); bot.save_state()
check("call expires", any(m == "editMessageText" and "без ответа" in p["text"] for m, p in calls), [])
# из приложения
bot.load_state(); res = bot.duel_api(init_for(USER), "call", "danya"); bot.save_state()
check("call from app", res["ok"] and res["done"] and "Вызов отправлен" in res["text"], [("sendMessage", {"text": str(res)})])
check("fake app call rejected", not bot.duel_api(init_for(USER, bad=True), "lfg")["ok"], [])
res = bot.duel_api(init_for(OWNER), "lfg"); bot.save_state()
check("lfg from app", res["done"] and bot.duels_json()["lfg"] == ["Ernest"], [("sendMessage", {"text": str(res)})])
# Эло и ачивки
bot.load_state(); bot.duels[CHAT] = {}; bot.settings[CHAT].pop("ach", None); bot.save_state()
shot(USER, OWNER, [True] * 6)
bot.load_state(); elo = bot.elo_table(CHAT)
check("elo: winner up, loser down, duel counted once", elo["ivan"][1] > 1000 > elo["ernest"][1] and elo["ivan"][2] == 6
      and elo["ernest"][2] == 6 and round(elo["ivan"][1] + elo["ernest"][1]) == 2000, [("sendMessage", {"text": str(elo)})])
j = bot.duels_json()
check("elo in app data", j["elo"][0][0] == "Ivan" and j["elo"][0][3] == j["elo"][0][1] - 1000, [("sendMessage", {"text": str(j["elo"])})])
calls.clear(); bot.load_state(); new = bot.ach_update(CHAT); bot.save_state()
check("first achievements run is silent", "first" in dict(bot.ach_json(CHAT)["ivan"]) and (USER, "w5") in new and not posts(calls), [])
shot(USER, MOD, [True, True, True, True]); calls.clear(); bot.load_state(); bot.ach_update(CHAT); bot.save_state()
check("new achievement announced", "10 побед подряд" in posts(calls) and "@ivan" in posts(calls)
      and "5 побед" not in posts(calls), [("sendMessage", {"text": posts(calls)})])
calls.clear(); bot.load_state(); bot.ach_update(CHAT); check("achievement announced once", not posts(calls), [])
j = bot.duels_json()
check("achievements in app data", "w10" in dict(j["ach"]["ivan"]) and j["ach_info"]["w10"][1] == "10 побед подряд", [])
r = send(MOD, "/norm"); check("norm still works", " из " in said(r), r)
print("\n".join(checks))
failed = sum(c.startswith("FAIL") for c in checks)
print("FAILED:", failed, "of", len(checks))
if failed:
    sys.exit(1)
