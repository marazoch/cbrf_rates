import os
import json
from typing import Optional, Dict, Tuple, List

import requests
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes
)
from telegram.error import BadRequest

load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
API_BASE = os.getenv("API_BASE", "http://127.0.0.1:8000").rstrip("/")

DEFAULT_LIMIT = 20
MAX_LIMIT = 200
DATE_FMT = "%Y-%m-%d"


def api_get(path: str, params: Optional[Dict] = None) -> dict:
    url = f"{API_BASE}{path}"
    try:
        r = requests.get(url, params=params, timeout=10)
    except requests.RequestException as e:
        raise RuntimeError(f"API error: {e}")
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail")
        except Exception:
            detail = r.text
        raise RuntimeError(f"HTTP {r.status_code}: {detail}")
    return r.json()

def api_post(path: str, json_body: Optional[Dict] = None) -> dict:
    url = f"{API_BASE}{path}"
    try:
        r = requests.post(url, json=json_body or {}, timeout=15)
    except requests.RequestException as e:
        raise RuntimeError(f"API error: {e}")
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail")
        except Exception:
            detail = r.text
        raise RuntimeError(f"HTTP {r.status_code}: {detail}")
    return r.json()


def clamp_limit(n: int) -> int:
    try:
        n = int(n)
    except Exception:
        n = DEFAULT_LIMIT
    return max(1, min(n, MAX_LIMIT))

def parse_sort(args: List[str]) -> Tuple[str, bool]:
    sort_by = "char_code"
    desc = False
    if "sort=value" in args:
        sort_by = "value_per_unit"
    elif "sort=code" in args:
        sort_by = "char_code"
    elif "sort=name" in args:
        sort_by = "name"
    if "desc" in args:
        desc = True
    return sort_by, desc

def mk_table_rates(items: List[dict], title: str) -> Tuple[str, bool]:
    if not items:
        return f"*{title}*\n```\n(пусто)\n```", False

    code_w = max(7, max(len(i.get("char_code","")) for i in items))
    name_w = max(15, max(len(i.get("name","") or "") for i in items))
    val_w = 12

    header = f"{'Code':<{code_w}} {'Name':<{name_w}} {'RUB/1':>{val_w}}"
    lines = [header, "-" * len(header)]
    for it in items:
        code = (it.get("char_code") or "")
        name = (it.get("name") or "")
        v = it.get("value_per_unit", None)
        vs = "-" if v is None else f"{float(v):,.4f}".replace(",", " ").replace(".", ",")
        lines.append(f"{code:<{code_w}} {name:<{name_w}} {vs:>{val_w}}")

    body = "\n".join(lines)
    text = f"*{title}*\n```\n{body}\n```"
    truncated = False
    while len(text) > 3900 and len(lines) > 3:
        lines.pop()
        truncated = True
        body = "\n".join(lines)
        text = f"*{title}*\n```\n{body}\n```"
    return text, truncated

def mk_table_range(items: List[dict], title: str) -> Tuple[str, bool]:
    if not items:
        return f"*{title}*\n```\n(пусто)\n```", False

    date_w = 10
    val_w = 12
    header = f"{'Date':<{date_w}} {'RUB/1':>{val_w}}"
    lines = [header, "-" * len(header)]
    for it in items:
        date = it.get("date","")
        v = it.get("value_per_unit", None)
        vs = "-" if v is None else f"{float(v):,.4f}".replace(",", " ").replace(".", ",")
        lines.append(f"{date:<{date_w}} {vs:>{val_w}}")

    body = "\n".join(lines)
    text = f"*{title}*\n```\n{body}\n```"
    truncated = False
    while len(text) > 3900 and len(lines) > 3:
        lines.pop()
        truncated = True
        body = "\n".join(lines)
        text = f"*{title}*\n```\n{body}\n```"
    return text, truncated


def _cb_compact(data: dict) -> str:
    s = json.dumps(data, separators=(",", ":"))
    if len(s) <= 64:
        return s

    d = dict(data)
    q = d.get("q")
    if isinstance(q, dict) and "codes" in q:
        q = dict(q)
        q.pop("codes", None)
        d["q"] = q
        s = json.dumps(d, separators=(",", ":"))
        if len(s) <= 64:
            return s


    if isinstance(d.get("q"), dict):
        q = d["q"]
        short = {}
        mapping = {"date": "d", "sort_by": "s", "order": "o", "limit": "l", "offset": "f", "code": "c",
                   "start": "a", "end": "b"}
        for k, v in q.items():
            short[mapping.get(k, k[:1])] = v
        d["q"] = short
        s = json.dumps(d, separators=(",", ":"))
        if len(s) <= 64:
            return s

    return s[:64]


def mk_pager(query_dict: Dict, total: int, limit: int, offset: int, kind: str) -> Optional[InlineKeyboardMarkup]:
    buttons: List[InlineKeyboardButton] = []

    if offset > 0:
        q = dict(query_dict); q["offset"] = max(0, offset - limit); q["limit"] = limit
        payload = {"k": kind, "q": q}
        buttons.append(InlineKeyboardButton("◀ Prev", callback_data=_cb_compact(payload)))

    if offset + limit < total:
        q = dict(query_dict); q["offset"] = offset + limit; q["limit"] = limit
        payload = {"k": kind, "q": q}
        buttons.append(InlineKeyboardButton("Next ▶", callback_data=_cb_compact(payload)))

    return InlineKeyboardMarkup([buttons]) if buttons else None


HELP_TEXT = (
    "Доступные команды (бот ходит в API):\n"
    "/dates — последние доступные даты\n"
    "/latest [USD,EUR,...] [sort=value|code|name] [desc]\n"
    "  Примеры: /latest\n"
    "           /latest usd,eur\n"
    "           /latest usd,eur sort=value desc\n\n"
    "/rates YYYY-MM-DD [USD,EUR,...] [sort=value|code|name] [desc] [limit=20] [offset=0]\n"
    "  Пример: /rates 2025-09-29 usd,eur sort=value desc limit=10\n\n"
    "/range CODE START END [asc|desc] [agg=first|last|min|max|mean] [limit=50] [offset=0]\n"
    "  Пример: /range usd 2025-09-20 2025-09-29 asc\n\n"
    "/reload — попросить API перечитать CSV\n"
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я бот по курсам ЦБ РФ.\n\n" + HELP_TEXT
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT)

async def dates_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        data = api_get("/dates")
        dates = data.get("dates", [])[:20]
        msg = "Последние даты:\n" + "\n".join(dates) if dates else "Пока нет дат."
        await update.message.reply_text(msg)
    except Exception as e:
        await update.message.reply_text(f"Ошибка API: {e}")

async def latest_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = [a.strip().lower() for a in (context.args or [])]
    codes = None
    if args and not args[0].startswith("sort"):
        codes = args[0]
        args = args[1:]
    sort_by, desc = parse_sort(args)

    params = {
        "sort_by": sort_by,
        "order": "desc" if desc else "asc",
        "limit": DEFAULT_LIMIT,
        "offset": 0,
    }
    if codes:
        params["codes"] = codes

    try:
        resp = api_get("/rates", params=params)
        total = int(resp.get("total", 0))
        items = resp.get("items", [])
        date = items[0]["date"] if items else "(нет данных)"
        title = f"Курсы за {date} (первые {min(DEFAULT_LIMIT,total)} из {total})"
        text, _ = mk_table_rates(items, title)


        kb = None
        if total > DEFAULT_LIMIT:
            base_q = {"sort_by": sort_by, "order": "desc" if desc else "asc"}
            if codes: base_q["codes"] = codes
            kb = mk_pager(base_q, total, DEFAULT_LIMIT, 0, kind="rates")

        await update.message.reply_text(
            text,
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True,
            reply_markup=kb
        )
    except Exception as e:
        await update.message.reply_text(f"Ошибка API: {e}")

async def rates_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Формат: /rates YYYY-MM-DD [USD,EUR,...] [sort=value|code|name] [desc] [limit=20] [offset=0]"
        )
        return

    date = context.args[0]
    rest = [a.strip().lower() for a in context.args[1:]]

    codes = None
    limit = DEFAULT_LIMIT
    offset = 0
    sort_by, desc = parse_sort(rest)

    for a in rest:
        if a.startswith("limit="):
            limit = clamp_limit(a.split("=",1)[1])
        elif a.startswith("offset="):
            try:
                offset = max(0, int(a.split("=",1)[1]))
            except:
                offset = 0

    if rest and ("," in rest[0] or len(rest[0]) in (3,4)) and not rest[0].startswith("sort"):
        codes = rest[0]

    params = {
        "date": date,
        "sort_by": sort_by,
        "order": "desc" if desc else "asc",
        "limit": limit,
        "offset": offset,
    }
    if codes:
        params["codes"] = codes

    try:
        resp = api_get("/rates", params=params)
        total = int(resp.get("total", 0))
        items = resp.get("items", [])
        title = f"Курсы за {date} (offset {offset}, limit {limit}, total {total})"
        text, _ = mk_table_rates(items, title)

        base_q = {"date": date, "sort_by": sort_by, "order": "desc" if desc else "asc"}
        if codes: base_q["codes"] = codes
        kb = mk_pager(base_q, total, limit, offset, "rates")

        await update.message.reply_text(
            text,
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True,
            reply_markup=kb
        )
    except Exception as e:
        await update.message.reply_text(f"Ошибка API: {e}")

async def range_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or len(context.args) < 3:
        await update.message.reply_text(
            "Формат: /range CODE START END [asc|desc] [agg=first|last|min|max|mean] [limit=50] [offset=0]\n"
            "Пример: /range usd 2025-09-20 2025-09-29 asc"
        )
        return

    code = context.args[0].upper()
    start = context.args[1]
    end = context.args[2]
    extra = [a.strip().lower() for a in context.args[3:]]

    order = "asc"
    agg = None
    limit = 50
    offset = 0

    if "desc" in extra:
        order = "desc"
    for a in extra:
        if a.startswith("agg="):
            v = a.split("=",1)[1]
            if v in ("first","last","min","max","mean"):
                agg = v
        elif a.startswith("limit="):
            limit = clamp_limit(a.split("=",1)[1])
        elif a.startswith("offset="):
            try: offset = max(0, int(a.split("=",1)[1]))
            except: offset = 0

    params = {
        "code": code,
        "start": start,
        "end": end,
        "order": order,
        "limit": limit,
        "offset": offset
    }
    if agg:
        params["agg"] = agg

    try:
        resp = api_get("/range", params=params)
        total = int(resp.get("total", 0))
        items = resp.get("items", [])
        title = f"{code}: {start}—{end} ({order}), offset {offset}, limit {limit}, total {total}"
        text, _ = mk_table_range(items, title)

        base_q = {"code": code, "start": start, "end": end, "order": order}
        if agg: base_q["agg"] = agg
        kb = mk_pager(base_q, total, limit, offset, "range")

        await update.message.reply_text(
            text,
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True,
            reply_markup=kb
        )
    except Exception as e:
        await update.message.reply_text(f"Ошибка API: {e}")


async def on_pager(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try:
        payload = json.loads(q.data)
    except Exception:
        await q.edit_message_text("Некорректные параметры.")
        return

    kind = payload.get("k")
    params = payload.get("q", {})
    limit = clamp_limit(params.get("limit", DEFAULT_LIMIT))
    offset = max(0, int(params.get("offset", 0)))

    try:
        if kind == "rates":
            resp = api_get("/rates", params=params)
            total = int(resp.get("total", 0))
            items = resp.get("items", [])
            date = items[0]["date"] if items else params.get("date","")
            title = f"Курсы за {date} (offset {offset}, limit {limit}, total {total})"
            text, _ = mk_table_rates(items, title)
            kb = mk_pager(
                {k:v for k,v in params.items() if k not in ("limit","offset")},
                total, limit, offset, "rates"
            )
            try:
                if kb:
                    await q.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
                else:
                    await q.edit_message_text(text, parse_mode=ParseMode.MARKDOWN)
            except BadRequest as e:
                if "Message is not modified" in str(e):
                    await q.answer("Это уже текущая страница", show_alert=False)
                else:
                    raise

        elif kind == "range":
            resp = api_get("/range", params=params)
            total = int(resp.get("total", 0))
            items = resp.get("items", [])
            code = params.get("code","")
            title = f"{code}: {params.get('start')}—{params.get('end')} ({params.get('order')}) " \
                    f"(offset {offset}, limit {limit}, total {total})"
            text, _ = mk_table_range(items, title)
            kb = mk_pager(
                {k:v for k,v in params.items() if k not in ("limit","offset")},
                total, limit, offset, "range"
            )
            try:
                if kb:
                    await q.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
                else:
                    await q.edit_message_text(text, parse_mode=ParseMode.MARKDOWN)
            except BadRequest as e:
                if "Message is not modified" in str(e):
                    await q.answer("Это уже текущая страница", show_alert=False)
                else:
                    raise
        else:
            await q.edit_message_text("Неизвестный тип пагинации.")
    except Exception as e:
        try:
            await q.edit_message_text(f"Ошибка API: {e}")
        except Exception:
            await q.message.reply_text(f"Ошибка API: {e}")


async def reload_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        resp = api_post("/reload", {})
        rows = resp.get("rows")
        await update.message.reply_text(f"API перезагрузило CSV. Всего строк: {rows}")
    except Exception as e:
        await update.message.reply_text(f"Ошибка API: {e}")


def main():
    if not TELEGRAM_TOKEN:
        raise RuntimeError("Set TELEGRAM_TOKEN in .env or environment")
    print(f"[bot] Using API_BASE={API_BASE}")

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("dates", dates_cmd))
    app.add_handler(CommandHandler("latest", latest_cmd))
    app.add_handler(CommandHandler("rates", rates_cmd))
    app.add_handler(CommandHandler("range", range_cmd))
    app.add_handler(CommandHandler("reload", reload_cmd))  # <-- теперь есть функция
    app.add_handler(CallbackQueryHandler(on_pager))

    print("[bot] Running…")
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
