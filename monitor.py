#!/usr/bin/env python3
"""rknKID монитор: опрашивает источники и шлёт новое в Telegram.

Только стандартная библиотека Python 3.9+. Запуск:
    python3 monitor.py              # один проход (для cron / GitHub Actions)
    python3 monitor.py --dry-run    # ничего не шлёт, печатает в консоль
    python3 monitor.py --loop 300   # крутиться самому, проход раз в 5 минут
Переменные окружения: TG_BOT_TOKEN, TG_CHAT_ID.
"""
import argparse
import datetime as dt
import email.utils
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
KEEP_IDS = 500          # сколько id помнить на источник
FAIL_ALERT_AT = 6
MAX_AGE = 3 * 3600      # старше — не шлём: лента отдала старьё задним числом       # после скольких падений подряд сказать, что источник лежит
LEVEL_MARK = {1: "🔴 У1", 2: "🟡 У2", 3: "⚪ У3"}
TZ = dt.timezone(dt.timedelta(hours=5), "Тюмень")
LOG = os.path.join(HERE, "detections.csv")  # когда появилось в источнике vs когда поймали


def parse_ts(s):
    """RSS-дата, ISO или unix → unix-время; не вышло → None."""
    if not s:
        return None
    try:
        return int(s) if str(s).isdigit() else int(email.utils.parsedate_to_datetime(s).timestamp())
    except (TypeError, ValueError):
        try:
            return int(dt.datetime.fromisoformat(s.strip().replace("Z", "+00:00")).timestamp())
        except ValueError:
            return None


# ---------- сеть ----------

def fetch(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "ru,en;q=0.8"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        charset = r.headers.get_content_charset()
    if not charset:
        m = re.search(rb'charset=["\']?([\w-]+)', raw[:2000])
        charset = m.group(1).decode() if m else "utf-8"
    return raw.decode(charset, errors="replace")


def strip_tags(s):
    s = re.sub(r"<br\s*/?>|</(p|div|li|tr|h\d)>", "\n", s or "", flags=re.I)
    s = re.sub(r"<script.*?</script>|<style.*?</style>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"[ \t\r\f\v]+", " ", html.unescape(s)).strip()


def short(s, n=300):
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


# ---------- типы источников ----------
# Каждый возвращает список dict: {id, title, text, link}

def src_rss(cfg):
    root = ET.fromstring(fetch(cfg["url"]).encode("utf-8"))
    items = []
    for el in root.iter():
        tag = el.tag.split("}")[-1]
        if tag not in ("item", "entry"):
            continue
        f = {c.tag.split("}")[-1]: c for c in el}
        title = (f["title"].text if "title" in f else "") or ""
        link = ""
        if "link" in f:
            link = f["link"].text or f["link"].get("href", "")
        desc = ""
        for k in ("description", "summary", "content"):
            if k in f and f[k].text:
                desc = f[k].text
                break
        guid = (f["guid"].text if "guid" in f else None) or (f["id"].text if "id" in f else None) or link or title
        when = next((f[k].text for k in ("pubDate", "published", "updated", "date") if k in f and f[k].text), None)
        items.append({"id": guid.strip(), "title": strip_tags(title), "text": strip_tags(desc), "link": link.strip(),
                      "ts": parse_ts(when)})
    return items


def src_telegram(cfg):
    ch = cfg["channel"].lstrip("@")
    page = fetch(f"https://t.me/s/{ch}")
    items = []
    for block in page.split('class="tgme_widget_message_wrap')[1:]:
        m = re.search(r'data-post="([^"]+/\d+)"', block)
        if not m:
            continue
        t = re.search(r'tgme_widget_message_text[^>]*>(.*?)</div>', block, re.S)
        text = strip_tags(t.group(1)) if t else "(медиа без текста)"
        w = re.search(r'<time[^>]+datetime="([^"]+)"', block)
        items.append({"id": m.group(1), "title": short(text, 120), "text": text, "link": f"https://t.me/{m.group(1)}",
                      "ts": parse_ts(w and w.group(1))})
    if not items:
        raise ValueError(f"t.me/s/{ch}: постов не видно (канал не найден или закрыт превью)")
    return items


def src_page(cfg):
    """Дифф страницы. С pattern: каждое совпадение регулярки = пункт.
    Без pattern: строки видимого текста, новые строки = пункты."""
    page = fetch(cfg["url"])
    if cfg.get("pattern"):
        items = []
        for m in re.finditer(cfg["pattern"], page, re.S):
            d = m.groupdict()
            title = strip_tags(d.get("title") or m.group(0))
            link = urllib.parse.urljoin(cfg["url"], html.unescape(d["link"])) if d.get("link") else cfg["url"]
            items.append({"id": d.get("id") or (link if d.get("link") else title), "title": title, "text": "", "link": link})
        return items
    lines = [l.strip() for l in strip_tags(page).split("\n")]
    text = re.sub(r"\s{2,}", "\n", "\n".join(lines))
    return [{"id": hashlib.sha1(l.encode()).hexdigest()[:16], "title": short(l, 200), "text": "", "link": cfg["url"]}
            for l in text.split("\n") if len(l) >= cfg.get("min_len", 25)]


def src_ioda(cfg):
    now = int(time.time())
    q = urllib.parse.urlencode({"entityType": cfg.get("entity_type", "country"),
                                "entityCode": cfg.get("entity_code", "RU"),
                                "from": now - 6 * 3600, "until": now})
    data = json.loads(fetch(f"https://api.ioda.inetintel.cc.gatech.edu/v2/outages/alerts?{q}"))
    items = []
    for a in data.get("data") or []:
        if a.get("level") == "normal":
            continue
        ent = a.get("entity") or {}
        when = time.strftime("%d.%m %H:%M UTC", time.gmtime(a.get("time", 0)))
        title = f"IODA: {ent.get('name', '?')} — {a.get('datasource')} {a.get('level')} ({when})"
        items.append({"id": f"{ent.get('code')}-{a.get('datasource')}-{a.get('time')}", "title": title,
                      "text": f"значение {a.get('value')} при норме {a.get('historyValue')}",
                      "link": f"https://ioda.inetintel.cc.gatech.edu/{ent.get('type', 'country')}/{ent.get('code', 'RU')}",
                      "ts": a.get("time")})
    return items


def src_yandex_artist(cfg):
    """Треки артиста на Яндекс Музыке. Пункт = трек; пропал или стал недоступен = новость.
    Работает только с российского IP. Неофициальный API, может сломаться."""
    items = []
    for aid in list(cfg.get("artists", [])) + [resolve_artist(n, cfg["_cache"]) for n in cfg.get("names", [])]:
        if not aid:
            continue
        data = json.loads(fetch(f"https://api.music.yandex.net/artists/{aid}/tracks?page-size=500"))
        for t in (data.get("result") or {}).get("tracks") or []:
            if t.get("available", True):
                artists = ", ".join(a.get("name", "") for a in t.get("artists") or [])
                items.append({"id": str(t["id"]), "title": f"{artists} — {t.get('title')}", "text": "",
                              "link": f"https://music.yandex.ru/track/{t['id']}"})
    return items


def resolve_artist(name, cache):
    """Имя → id через поиск Яндекс Музыки, кэш в state.json. Точное совпадение имени важнее первого результата."""
    if name not in cache:
        q = urllib.parse.urlencode({"text": name, "type": "artist", "page": 0})
        res = (json.loads(fetch(f"https://api.music.yandex.net/search?{q}")).get("result") or {})
        found = (res.get("artists") or {}).get("results") or []
        best = next((a for a in found if a.get("name", "").lower() == name.lower()), found[0] if found else None)
        cache[name] = best and best["id"]
        print(f"[yandex] {name} → {best and best.get('name')} ({cache[name]})")
    return cache[name]


SOURCES = {"rss": src_rss, "telegram": src_telegram, "page": src_page, "ioda": src_ioda,
           "yandex_artist": src_yandex_artist}


# ---------- фильтр, отправка ----------

def matches(item, kws):
    """Ключ «a» — подстрока; «a+b|c» — есть a и при этом b или c (чтобы «замедл» не ловил экономику)."""
    if not kws:
        return True
    blob = (item["title"] + " " + item["text"]).lower()
    for k in kws:
        head, _, ctx = k.partition("+")
        if head in blob and (not ctx or any(c in blob for c in ctx.split("|"))):
            return True
    return False


def fmt(cfg, item, removed=False):
    head = f"{LEVEL_MARK.get(cfg.get('level', 3), '')} <b>{html.escape(cfg.get('name', cfg['id']))}</b>"
    if removed:
        head = "🚫 ПРОПАЛО · " + head
    body = f"<b>{html.escape(item['title'])}</b>"
    if item["text"] and not short(item["text"], 10**6).startswith(item["title"].rstrip("…")):
        body += "\n" + html.escape(short(item["text"]))
    now = time.time()
    hm = lambda t: dt.datetime.fromtimestamp(t, TZ).strftime("%d.%m %H:%M")
    when = f"поймали {hm(now)}"
    if item.get("ts"):
        when = f"в источнике {hm(item['ts'])} · {when} (+{max(0, int(now - item['ts']) // 60)} мин)"
    return f"{head}\n{body}\n{html.escape(item['link'])}\n<i>{when} тмн</i>"


def send(text, dry):
    if dry:
        print("—" * 40 + "\n" + re.sub(r"</?[bi]>", "", html.unescape(text)))
        return
    token, chat = os.environ["TG_BOT_TOKEN"], os.environ["TG_CHAT_ID"]
    payload = urllib.parse.urlencode({"chat_id": chat, "text": text, "parse_mode": "HTML",
                                      "disable_web_page_preview": "true"}).encode()
    for _ in range(3):
        try:
            urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", payload, timeout=20)
            time.sleep(1.1)  # лимит тг ~20 сообщений в минуту в группу
            return
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(json.loads(e.read()).get("parameters", {}).get("retry_after", 5) + 1)
            else:
                print(f"telegram {e.code}: {e.read()[:200]}", file=sys.stderr)
                return


# ---------- huEVO ----------
HUEVO_GAP = 3600  # не чаще поста в час: п. 3.1 правил huEVO (флуд)


def huevo_text(cfg, item):
    body = item["text"] or item["title"]
    body = short(body, 700) if len(body) > 700 else body
    src = cfg.get("huevo_src") or cfg.get("name", cfg["id"])
    link = "" if "t.me/" in item["link"] else "\n" + item["link"]  # ссылки на тг ловят автомодерацию
    return f"{body.lower()}\n\nисточник: {src.lower()}{link}"


def huevo_post(text):
    """Пост от имени сообщества. Запрос снят с сайта: POST /group/<id>/add, multipart, as_group=1.
    Cookie (hue_dev, hchk, session) — в секрете HUEVO_COOKIE. Возвращает None или текст ошибки."""
    group = os.environ.get("HUEVO_GROUP", "5779")
    fields = [("message", text), ("poll_question", "")] + [("poll_option", "")] * 4 + \
        [("as_group", "1"), ("schedule_at", ""), ("schedule_offset", "-300")]
    b = "----rknkid" + hashlib.sha1(text.encode()).hexdigest()[:16]
    parts = [f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n' for k, v in fields]
    parts.append(f'--{b}\r\nContent-Disposition: form-data; name="media"; filename=""\r\n'
                 f'Content-Type: application/octet-stream\r\n\r\n\r\n')
    data = ("".join(parts) + f"--{b}--\r\n").encode()
    req = urllib.request.Request(f"https://huevo.su/group/{group}/add", data=data, headers={
        "User-Agent": UA, "Content-Type": f"multipart/form-data; boundary={b}", "Cookie": os.environ["HUEVO_COOKIE"],
        "Origin": "https://huevo.su", "Referer": "https://huevo.su/rknkid", "X-Huevo-Pjax": "1", "Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            final, body = r.geturl(), r.read(2000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code}: {short(e.read(300).decode('utf-8', 'replace'), 150)}"
    except OSError as e:
        return str(e)
    if "/login" in final or "войти" in body.lower()[:2000] and "пароль" in body.lower():
        return "похоже, cookie протух (редирект на вход)"
    print(f"[huevo] ок: {final} {short(body, 120)}")
    return None


def maybe_huevo(cfg, item, state, dry):
    if cfg.get("_huevo_mode") == "queue" or not cfg.get("huevo") or not os.environ.get("HUEVO_COOKIE"):
        return
    hs = state.setdefault("_huevo", {"last": 0, "err": ""})
    if time.time() - hs["last"] < cfg.get("_huevo_gap", HUEVO_GAP):
        print(f"[huevo] пропуск, прошло меньше часа: {item['title'][:60]}")
        return
    text = huevo_text(cfg, item)
    if dry:
        print("[huevo dry]\n" + text)
        return
    err = huevo_post(text)
    if err:
        print(f"[huevo] ошибка: {err}", file=sys.stderr)
        if hs.get("err") != err[:40]:  # про одну и ту же ошибку пишем в тг один раз
            send(f"⚠️ huEVO не принял пост: {html.escape(err)}\nесли cookie протух — обнови секрет HUEVO_COOKIE", dry)
        hs["err"] = err[:40]
        return
    hs.update(last=int(time.time()), err="")
    send(f"✅ ушло на huEVO: {html.escape(short(item['title'], 100))}", dry)


def publish_queue(state, dry):
    """queue.json пишет агент-редактор: [{"id", "at": ISO UTC, "text", "source"}].
    Бот публикует один созревший пост за проход; что опубликовано, помнит в state, сам queue.json не трогает."""
    path = os.path.join(HERE, "queue.json")
    if not os.path.exists(path) or not os.environ.get("HUEVO_COOKIE"):
        return
    hs = state.setdefault("_huevo", {"last": 0, "err": ""})
    done = hs.setdefault("posted", [])
    now = time.time()
    for q in sorted(json.load(open(path, encoding="utf-8")), key=lambda q: q["at"]):
        at = parse_ts(q["at"])
        if q["id"] in done or not at or at > now or now - at > 2 * 3600:
            continue
        if dry:
            print("[huevo queue dry]\n" + q["text"])
            return
        err = huevo_post(q["text"])
        if err:
            print(f"[huevo] ошибка: {err}", file=sys.stderr)
            if hs.get("err") != err[:40]:
                send(f"⚠️ huEVO не принял пост из очереди: {html.escape(err)}\n"
                     f"если cookie протух — обнови секрет HUEVO_COOKIE", dry)
            hs["err"] = err[:40]
            return
        done.append(q["id"])
        del done[:-300]
        hs.update(last=int(now), err="")
        send(f"✅ huEVO: {html.escape(short(q['text'], 120))}", dry)
        return


def log_detection(sid, item):
    """Для подсчёта, насколько мы первые: время в источнике (если есть) и время, когда поймали, в UTC."""
    iso = lambda t: dt.datetime.fromtimestamp(t, dt.timezone.utc).isoformat(timespec="seconds") if t else ""
    new_file = not os.path.exists(LOG)
    with open(LOG, "a", encoding="utf-8") as f:
        if new_file:
            f.write("source,source_time_utc,detected_utc,link,title\n")
        title = item["title"].replace('"', "'")
        f.write(f'{sid},{iso(item.get("ts"))},{iso(time.time())},{item["link"]},"{title[:150]}"\n')


def yandex_removals(cfg, st, items, first_run, dry):
    """Для музыки новость — это то, что исчезло, а не новые релизы."""
    prev, cur = st.get("titles", {}), {i["id"]: i["title"] for i in items}
    gone = [g for g in prev if g not in cur]
    st["titles"] = cur
    if first_run or not gone:
        return 0
    if len(gone) > max(10, len(prev) * 0.3):  # API отдал пустоту или сломался, а не массовое удаление
        print(f"[{cfg['id']}] пропало {len(gone)} из {len(prev)}, похоже на сбой API, не шлю", file=sys.stderr)
        st["titles"] = prev
        return 0
    for g in gone:
        item = {"id": g, "title": prev[g], "text": "трек пропал или стал недоступен",
                "link": f"https://music.yandex.ru/track/{g}"}
        send(fmt(cfg, item, removed=True), dry)
        log_detection(cfg["id"], item)
    return len(gone)


# ---------- основной проход ----------

def run_once(sources_path, state_path, dry):
    conf = json.load(open(sources_path, encoding="utf-8"))
    state = json.load(open(state_path, encoding="utf-8")) if os.path.exists(state_path) else {}
    default_kws = [k.lower() for k in conf.get("keywords", [])]
    sent = 0
    for cfg in conf["sources"]:
        if not cfg.get("enabled", True):
            continue
        sid = cfg["id"]
        st = state.setdefault(sid, {"seen": [], "fails": 0})
        cfg["_cache"] = state.setdefault("_ya_ids", {})
        cfg["_huevo_gap"] = conf.get("huevo_gap", HUEVO_GAP)
        cfg["_huevo_mode"] = conf.get("huevo_mode", "direct")
        try:
            items = SOURCES[cfg["type"]](cfg)
            uniq = {}
            for i in items:  # одна ссылка часто стоит на странице дважды (картинка + заголовок)
                if len(i["title"]) > len(uniq.get(i["id"], {"title": ""})["title"]):
                    uniq[i["id"]] = i
            items = list(uniq.values())
        except Exception as e:  # noqa: BLE001 — один упавший источник не должен ронять остальные
            st["fails"] = st.get("fails", 0) + 1
            print(f"[{sid}] ошибка ({st['fails']} подряд): {e}", file=sys.stderr)
            if st["fails"] == FAIL_ALERT_AT:
                send(f"⚠️ источник <b>{html.escape(sid)}</b> не отвечает {FAIL_ALERT_AT} проходов подряд: "
                     f"{html.escape(short(str(e), 150))}", dry)
            continue
        st["fails"] = 0
        seen = set(st["seen"])
        first_run = not st["seen"]
        kws = None if cfg.get("filter") is False else [k.lower() for k in cfg.get("keywords", default_kws)]
        new = [i for i in items if i["id"] not in seen]
        print(f"[{sid}] пунктов {len(items)}, новых {0 if first_run else len(new)}")
        if cfg["type"] == "yandex_artist":
            sent += yandex_removals(cfg, st, items, first_run, dry)
        elif not first_run:  # первый проход только запоминает, чтобы не завалить чат старьём
            for item in reversed(new):  # в ленте сверху новое, шлём по хронологии
                if item.get("ts") and time.time() - item["ts"] > MAX_AGE:
                    continue
                if matches(item, kws):
                    send(fmt(cfg, item), dry)
                    log_detection(sid, item)
                    maybe_huevo(cfg, item, state, dry)
                    sent += 1
        if cfg["type"] == "page" and not cfg.get("pattern"):
            st["seen"] = [i["id"] for i in items]  # дифф строк: помним текущую версию страницы
        else:
            st["seen"] = ([i["id"] for i in new] + st["seen"])[:KEEP_IDS]
    if conf.get("huevo_mode") == "queue":
        publish_queue(state, dry)
    tmp = state_path + ".tmp"
    json.dump(state, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=0)
    os.replace(tmp, state_path)
    print(f"готово, отправлено {sent}")


def find_chat_id():
    """Нет TG_CHAT_ID: берём группу, куда добавили бота, из getUpdates и запоминаем в chat_id.txt."""
    path = os.path.join(HERE, "chat_id.txt")
    if os.path.exists(path):
        return open(path).read().strip()
    url = f"https://api.telegram.org/bot{os.environ['TG_BOT_TOKEN']}/getUpdates"
    for u in reversed(json.loads(fetch(url)).get("result", [])):
        msg = u.get("message") or u.get("my_chat_member") or u.get("channel_post") or {}
        chat = msg.get("chat") or {}
        if chat.get("type") in ("group", "supergroup", "channel"):
            open(path, "w").write(str(chat["id"]))
            print(f"нашёл чат {chat.get('title')} ({chat['id']})")
            return str(chat["id"])
    sys.exit("не нашёл группу: добавь бота в группу и напиши туда любое сообщение")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--sources", default=os.path.join(HERE, "sources.json"))
    p.add_argument("--state", default=os.path.join(HERE, "state.json"))
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--loop", type=int, default=0, help="секунд между проходами; 0 = один проход")
    a = p.parse_args()
    if not a.dry_run:
        if not os.environ.get("TG_BOT_TOKEN"):
            sys.exit("нужен TG_BOT_TOKEN (или --dry-run)")
        if not os.environ.get("TG_CHAT_ID"):
            fresh = not os.path.exists(os.path.join(HERE, "chat_id.txt"))
            os.environ["TG_CHAT_ID"] = find_chat_id()
            if fresh:
                send("радар rknkid подключён. первый проход запоминает текущее, новое пойдёт со следующего.", False)
    while True:
        run_once(a.sources, a.state, a.dry_run)
        if not a.loop:
            break
        time.sleep(a.loop)


if __name__ == "__main__":
    main()
