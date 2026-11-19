#!/usr/bin/env python3
"""Парсер портала закупок zakup.sk.kz. Описание и запуск — README.md."""

import argparse
import asyncio
import datetime as dt
import gzip
import json
import math
import os
import random
import re
import shutil
import sys
import time
import zlib
from pathlib import Path

from playwright.async_api import TimeoutError as PWTimeout
from playwright.async_api import async_playwright

BASE = os.environ.get("ZAKUP_BASE", "https://zakup.sk.kz/")  # переопределяется только для тестов
PAGE_SIZE = 10        # поиск на сайте листается по 10 записей
MAX_PAGES = 1000      # глубже 1000-й страницы поиск не отдаёт
OPEN_PAGE_SIZE = 1000
SORT = "id,asc"       # стабильный порядок при листании

METHODS = ["CP", "CPOU", "CPOUEEP", "CPP", "DT", "EM", "IO", "OT", "OTOU", "OTP", "TKP", "VK"]
ADVERT_STATUSES = [
    "PUBLISHED", "PROPOSED", "PUBLISHED_SUPPLEMENT", "PROPOSED_SUPPLEMENT", "PUBLISHED_AUCTION",
    "AUCTION", "PURCHASING_APPROVE", "DISCUSSION_PUBLISHED", "DISCUSSION_REVIEW_INITIATOR",
    "DISCUSSION_REVIEW_OPERATOR", "DISCUSSION_REVIEW_OMBUDSMAN", "DISCUSSION_CLOSED",
    "CORRECTION_INITIATOR", "CORRECTION_OPERATOR", "OPENING", "CANCELLED", "REVIEW", "CLOSED",
    "CLOSED_FIRST_STAGE", "SUMMARIZING", "AMENDING", "AMENDED", "NEGOTIATION",
    "PUBLISHED_ADDITIONAL_PRICE"]
SUBJECTS = {"GOODS": "g", "WORKS": "w", "SERVICES": "s"}  # предмет закупки -> параметр адреса
PRICE_MAX = 10 ** 13
CONTRACT_STATUSES = ["SIGNED", "RESCIND", "EXECUTED", "SUPPLEMENTARY_AGREEMENT", "REFUSAL_PERFORM_CONTRACT"]
CONTRACT_TYPES = ["ADVERT", "RESCISSION", "ADVERT_SECOND_WINNER", "SUPPLEMENTARY_AGREEMENT", "PKO", "PKO_NEW"]
CONTRACT_DATE_MIN = dt.date(2015, 1, 1)

# файлы карточек большие и хорошо сжимаются — пишем их в .jsonl.gz
GZ_NAMES = {"adverts_full", "contracts_full", "plans_full", "complaints_full"}

ID_RE = re.compile(r'^\{"id":\s*(\d+)')
PAGE_IDX_RE = re.compile(r"[?&]page=(\d+)")


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def num_eq(body_val, v) -> bool:
    """Сумма в теле запроса совпадает с фильтром (0 сайт может вообще не отправить)."""
    if body_val in (None, ""):
        return v == 0
    try:
        return abs(float(body_val) - float(v)) < 0.5
    except (TypeError, ValueError):
        return False


def fkey(f: dict) -> str:
    return json.dumps(f, sort_keys=True, ensure_ascii=False)


def line_id(line: str):
    m = ID_RE.match(line)
    if m:
        return int(m.group(1))
    try:
        v = json.loads(line).get("id")
        return int(v) if v is not None else None
    except Exception:
        return None


def price_split(f):
    lo, hi = f.get("pfrom", 0), f.get("pto", PRICE_MAX)
    if hi - lo < 2:
        return None
    base = max(lo, 1)
    mid = int(math.sqrt(base * hi)) if hi > 4 * base else (lo + hi) // 2
    mid = max(lo + 1, min(hi - 1, mid))
    return [{**f, "pfrom": lo, "pto": mid}, {**f, "pfrom": mid, "pto": hi}]


def price_subj_split(f):
    """Сумма делится, пока можно; потом — по предмету закупки (товары / работы / услуги)."""
    parts = price_split(f)
    if parts is not None:
        return parts
    if "subj" not in f:
        return [{**f, "subj": s} for s in SUBJECTS]
    return None


def search_hash_tail(f):
    q = ""
    if "pfrom" in f:
        q += f"&pfrom={f['pfrom']}&pto={f['pto']}"
    if "pmet" in f:
        q += f"&pmet={f['pmet']}"
    if "subj" in f:
        q += f"&{SUBJECTS[f['subj']]}=true"
    return q


def search_body_ok(f, body):
    if body.get("tenderType") != f.get("pmet"):
        return False
    if "pfrom" in f and not (num_eq(body.get("sumFrom"), f["pfrom"]) and num_eq(body.get("sumTo"), f["pto"])):
        return False
    want = [f["subj"]] if "subj" in f else []
    if list(body.get("tenderSubjectTypes") or []) != want:
        return False
    return True


class AdvertSection:
    name = "adverts"
    api = "/eprocsearch/api/external/4dv3rts/filter"
    list_file = "adverts_list"
    full_file = "adverts_full"

    def __init__(self, statuses=None):
        # каждый статус — отдельный корневой срез (дальше дробится сам, если > 10 000)
        self.roots = [{"adst": s} for s in statuses] if statuses else [{}]

    def hash(self, f, page):
        return (f"#/ext?tabs=advert{search_hash_tail(f)}&adst={f.get('adst', 'ALL')}&lst=ALL"
                f"&page={page}&sort={SORT}")

    def body_ok(self, f, body):
        """Проверяет, что ответ относится именно к этому срезу (по телу запроса)."""
        if not isinstance(body, dict):
            return True
        if body.get("advertStatus", "ALL") != f.get("adst", "ALL"):
            return False
        return search_body_ok(f, body)

    def split(self, f):
        if "adst" not in f:
            return [{**f, "adst": s} for s in ADVERT_STATUSES]
        if "pmet" not in f:
            return [{**f, "pmet": m} for m in METHODS]
        return price_subj_split(f)

    def popup(self, item_id):
        return f"#/ext(popup:item/{item_id}/advert)?tabs=advert&adst=ALL&lst=ALL&page=1"


class PlanSection:
    name = "plans"
    api = "/eprocsearch/api/external/plan-items/filter"
    list_file = "plans_list"
    full_file = "plans_full"
    roots = [{}]

    def hash(self, f, page):
        return f"#/ext?tabs=plan{search_hash_tail(f)}&page={page}&sort={SORT}"

    def body_ok(self, f, body):
        if not isinstance(body, dict):
            return True
        return search_body_ok(f, body)

    def split(self, f):
        if "pmet" not in f:
            return [{**f, "pmet": m} for m in METHODS]
        return price_subj_split(f)

    def popup(self, item_id):
        return f"#/ext(popup:item/{item_id}/plan)?tabs=plan&page=1"

    def detail_suffix(self, item_id):
        return f"/plan-items/{item_id}"

    def detail_url(self, item_id):
        return BASE + f"#/ext(popup:item/{item_id}/plan)"


class ContractSection:
    name = "contracts"
    api = "/eprocsearch/api/external/contract-cards/filter"
    list_file = "contracts_list"
    full_file = "contracts_full"
    roots = [{}]

    def hash(self, f, page):
        q = "tabs=contractCard"
        for k in ("ccs", "fpc", "tt", "ct", "dt"):
            if k in f:
                q += f"&{k}={f[k]}"
        if "d1" in f:
            q += f"&cdtf={f['d1']}T00:00:00%2B05:00&cdtt={f['d2']}T00:00:00%2B05:00"
        return f"#/ext?{q}&page={page}&sort={SORT}"

    def body_ok(self, f, body):
        if not isinstance(body, dict):
            return True
        checks = [("tt", "tenderType"), ("ccs", "contractCardStatus"), ("ct", "contractType"), ("dt", "durationType")]
        for k, b in checks:
            if body.get(b) != f.get(k):
                return False
        if "d1" in f and not str(body.get("contractDateTimeFrom", "")).startswith(f["d1"]):
            return False
        return True

    def split(self, f):
        if "tt" not in f:
            return [{**f, "tt": m} for m in METHODS]
        if "ccs" not in f:
            return [{**f, "ccs": s} for s in CONTRACT_STATUSES]
        if "ct" not in f:
            return [{**f, "ct": t} for t in CONTRACT_TYPES]
        if "d1" not in f:
            d1, d2 = CONTRACT_DATE_MIN, dt.date.today() + dt.timedelta(days=2)
        else:
            d1, d2 = dt.date.fromisoformat(f["d1"]), dt.date.fromisoformat(f["d2"])
        if (d2 - d1).days >= 2 or "d1" not in f:
            mid = d1 + (d2 - d1) // 2
            return [{**f, "d1": d1.isoformat(), "d2": mid.isoformat()},
                    {**f, "d1": mid.isoformat(), "d2": d2.isoformat()}]
        if "fpc" not in f:
            return [{**f, "fpc": "0"}, {**f, "fpc": "1"}]
        if "dt" not in f:
            return [{**f, "dt": "ANNUAL"}, {**f, "dt": "LONG_TIME"}]
        return None

    def popup(self, item_id):
        return f"#/ext(popup:item/{item_id}/contractCard)?tabs=contractCard&page=1"

    def detail_suffix(self, item_id):
        return f"/contract-cards/{item_id}"

    def detail_url(self, item_id):
        return BASE + f"#/ext(popup:item/{item_id}/contractCard)"


class Store:
    """*.jsonl, для GZ_NAMES — части <имя>.<запуск>.jsonl.gz; <имя>.ids — готовые id."""

    def __init__(self, out: Path, gz_names=GZ_NAMES):
        self.out = out
        out.mkdir(parents=True, exist_ok=True)
        self.gz = set(gz_names)
        self.run_tag = time.strftime("%Y%m%d-%H%M%S")
        self.state_path = out / "state.json"
        self.state = {}
        if self.state_path.exists():
            try:
                self.state = json.loads(self.state_path.read_text("utf-8"))
            except Exception:
                bak = self.state_path.with_suffix(".bak")
                if bak.exists():
                    self.state = json.loads(bak.read_text("utf-8"))
        self._files = {}


    def append(self, name, rows):
        f = self._files.get(name)
        if f is None:
            if name in self.gz:
                p = self.out / f"{name}.{self.run_tag}.jsonl.gz"
                f = gzip.open(p, "at", encoding="utf-8", compresslevel=6)
            else:
                f = open(self.out / f"{name}.jsonl", "a", encoding="utf-8")
            self._files[name] = f
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
        f.flush()

    def mark_done(self, name, ids):
        f = self._files.get(name + ".ids")
        if f is None:
            f = self._files[name + ".ids"] = open(self.out / f"{name}.ids", "a", encoding="utf-8")
        for i in ids:
            f.write(f"{i}\n")
        f.flush()


    def parts(self, name):
        res = []
        plain = self.out / f"{name}.jsonl"
        if plain.exists():
            res.append(plain)
        res += sorted(self.out.glob(f"{name}.*.jsonl.gz"))
        return res

    def iter_lines(self, name):
        for p in self.parts(name):
            opener = gzip.open if p.suffix == ".gz" else open
            try:
                with opener(p, "rt", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        line = line.strip()
                        if line.endswith("}") or line.endswith("]"):
                            yield line
            except (EOFError, zlib.error, gzip.BadGzipFile, OSError) as e:
                log(f"  {p.name}: файл оборван в конце ({type(e).__name__}) — прочитано всё до обрыва")

    def names(self):
        res = set()
        for p in self.out.iterdir():
            n = p.name
            if n.endswith(".jsonl"):
                res.add(n[:-6])
            elif n.endswith(".jsonl.gz"):
                res.add(n.split(".")[0])
        return sorted(res)

    def load_done(self, name, line_ok=None):
        """Готовые id из <name>.ids; если его нет — один раз сканирует данные и создаёт его."""
        p = self.out / f"{name}.ids"
        if p.exists():
            with open(p, encoding="utf-8") as f:
                return {s.strip() for s in f if s.strip()}
        if not self.parts(name):
            return set()
        log(f"  {name}: первый запуск новой версии — составляю список готовых записей…")
        done = set()
        for line in self.iter_lines(name):
            if line_ok and not line_ok(line):
                continue
            i = line_id(line)
            if i is not None:
                done.add(str(i))
        self.mark_done(name, sorted(done))
        return done


    def save_state(self):
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False), "utf-8")
        if self.state_path.exists():
            self.state_path.replace(self.state_path.with_suffix(".bak"))
        tmp.replace(self.state_path)

    def close(self):
        for f in self._files.values():
            f.close()
        self._files.clear()

    def compact(self):
        """Старые несжатые *_full.jsonl -> <имя>.000000-legacy.jsonl.gz (с проверкой), затем удаляет оригинал."""
        self.close()
        for name in sorted(self.gz):
            src = self.out / f"{name}.jsonl"
            if not src.exists():
                continue
            dst = self.out / f"{name}.000000-legacy.jsonl.gz"
            log(f"Сжимаю {src.name} ({src.stat().st_size / 2**30:.1f} ГБ)…")
            n_in = 0
            with open(src, encoding="utf-8", errors="replace") as fi, \
                    gzip.open(dst, "wt", encoding="utf-8", compresslevel=6) as fo:
                for line in fi:
                    if line.strip():
                        fo.write(line if line.endswith("\n") else line + "\n")
                        n_in += 1
                        if n_in % 2000 == 0:
                            log(f"  {n_in} строк…")
            n_out = 0
            with gzip.open(dst, "rt", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        n_out += 1
            if n_out != n_in:
                log(f"  ОШИБКА проверки ({n_in} != {n_out}) — оригинал оставлен, сжатая копия удалена")
                dst.unlink()
                continue
            src.unlink()
            old_json = self.out / f"{name}.json"
            log(f"  готово: {dst.name} ({dst.stat().st_size / 2**30:.2f} ГБ), {n_out} строк"
                + (f"; старый {old_json.name} можно удалить" if old_json.exists() else ""))

    def export_json(self):
        """*.jsonl -> *.json(.gz): массив без дублей по id (последняя версия), потоково."""
        self.close()
        for name in self.names():
            if name == "files_index":
                continue
            last = {}
            for i, line in enumerate(self.iter_lines(name)):
                k = line_id(line)
                last[k if k is not None else f"_{i}"] = i
            keep = set(last.values())
            gz = name in self.gz
            path = self.out / (f"{name}.json.gz" if gz else f"{name}.json")
            n = 0
            with (gzip.open(path, "wt", encoding="utf-8", compresslevel=6) if gz
                  else open(path, "w", encoding="utf-8")) as out:
                out.write("[\n")
                for i, line in enumerate(self.iter_lines(name)):
                    if i in keep:
                        out.write((",\n" if n else "") + line)
                        n += 1
                out.write("\n]\n")
            log(f"  {path.name}: {n} записей")


class Throttle:
    """При >= 20% ошибок за последние 50 запросов — общая пауза всех вкладок (растущая)."""

    WINDOW, SHARE = 50, 0.2

    def __init__(self):
        self.until = 0.0
        self.errors = 0
        self.recent = []  # последние исходы: True = ошибка
        self.level = 0
        self.ok_streak = 0

    def _push(self, is_err):
        self.recent.append(is_err)
        if len(self.recent) > self.WINDOW:
            self.recent.pop(0)

    def ok(self):
        self._push(False)
        self.ok_streak += 1
        if self.ok_streak >= 500 and self.level:
            self.level -= 1
            self.ok_streak = 0

    def hit(self):
        now = time.time()
        self.errors += 1
        self.ok_streak = 0
        self._push(True)
        if len(self.recent) >= 10 and sum(self.recent) >= self.SHARE * len(self.recent):
            self.level = min(self.level + 1, 6)
            pause = 30 * 2 ** (self.level - 1)  # 30 с, 1, 2, 4, 8, 16 мин
            self.until = max(self.until, now + pause)
            self.recent.clear()
            log(f"  сервер часто отвечает ошибками — общая пауза {pause if pause < 60 else pause // 60} "
                f"{'с' if pause < 60 else 'мин'}")

    async def wait(self):
        d = self.until - time.time()
        if d > 0:
            await asyncio.sleep(d)


def post_body(resp):
    try:
        return resp.request.post_data_json
    except Exception:
        return None


class Worker:
    def __init__(self, wid, page, delay, timeout, throttle: Throttle):
        self.wid = wid
        self.page = page
        self.delay = delay
        self.timeout_ms = int(timeout * 1000)
        self.throttle = throttle
        self.responses = {}
        self.last_total = None
        page.on("response", self._on_response)

    def _on_response(self, resp):
        if "/api/external/" in resp.url:
            self.responses[resp.url] = resp
            if len(self.responses) > 300:
                for k in list(self.responses)[:150]:
                    self.responses.pop(k, None)

    async def pause(self, k=1.0):
        await asyncio.sleep(self.delay * k * random.uniform(0.7, 1.3))

    async def open_site(self):
        await self.page.goto(BASE + "#/ext", wait_until="domcontentloaded", timeout=90_000)
        await self.page.wait_for_timeout(4000)

    async def set_hash(self, h):
        await self.page.evaluate("h => { location.hash = h }", h)

    async def nav_wait(self, hash_, url_pred, tries=4):
        """Меняет hash (SPA-навигация) и ждёт нужный ответ API. Возвращает JSON или None."""
        for attempt in range(1, tries + 1):
            await self.throttle.wait()
            try:
                if await self.page.evaluate("() => location.hash") == hash_:
                    await self.set_hash("#/ext/faq")  # тот же адрес не вызовет запрос
                    await self.page.wait_for_timeout(800)
                async with self.page.expect_response(url_pred, timeout=self.timeout_ms) as info:
                    await self.set_hash(hash_)
                resp = await info.value
                if resp.status == 200:
                    self.throttle.ok()
                    data = await resp.json()
                    try:
                        tc = (await resp.all_headers()).get("x-total-count")
                        self.last_total = int(tc) if tc is not None and str(tc).isdigit() else None
                        if os.environ.get("ZAKUP_IGNORE_TOTAL"):  # только для тестов запасного пути
                            self.last_total = None
                    except Exception:
                        self.last_total = None
                    return data
                if attempt > 1:
                    log(f"  [w{self.wid}] HTTP {resp.status} (попытка {attempt}/{tries})")
                if resp.status >= 400:
                    self.throttle.hit()
            except PWTimeout:
                log(f"  [w{self.wid}] таймаут (попытка {attempt}/{tries})")
            except Exception as e:
                log(f"  [w{self.wid}] ошибка: {type(e).__name__}: {str(e)[:150]}")
            await asyncio.sleep(3 * attempt * attempt)  # 3, 12, 27 с
            if attempt >= 2:  # повторная неудача — перезагружаем сайт во вкладке
                try:
                    await self.open_site()
                except Exception:
                    pass
        return None


    def list_pred(self, sec, f, idx):
        def pred(r):
            if sec.api not in r.url or "sort=id" not in r.url:
                return False
            m = PAGE_IDX_RE.search(r.url)
            return bool(m) and int(m.group(1)) == idx and sec.body_ok(f, post_body(r))
        return pred

    async def fetch_page(self, sec, f, n):
        """Страница n (с 1) среза f. Возвращает список строк или None."""
        data = await self.nav_wait(sec.hash(f, n), self.list_pred(sec, f, n - 1))
        if data is None:
            return None
        return data if isinstance(data, list) else data.get("content", [])

    async def probe(self, sec, f):
        """Размер среза по 1000-й странице. Возвращает (status, total, {page: rows})."""
        seen = []

        def on(r):
            if sec.api in r.url:
                seen.append(r)

        self.page.on("response", on)
        try:
            self.last_total = None
            rows = await self.fetch_page(sec, f, MAX_PAGES)
            if rows is None:
                return None
            if len(rows) >= PAGE_SIZE:
                return ("big", None, {})
            if self.last_total is not None:  # сервер сам сказал, сколько записей
                if self.last_total >= MAX_PAGES * PAGE_SIZE:
                    return ("big", None, {})
                pre = {MAX_PAGES: rows} if rows else {}
                return ("ok", self.last_total, pre)
            if rows:
                return ("ok", (MAX_PAGES - 1) * PAGE_SIZE + len(rows), {MAX_PAGES: rows})
            t0 = time.time()
            while time.time() - t0 < 6:
                for r in list(seen):
                    m = PAGE_IDX_RE.search(r.url)
                    if not m or int(m.group(1)) == MAX_PAGES - 1 or r.status != 200 or "sort=id" not in r.url:
                        continue
                    if not sec.body_ok(f, post_body(r)):
                        continue
                    try:
                        last_rows = await r.json()
                    except Exception:
                        continue
                    idx = int(m.group(1))
                    return ("ok", idx * PAGE_SIZE + len(last_rows), {idx + 1: last_rows})
                await asyncio.sleep(0.25)
        finally:
            self.page.remove_listener("response", on)
        # сайт не перешёл на последнюю страницу — смотрим первую сами
        first = await self.fetch_page(sec, f, 1)
        if first is None:
            return None
        if not first:
            return ("ok", 0, {})
        return ("walk", None, {1: first})  # количество неизвестно — листаем до конца


    async def fetch_advert(self, sec, aid):
        detail_suffix = f"/4dv3rts/{aid}"
        lots0 = re.compile(rf"/4dv3rts/lots/{aid}\?.*page=0(&|$)")
        for u in [u for u in self.responses if f"/{aid}" in u]:
            self.responses.pop(u, None)

        detail = await self.nav_wait(sec.popup(aid), lambda r: r.url.split("?")[0].endswith(detail_suffix))
        if detail is None:
            return None

        lots, lots_complete, lots_total = [], False, None
        t0 = time.time()
        while time.time() - t0 < self.timeout_ms / 1000:
            r = next((v for u, v in self.responses.items() if lots0.search(u)), None)
            if r is not None:
                try:
                    if r.status == 200:
                        lots, lots_complete = list(await r.json()), True
                        tc = (await r.all_headers()).get("x-total-count")
                        lots_total = int(tc) if tc and str(tc).isdigit() else None
                except Exception:
                    pass
                break
            await asyncio.sleep(0.2)

        # больше 10 лотов — листаем пагинацию внутри карточки (программный клик)
        k = 1
        sel = 'ngb-modal-window ngb-pagination li:not(.disabled) a[aria-label="Next"]'
        while lots_complete and len(lots) == k * PAGE_SIZE:
            if lots_total is not None and len(lots) >= lots_total:
                break  # сервер сообщил число лотов — все собраны
            nxt = self.page.locator(sel).first
            try:
                await nxt.wait_for(state="attached", timeout=6000)
            except Exception:
                break  # кнопки «дальше» нет — значит, лотов ровно k*10
            pre = re.compile(rf"/4dv3rts/lots/{aid}\?.*page={k}(&|$)")
            chunk = None
            for attempt in range(3):  # при ошибке сервера сайт оставляет кнопку — жмём ещё раз
                try:
                    async with self.page.expect_response(lambda r: pre.search(r.url) is not None,
                                                         timeout=self.timeout_ms) as info:
                        await self.page.locator(sel).first.evaluate("a => a.click()")
                    resp = await info.value
                    if resp.status == 200:
                        self.throttle.ok()
                        chunk = await resp.json()
                        break
                    self.throttle.hit()
                except Exception:
                    pass
                await asyncio.sleep(2 + 3 * attempt)
            if chunk is None:
                lots_complete = False
                break
            lots.extend(chunk)
            k += 1
            if not chunk:
                break
            await self.pause(0.3)

        await asyncio.sleep(0.3)
        min_salary = None
        r = next((v for u, v in self.responses.items() if f"min-salary-documents/{aid}" in u), None)
        if r is not None and r.status == 200:
            try:
                min_salary = await r.json()
            except Exception:
                pass

        rec = dict(detail)
        rec["lots"] = lots
        rec["_lotsComplete"] = lots_complete
        rec["_minSalaryDocuments"] = min_salary
        rec["_url"] = BASE + f"#/ext(popup:item/{aid}/advert)"
        return rec

    async def fetch_simple(self, sec, item_id):
        """Карточка договора / пункта плана: один ответ API."""
        suffix = sec.detail_suffix(item_id)
        detail = await self.nav_wait(sec.popup(item_id), lambda r: r.url.split("?")[0].endswith(suffix))
        if detail is None:
            return None
        rec = {"id": item_id, **detail} if isinstance(detail, dict) else {"id": item_id, "data": detail}
        rec["_url"] = sec.detail_url(item_id)
        return rec


class Crawler:
    def __init__(self, store: Store, workers, sections, details: dict):
        self.store = store
        self.workers = workers
        self.sections = sections
        self.details = details  # имя раздела -> собирать ли карточки
        self.list_q = asyncio.Queue()
        self.detail_q = asyncio.Queue()
        self.active = 0
        self.done_ids = {s.name: set() for s in sections}
        self.queued_ids = {s.name: set() for s in sections}
        self.stats = {"pages": 0, "details": 0, "detail_fail": 0, "probes": 0}
        self.t0 = time.time()

    def sl(self, sec):
        return self.store.state.setdefault("slices", {}).setdefault(sec.name, {})


    def bootstrap(self):
        for sec in self.sections:
            for root in sec.roots:
                self._plan(sec, root)
            if not self.details.get(sec.name):
                continue
            # готовые карточки (объявления — только если лоты собраны полностью)
            ok = (lambda line: '"_lotsComplete": false' not in line) if sec.name == "adverts" else None
            done = self.done_ids[sec.name]
            done.update(int(x) for x in self.store.load_done(sec.full_file, ok) if x.isdigit())
            todo = set()
            for line in self.store.iter_lines(sec.list_file):
                i = line_id(line)
                if i is not None and i not in done:
                    todo.add(i)
            for i in sorted(todo, reverse=True):  # сначала самые новые
                self.queued_ids[sec.name].add(i)
                self.detail_q.put_nowait((sec, i))
            log(f"[{sec.name}] карточек уже есть: {len(done)}, в очереди: {len(todo)}")

    def _plan(self, sec, f):
        s = self.sl(sec).get(fkey(f))
        if s is None:
            self.list_q.put_nowait(("probe", sec, f))
        elif s["st"] == "big":
            for c in sec.split(f):
                self._plan(sec, c)
        elif s["st"] in ("ok", "overflow"):
            self._enqueue_pages(sec, f, s)
        elif s["st"] == "walk":
            self.list_q.put_nowait(("walk", sec, f))

    def _enqueue_pages(self, sec, f, s):
        n_pages = min(MAX_PAGES, math.ceil((s.get("total") or 0) / PAGE_SIZE))
        done = set(s.get("done", []))
        for n in range(1, n_pages + 1):
            if n not in done:
                self.list_q.put_nowait(("page", sec, f, n))


    def _got_rows(self, sec, f, n, rows):
        for r in rows:
            r["_slice"] = f
        self.store.append(sec.list_file, rows)
        s = self.sl(sec)[fkey(f)]
        s.setdefault("done", []).append(n)
        self.stats["pages"] += 1
        if self.details.get(sec.name):
            for r in rows:
                i = r.get("id")
                if i is not None and i not in self.done_ids[sec.name] and i not in self.queued_ids[sec.name]:
                    self.queued_ids[sec.name].add(i)
                    self.detail_q.put_nowait((sec, i))

    async def do_task(self, w: Worker, task):
        kind = task[0]
        if kind == "probe":
            _, sec, f = task
            res = await w.probe(sec, f)
            self.stats["probes"] += 1
            if res is None:
                log(f"  [{sec.name}] не удалось оценить срез {f} — повторю при перезапуске")
                return
            st, total, pre = res
            key = fkey(f)
            if st == "big":
                children = sec.split(f)
                if children is None:
                    log(f"  [{sec.name}] ВНИМАНИЕ: срез {f} больше 10 000 и дальше не делится — "
                        f"будут собраны только первые 10 000 записей")
                    self.sl(sec)[key] = {"f": f, "st": "overflow", "total": MAX_PAGES * PAGE_SIZE, "done": []}
                    self._enqueue_pages(sec, f, self.sl(sec)[key])
                else:
                    self.sl(sec)[key] = {"f": f, "st": "big"}
                    for c in children:
                        self.list_q.put_nowait(("probe", sec, c))
                return
            self.sl(sec)[key] = {"f": f, "st": st, "total": total, "done": []}
            for n, rows in pre.items():
                self._got_rows(sec, f, n, rows)
            if st == "ok":
                self._enqueue_pages(sec, f, self.sl(sec)[key])
            else:  # walk
                self.list_q.put_nowait(("walk", sec, f))
        elif kind == "page":
            _, sec, f, n = task
            rows = await w.fetch_page(sec, f, n)
            if rows is None:
                log(f"  [{sec.name}] страница {n} среза {f} не загрузилась — повторю при перезапуске")
                return
            self._got_rows(sec, f, n, rows)
        elif kind == "walk":
            _, sec, f = task
            s = self.sl(sec)[fkey(f)]
            done = set(s.get("done", []))
            n = 1
            while n <= MAX_PAGES:
                if n not in done:
                    rows = await w.fetch_page(sec, f, n)
                    if rows is None:
                        return
                    self._got_rows(sec, f, n, rows)
                    if len(rows) < PAGE_SIZE:
                        s["total"] = (n - 1) * PAGE_SIZE + len(rows)
                        s["st"] = "ok"
                        return
                    await w.pause()
                n += 1
        elif kind == "detail":
            _, sec, i = task
            if sec.name == "adverts":
                rec = await w.fetch_advert(sec, i)
                for _ in range(2):  # лоты не догрузились — открываем карточку заново
                    if rec is None or rec.get("_lotsComplete"):
                        break
                    await asyncio.sleep(3)
                    rec2 = await w.fetch_advert(sec, i)
                    if rec2 is not None:
                        rec = rec2
            else:
                rec = await w.fetch_simple(sec, i)
            if rec is None:
                self.stats["detail_fail"] += 1
                return
            self.store.append(sec.full_file, [rec])
            if sec.name != "adverts" or rec.get("_lotsComplete"):
                self.done_ids[sec.name].add(i)
                self.store.mark_done(sec.full_file, [i])
            self.stats["details"] += 1

    async def worker_loop(self, w: Worker):
        while True:
            task = None
            # списки и карточки чередуются, чтобы карточки начинали появляться сразу
            take_list = not self.list_q.empty() and (self.detail_q.empty() or random.random() < 0.5)
            if take_list:
                task = self.list_q.get_nowait()
            elif not self.detail_q.empty():
                sec, i = self.detail_q.get_nowait()
                task = ("detail", sec, i)
            if task is None:
                if self.active == 0 and self.list_q.empty() and self.detail_q.empty():
                    return
                await asyncio.sleep(0.5)
                continue
            self.active += 1
            try:
                await self.do_task(w, task)
            except Exception as e:
                log(f"  [w{w.wid}] ошибка задачи {task[0]}: {type(e).__name__}: {str(e)[:150]}")
                try:
                    await w.open_site()
                except Exception:
                    pass
            finally:
                self.active -= 1
            await w.pause()

    async def reporter(self):
        last = (0, 0, time.time())
        while True:
            await asyncio.sleep(30)
            self.store.save_state()
            p, d, t = self.stats["pages"], self.stats["details"], time.time()
            rate_d = (d - last[1]) / max(t - last[2], 1)
            q_d = self.detail_q.qsize()
            eta = f", карточки ~{q_d / rate_d / 3600:.1f} ч" if rate_d > 0 and q_d else ""
            log(f"[прогресс] срезов проверено {self.stats['probes']}, страниц {p}, в очереди списка "
                f"{self.list_q.qsize()} | карточек {d} (+{rate_d:.2f}/с), в очереди {q_d}, "
                f"не загрузились {self.stats['detail_fail']} | ошибок сервера {self.workers[0].throttle.errors}{eta}")
            last = (p, d, t)

    async def run(self):
        self.bootstrap()
        rep = asyncio.create_task(self.reporter())
        try:
            await asyncio.gather(*(self.worker_loop(w) for w in self.workers))
        finally:
            rep.cancel()
            self.store.save_state()
        log(f"[поиск] готово: страниц {self.stats['pages']}, карточек {self.stats['details']}, "
            f"не загрузились {self.stats['detail_fail']} (догрузятся при перезапуске), "
            f"{(time.time() - self.t0) / 60:.0f} мин")


def open_lists():
    # ключ состояния, файл, путь (page/size подставляются), сортировка для стабильного порядка
    res = [
        ("complaints_list", "complaints_list", "/eprocworkflow/open-api/complaints", "&sort=id,desc"),
        ("companies", "companies", "/eprocglobal/open-api/companies/for-main-page", ""),
        ("bad_suppliers", "bad_suppliers", "/eprocglobal/open-api/suppliers?registerType=BAD_SUPPLIER", "&sort=nameRu,asc"),
        ("disability_orgs", "disability_orgs", "/eprocglobal/open-api/suppliers?registerType=INVALID_COMPANY", "&sort=nameRu,asc"),
        ("pko_suppliers", "pko_suppliers", "/eprocpko/open-api/pko-qualified-suppliers", ""),
        ("dpo_suppliers", "dpo_suppliers", "/eprocglobal/open-api/dpo-pep-suppliers", "&sort=registryAddedAt,ASC"),
        ("ppk_items", "ppk_items", "/eprockuz/open-api/ppk/items", "&sort=id,asc"),
        ("pko_categories", "pko_categories", "/eprocpko/open-api/pko-category-dictionaries", ""),
        ("regulations", "regulations", "/eprocglobal/open-api/regulations/?recipient=ALL", "&sort=id,desc"),
    ]
    for y in range(2015, dt.date.today().year + 2):
        res.append((f"plan_extracts:{y}", "plan_extracts", f"/eprocplan/open-api/plan-extract/filter?year={y}", ""))
    return res


OPEN_SINGLE = [
    ("currency_rates", "/eprocglobal/open-api/currency-rates"),
]

DICT_CODES = [
    "advert_status", "claim_action_type", "claim_nsi", "composite_document_computed_field", "conclusion_type",
    "contract_card_rescission_reason", "contract_card_second_winner_reason",
    "contract_card_supplementary_agreement_reason", "contract_card_status", "country",
    "cp_cpp_contract_card_second_winner_reason", "cpou_eep_contract_card_second_winner_reason",
    "cpou_source_reason", "discussion_status", "faq_group", "goods_attributes", "incoterm_condition",
    "kato_region", "lot_status", "mkei", "nds_size", "participation_status", "plan_duration_type",
    "plan_item_status", "plan_type", "requirement", "single_source_reason", "supplier_bad",
    "supplier_genuine", "tender_priority", "tender_subject_type", "tender_type", "tkp_source_reason"]


async def open_get(req, url, tries=5, quiet404=False):
    for attempt in range(1, tries + 1):
        try:
            r = await req.get(url, timeout=120_000)
            if r.status == 200:
                return await r.json(), r.headers.get("x-total-count")
            if quiet404 and r.status in (400, 404, 405, 410):
                return None, None
            log(f"  [open] HTTP {r.status} {url[:110]} (попытка {attempt})")
        except Exception as e:
            log(f"  [open] {type(e).__name__} {url[:110]} (попытка {attempt})")
        await asyncio.sleep(5 * attempt)
    return None, None


def page_rows(data):
    """Ответ open-api: либо список, либо страница Spring {content, totalElements}."""
    if isinstance(data, list):
        return data, None
    if isinstance(data, dict) and isinstance(data.get("content"), list):
        return data["content"], data.get("totalElements")
    return ([data] if data else []), None


async def scrape_open(ctx, store: Store, delay, workers, complaint_details=True):
    req = ctx.request
    root = BASE.rstrip("/")
    st = store.state.setdefault("open", {})
    for key, name, path, sort in open_lists():
        s = st.setdefault(key, {"next": 0, "done": False, "size": OPEN_PAGE_SIZE, "got": 0})
        if s["done"]:
            continue
        sep = "&" if "?" in path else "?"
        prev_first = None
        while True:
            url = f"{root}{path}{sep}page={s['next']}&size={s['size']}{sort}"
            data, tc = await open_get(req, url)
            if data is None:
                log(f"[open] {key}: остановка на странице {s['next']} — продолжу при перезапуске")
                break
            rows, total2 = page_rows(data)
            total = int(tc) if tc and str(tc).isdigit() else total2
            first = json.dumps(rows[0], sort_keys=True)[:500] if rows else None
            if s["next"] > 0 and first is not None and first == prev_first:
                rows = []  # сервер не листает (отдаёт одно и то же) — всё уже собрано
            prev_first = first
            if s["next"] == 0 and total and 0 < len(rows) < s["size"] and len(rows) < total:
                s["size"] = len(rows)  # сервер урезал размер страницы — подстраиваемся
            if rows:
                if key.startswith("plan_extracts"):
                    for r in rows:
                        r.setdefault("_year", key.split(":")[1])
                store.append(name, rows)
            s["got"] += len(rows)
            s["next"] += 1
            finished = not rows or (total is not None and s["got"] >= total) or \
                (total is None and len(rows) < s["size"])
            store.save_state()
            if finished:
                s["done"] = True
                store.save_state()
                log(f"[open] {key}: готово ({s['got']} записей)")
                break
            if s["next"] % 20 == 0:
                log(f"[open] {key}: {s['got']}/{total or '?'}")
            await asyncio.sleep(delay)

    for name, path in OPEN_SINGLE:
        if st.get(name, {}).get("done"):
            continue
        data, _ = await open_get(req, root + path, tries=3)
        if data is not None:
            rows, _ = page_rows(data)
            store.append(name, rows)
            st[name] = {"done": True, "got": len(rows)}
            store.save_state()
            log(f"[open] {name}: готово ({len(rows)})")

    dst = st.setdefault("dictionaries", {})
    for code in DICT_CODES:
        if dst.get(code):
            continue
        data, _ = await open_get(req, f"{root}/eprocglobal/open-api/entries?code={code}&size=100000",
                                 tries=3, quiet404=True)
        if data is None:
            continue
        rows, _ = page_rows(data)
        store.append("dictionaries", [{**r, "_code": code} if isinstance(r, dict) else {"_code": code, "v": r}
                                      for r in rows])
        dst[code] = len(rows) or True
        store.save_state()
        await asyncio.sleep(delay)
    log(f"[open] справочники: {len(dst)}/{len(DICT_CODES)}")

    if not complaint_details:
        return
    done = store.load_done("complaints_full")
    ids = [line_id(l) for l in store.iter_lines("complaints_list")]
    todo = list(dict.fromkeys(i for i in ids if i is not None and str(i) not in done))
    log(f"[open] карточки жалоб: есть {len(done)}, к загрузке {len(todo)}")
    q = asyncio.Queue()
    for i in todo:
        q.put_nowait(i)
    cnt = {"n": 0}

    async def run():
        while not q.empty():
            i = q.get_nowait()
            data, _ = await open_get(req, f"{root}/eprocworkflow/open-api/complaints/{i}", tries=3)
            if data is not None:
                if isinstance(data, dict):
                    data = {"id": i, **data}
                store.append("complaints_full", [data])
                store.mark_done("complaints_full", [i])
            cnt["n"] += 1
            if cnt["n"] % 500 == 0:
                log(f"[open] карточки жалоб: {cnt['n']}/{len(todo)}")
            await asyncio.sleep(delay * random.uniform(0.7, 1.3))

    await asyncio.gather(*(run() for _ in range(workers)))
    log("[open] карточки жалоб: готово")


FILE_OBJ_RE = re.compile(r'\{[^{}]*"(?:fileUid|excelFileUid)"\s*:\s*"[^"]+"[^{}]*\}')
FILE_UID_RE = re.compile(r'"(fileUid|excelFileUid)"\s*:\s*"([^"]+)"')
BAD_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def collect_file_refs(store: Store):
    refs = {}
    for name in store.names():
        if name == "files_index":
            continue
        n0 = len(refs)
        for line in store.iter_lines(name):
            if "ileUid" not in line:
                continue
            for m in FILE_OBJ_RE.finditer(line):
                obj = m.group(0)
                um = FILE_UID_RE.search(obj)
                uid = um.group(2)
                if uid in refs:
                    continue
                try:
                    d = json.loads(obj)
                except Exception:
                    d = {}
                refs[uid] = {"uid": uid, "fileName": d.get("fileName") or d.get("name"),
                             "contentType": d.get("contentType"), "from": name,
                             "category": d.get("documentCategory") or d.get("documentType")}
        if len(refs) > n0:
            log(f"[files] {name}: найдено ссылок {len(refs) - n0}")
    return refs


async def scrape_files(ctx, store: Store, workers, delay, min_free_gb):
    log("[files] ищу ссылки на документы в собранных данных…")
    refs = collect_file_refs(store)
    done = store.load_done("files")
    todo = [r for u, r in refs.items() if u not in done]
    log(f"[files] всего документов {len(refs)}, уже скачано {len(refs) - len(todo)}, к загрузке {len(todo)}")
    if not todo:
        return
    fdir = store.out / "files"
    q = asyncio.Queue()
    for r in todo:
        q.put_nowait(r)
    cnt = {"n": 0, "bytes": 0, "stop": False}
    root = BASE.rstrip("/")
    t0 = time.time()

    async def run():
        while not q.empty() and not cnt["stop"]:
            ref = q.get_nowait()
            if shutil.disk_usage(store.out).free < min_free_gb * 2 ** 30:
                if not cnt["stop"]:
                    log(f"[files] на диске меньше {min_free_gb} ГБ — останавливаю скачивание файлов "
                        f"(освободите место и запустите снова, продолжится с того же места)")
                cnt["stop"] = True
                return
            uid = ref["uid"]
            status = None
            for attempt in range(1, 4):
                try:
                    r = await ctx.request.get(f"{root}/eprocfilestorage/open-api/files/download/{uid}",
                                              timeout=300_000)
                    status = r.status
                    if r.status == 200:
                        body = await r.body()
                        name = BAD_CHARS.sub("_", ref.get("fileName") or "file").strip(" .")[:150] or "file"
                        d = fdir / uid[:2] / BAD_CHARS.sub("_", uid)
                        d.mkdir(parents=True, exist_ok=True)
                        (d / name).write_bytes(body)
                        store.append("files_index", [{**ref, "size": len(body),
                                                      "path": str((d / name).relative_to(store.out))}])
                        store.mark_done("files", [uid])
                        cnt["bytes"] += len(body)
                        break
                    if r.status in (400, 403, 404, 410):
                        store.append("files_index", [{**ref, "error": r.status}])
                        store.mark_done("files", [uid])
                        break
                except Exception as e:
                    status = type(e).__name__
                await asyncio.sleep(5 * attempt)
            cnt["n"] += 1
            if cnt["n"] % 500 == 0:
                el = time.time() - t0
                log(f"[files] {cnt['n']}/{len(todo)}, {cnt['bytes'] / 2**30:.2f} ГБ, "
                    f"{cnt['n'] / el:.1f} файл/с, ~{(len(todo) - cnt['n']) / max(cnt['n'] / el, 0.01) / 3600:.1f} ч"
                    + (f" (последний ответ: {status})" if status != 200 else ""))
            await asyncio.sleep(delay * random.uniform(0.7, 1.3))

    await asyncio.gather(*(run() for _ in range(workers)))
    log(f"[files] готово за этот запуск: {cnt['n']} файлов, {cnt['bytes'] / 2**30:.2f} ГБ")


SECTIONS = ("open", "adverts", "contracts", "plans", "files")


async def amain(args):
    sections = [s.strip() for s in args.sections.split(",") if s.strip()]
    for s in sections:
        if s not in SECTIONS:
            sys.exit(f"Неизвестный раздел: {s}. Доступно: {', '.join(SECTIONS)}")
    store = Store(Path(args.out))
    throttle = Throttle()
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=not args.show)
        ctx = await browser.new_context(locale="ru-RU", viewport={"width": 1366, "height": 900})
        try:
            if "open" in sections:
                await scrape_open(ctx, store, args.delay, args.workers, not args.no_complaint_details)
            secs = []
            if "adverts" in sections:
                v = args.advert_status.upper()
                if v == "ALL":
                    statuses = None
                elif v == "CURRENT":  # всё, кроме архива (завершённых и отменённых)
                    statuses = [s for s in ADVERT_STATUSES if s not in ("CLOSED", "CANCELLED")]
                else:
                    statuses = [s.strip() for s in v.split(",") if s.strip()]
                    bad = [s for s in statuses if s not in ADVERT_STATUSES]
                    if bad:
                        sys.exit(f"Неизвестные статусы: {bad}. Доступно: {', '.join(ADVERT_STATUSES)}")
                secs.append(AdvertSection(statuses))
            if "contracts" in sections:
                secs.append(ContractSection())
            if "plans" in sections:
                secs.append(PlanSection())
            if secs:
                log(f"Открываю {args.workers} вкладк(и/ах)…")
                workers = []
                for i in range(args.workers):
                    w = Worker(i + 1, await ctx.new_page(), args.delay, args.timeout, throttle)
                    await w.open_site()
                    workers.append(w)
                details = {"adverts": not args.no_advert_details, "contracts": not args.no_contract_details,
                           "plans": not args.no_plan_details}
                await Crawler(store, workers, secs, details).run()
                for w in workers:
                    try:
                        await w.page.close()
                    except Exception:
                        pass
            if "files" in sections:
                store.close()  # чтобы прочитать всё, что записано
                await scrape_files(ctx, store, args.file_workers, args.delay, args.min_free_gb)
        finally:
            store.save_state()
            try:
                await browser.close()
            except Exception:
                pass  # при Ctrl+C браузер уже закрыт


def main():
    ap = argparse.ArgumentParser(description="Полный парсер zakup.sk.kz -> JSON")
    ap.add_argument("--sections", default=",".join(SECTIONS),
                    help=f"через запятую: {', '.join(SECTIONS)} (по умолчанию всё)")
    ap.add_argument("--advert-status", default="ALL",
                    help="ALL (по умолчанию), CURRENT (всё, кроме завершённых и отменённых) "
                         "или статусы через запятую, напр. PUBLISHED,PROPOSED")
    ap.add_argument("--workers", type=int, default=5, help="вкладок параллельно (по умолчанию 5)")
    ap.add_argument("--file-workers", type=int, default=4, help="параллельных скачиваний файлов (по умолчанию 4)")
    ap.add_argument("--delay", type=float, default=0.5, help="пауза между запросами, сек (по умолчанию 0.5)")
    ap.add_argument("--timeout", type=float, default=30, help="таймаут ожидания ответа, сек")
    ap.add_argument("--min-free-gb", type=float, default=30,
                    help="не скачивать файлы, если на диске осталось меньше (ГБ, по умолчанию 30)")
    ap.add_argument("--no-advert-details", action="store_true", help="объявления без карточек и лотов")
    ap.add_argument("--no-contract-details", action="store_true", help="договоры без карточек")
    ap.add_argument("--no-plan-details", action="store_true", help="планы без карточек")
    ap.add_argument("--no-complaint-details", action="store_true", help="жалобы без карточек")
    ap.add_argument("--out", default="zakup_all", help="папка для результатов (по умолчанию zakup_all)")
    ap.add_argument("--no-export", action="store_true", help="не собирать *.json в конце (только *.jsonl)")
    ap.add_argument("--export-only", action="store_true",
                    help="только собрать *.json / *.json.gz из уже скачанного и выйти")
    ap.add_argument("--compact", action="store_true",
                    help="только пересжать старые несжатые *_full.jsonl в папке --out и выйти")
    ap.add_argument("--show", action="store_true", help="показывать окна браузера")
    args = ap.parse_args()
    args.workers = max(1, args.workers)
    args.file_workers = max(1, args.file_workers)

    if args.compact:
        Store(Path(args.out)).compact()
        return

    if args.export_only:
        Store(Path(args.out)).export_json()
        return

    finished = False
    try:
        asyncio.run(amain(args))
        finished = True
    except KeyboardInterrupt:
        log("Остановлено — прогресс сохранён, запустите ту же команду, чтобы продолжить.")
    if finished and not args.no_export:
        log("Собираю JSON-файлы (массивы без дублей)…")
        Store(Path(args.out)).export_json()
    elif not finished:
        log("JSON-массивы соберутся после полного завершения (или вручную: --export-only).")
    log(f"Готово. Результаты в папке: {Path(args.out).resolve()}")


if __name__ == "__main__":
    main()
