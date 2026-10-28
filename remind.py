#!/usr/bin/env python3
"""Шлёт в тг напоминания из reminders.json, время которых наступит в ближайшие 5 минут
(или прошло не больше 10 минут назад). Отправленные пишет в reminders_sent.txt, чтобы не дублировать.
Вызывается на каждом круге монитора."""
import datetime as dt
import json
import os
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
SENT = os.path.join(HERE, "reminders_sent.txt")
sent = set(open(SENT).read().split()) if os.path.exists(SENT) else set()
for r in json.load(open(os.path.join(HERE, "reminders.json"), encoding="utf-8")):
    if r["at"] in sent:
        continue
    at = dt.datetime.fromisoformat(r["at"].replace("Z", "+00:00"))
    wait = (at - dt.datetime.now(dt.timezone.utc)).total_seconds()
    if -600 < wait <= 300:
        time.sleep(max(0, wait))
        data = urllib.parse.urlencode({"chat_id": os.environ["TG_CHAT_ID"], "text": "⏰ " + r["text"]}).encode()
        urllib.request.urlopen(f"https://api.telegram.org/bot{os.environ['TG_BOT_TOKEN']}/sendMessage", data, timeout=20)
        with open(SENT, "a") as f:
            f.write(r["at"] + "\n")
        print("напоминание:", r["text"])
