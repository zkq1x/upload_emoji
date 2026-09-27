from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, Callable

import aiohttp
from yarl import URL

BASE = "https://webappinternal.telegram.org"
APP = BASE + "/stickers"
DEFAULT_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko)"

FLOOD_WAIT_RE = re.compile(r"FLOOD_WAIT_(\d+)", re.I)
FLOOD_TEXT_RE = re.compile(r"too many requests|flood|slow down|try again later", re.I)
DEFAULT_FLOOD_WAIT = 60

log = logging.getLogger("stickers")


class StickersError(Exception):
    pass


class SessionExpired(StickersError):
    pass


class FloodWait(StickersError):
    def __init__(self, seconds: int, text: str):
        super().__init__(f"флуд-лимит: подождать {seconds} с ({text})")
        self.seconds = seconds


class ApiError(StickersError):
    def __init__(self, message: str, field: str | None = None):
        super().__init__(message)
        self.message = message
        self.field = field


class UncertainResult(StickersError):
    pass


class PageFormatChanged(StickersError):
    pass


@dataclass
class Sticker:
    raw: str
    emoji: str


@dataclass
class Pack:
    id: int
    title: str
    is_emoji: bool
    stickers: list[Sticker]
    share_link: str | None = None

    @property
    def raws(self) -> list[str]:
        return [s.raw for s in self.stickers]


@dataclass
class PackInfo:
    id: int
    title: str
    short_name: str | None
    link: str | None
    kind: str | None


class _EditPageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str | None]] = []
        self.row_divs = 0
        self.title: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        classes = (a.get("class") or "").split()
        if tag == "div" and "js-sticker-row" in classes:
            self.row_divs += 1
            if a.get("data-raw"):
                self.rows.append([a["data-raw"], None])
        elif tag == "input" and a.get("name") == "emoji" and self.rows and self.rows[-1][1] is None:
            self.rows[-1][1] = a.get("value") or ""
        elif tag == "input" and a.get("name") == "title" and self.title is None:
            self.title = a.get("value") or ""

    handle_startendtag = handle_starttag


class _PackListParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.packs: list[PackInfo] = []
        self._tab: str | None = None
        self._cur: PackInfo | None = None
        self._field: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        classes = (a.get("class") or "").split()
        if "js-tab-content" in classes:
            self._tab = a.get("data-tab")
        m = re.fullmatch(r"/stickers/pack/(\d+)", a.get("href") or "") if tag == "a" else None
        if m:
            self._cur = PackInfo(int(m.group(1)), "", a.get("data-short-name"), None, self._tab)
            self.packs.append(self._cur)
        elif self._cur and "tm-row-value" in classes:
            self._field = "title"
        elif self._cur and "tm-row-description" in classes:
            self._field = "link"

    def handle_endtag(self, tag: str) -> None:
        if tag == "a":
            self._cur = None
        if tag == "div":
            self._field = None

    def handle_data(self, data: str) -> None:
        if self._cur and self._field == "title":
            self._cur.title += data.strip()
        elif self._cur and self._field == "link":
            self._cur.link = (self._cur.link or "") + data.strip()


def _parse_cookie(header: str) -> dict[str, str]:
    cookies = {}
    for part in header.split(";"):
        name, sep, value = part.strip().partition("=")
        if sep and name:
            cookies[name] = value
    return cookies


def _is_session_expired(status: int, payload: Any, text: str) -> bool:
    if status == 401:
        return True
    if isinstance(payload, dict) and isinstance(payload.get("s"), dict) and payload["s"].get("authPage"):
        return True
    return status == 400 and "tm-auth-expired" in text


def _flood_seconds(status: int, headers: Any, payload: Any) -> int | None:
    if status == 429:
        ra = headers.get("Retry-After", "")
        return int(ra) if ra.isdigit() else DEFAULT_FLOOD_WAIT
    err = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(err, str):
        if m := FLOOD_WAIT_RE.search(err):
            return int(m.group(1))
        if FLOOD_TEXT_RE.search(err):
            return int(payload.get("retry_after") or DEFAULT_FLOOD_WAIT)
    return None


@dataclass
class StickersClient:
    cookie: str
    user_agent: str = DEFAULT_UA
    delay: float = 3.0
    retries: int = 5
    max_flood_wait: int = 900
    timeout: float = 60.0
    _http: aiohttp.ClientSession | None = field(default=None, init=False, repr=False)
    _api_hash: str | None = field(default=None, init=False, repr=False)
    _last_request: float = field(default=0.0, init=False, repr=False)

    async def __aenter__(self) -> "StickersClient":
        jar = aiohttp.CookieJar()
        jar.update_cookies(_parse_cookie(self.cookie), URL(BASE))
        self._http = aiohttp.ClientSession(
            cookie_jar=jar,
            timeout=aiohttp.ClientTimeout(total=self.timeout),
            headers={"User-Agent": self.user_agent, "Accept-Language": "en-US,en;q=0.9"},
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._http:
            await self._http.close()

    async def _pace(self) -> None:
        wait = self._last_request + self.delay * random.uniform(0.8, 1.3) - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_request = time.monotonic()

    async def _backoff(self, attempt: int, why: str) -> None:
        pause = min(60.0, 2.0 ** attempt) * random.uniform(0.7, 1.3)
        log.warning("%s — повтор %d/%d через %.0f с", why, attempt, self.retries, pause)
        await asyncio.sleep(pause)

    async def _request(self, method: str, url: str, *, headers: dict[str, str],
                       data: Callable[[], Any] | None = None, expect_json: bool = True,
                       idempotent: bool = True) -> tuple[int, Any]:
        assert self._http is not None, "используй `async with StickersClient(...)`"
        attempt = 0
        flood_waits = 0
        while True:
            await self._pace()
            try:
                async with self._http.request(method, url, headers=headers,
                                              data=data() if data else None) as resp:
                    status, resp_headers = resp.status, resp.headers
                    text = await resp.text()
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                if not idempotent:
                    raise UncertainResult(f"сетевая ошибка: {e!r}") from e
                attempt += 1
                if attempt > self.retries:
                    raise StickersError(f"сеть недоступна после {self.retries} повторов: {e!r}") from e
                await self._backoff(attempt, f"сетевая ошибка {e!r}")
                continue

            payload: Any = None
            if expect_json or text.lstrip().startswith("{"):
                try:
                    payload = json.loads(text)
                except ValueError:
                    payload = None
            if _is_session_expired(status, payload, text):
                raise SessionExpired()

            flood = _flood_seconds(status, resp_headers, payload)
            if flood is not None:
                if flood > self.max_flood_wait or flood_waits >= 5:
                    raise FloodWait(flood, str(payload.get("error") if isinstance(payload, dict) else status))
                flood_waits += 1
                log.warning("Флуд-лимит, жду %d с", flood)
                await asyncio.sleep(flood + random.uniform(1, 5))
                continue

            if status >= 500 or (expect_json and payload is None):
                why = f"HTTP {status}" + ("" if payload is not None or not expect_json else ", ответ не JSON")
                if not idempotent:
                    raise UncertainResult(why)
                attempt += 1
                if attempt > self.retries:
                    raise StickersError(f"{why} после {self.retries} повторов: {text[:200]!r}")
                await self._backoff(attempt, why)
                continue
            if status >= 400:
                raise StickersError(f"HTTP {status}: {text[:200]!r}")
            return status, payload if expect_json else text

    async def _page(self, path: str, referer: str = APP) -> dict:
        _, payload = await self._request("GET", BASE + path, headers={
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
            "X-Aj-Referer": referer,
            "Referer": referer,
        })
        if not isinstance(payload, dict) or "h" not in payload:
            raise PageFormatChanged(f"{path}: неожиданный формат страницы")
        return payload

    async def refresh_api_hash(self) -> None:
        _, text = await self._request("GET", APP, expect_json=False, headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })
        m = re.search(r'"apiUrl"\s*:\s*"[^"]*?hash=([0-9a-f]+)"', text)
        if not m:
            raise PageFormatChanged("не нашёл apiUrl с hash на странице /stickers")
        self._api_hash = m.group(1)

    async def list_packs(self) -> list[PackInfo]:
        page = await self._page("/stickers")
        parser = _PackListParser()
        parser.feed(page["h"])
        return parser.packs

    async def get_pack(self, pack_id: int) -> Pack:
        edit = await self._page(f"/stickers/editpack/{pack_id}", f"{APP}/pack/{pack_id}")
        state = edit.get("s") or {}
        if str(state.get("packId")) != str(pack_id):
            raise PageFormatChanged(f"editpack/{pack_id}: в state packId={state.get('packId')!r}")
        parser = _EditPageParser()
        parser.feed(edit["h"])
        rows = parser.rows
        if not rows or parser.row_divs != len(rows) or any(e is None or not e for _, e in rows):
            raise PageFormatChanged(
                f"editpack/{pack_id}: разобрано {len(rows)} стикеров из {parser.row_divs} строк, "
                "или у кого-то пустой эмодзи")
        if parser.title is None:
            raise PageFormatChanged(f"editpack/{pack_id}: не нашёл название пака")

        view = await self._page(f"/stickers/pack/{pack_id}", APP)
        view_raws = re.findall(r'class="[^"]*tm-grid-item[^"]*"[^>]*data-raw="([^"]+)"', view["h"])
        if view_raws != [r for r, _ in rows]:
            raise PageFormatChanged(
                f"пак {pack_id}: страницы редактирования и просмотра разошлись "
                f"({len(rows)} vs {len(view_raws)} стикеров)")

        return Pack(
            id=pack_id,
            title=parser.title,
            is_emoji=bool(state.get("isEmojis")),
            stickers=[Sticker(r, e) for r, e in rows],
            share_link=(view.get("s") or {}).get("shareLink"),
        )

    async def _api(self, referer: str, data: Callable[[], Any], *, idempotent: bool = True) -> dict:
        if self._api_hash is None:
            await self.refresh_api_hash()
        for hash_refreshed in (False, True):
            _, payload = await self._request(
                "POST", f"{APP}/api?hash={self._api_hash}", data=data, idempotent=idempotent,
                headers={
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                    "X-Requested-With": "XMLHttpRequest",
                    "Origin": BASE,
                    "Referer": referer,
                })
            if not isinstance(payload, dict):
                raise StickersError(f"неожиданный ответ API: {payload!r}")
            if payload.get("error") == "Bad request" and not hash_refreshed:
                log.info("Сервер ответил «Bad request» — обновляю hash API")
                await self.refresh_api_hash()
                continue
            if payload.get("error"):
                log.debug("API error: %r", payload)
                raise ApiError(str(payload["error"]), payload.get("field"))
            if not payload.get("ok"):
                raise StickersError(f"неожиданный ответ API: {payload!r}")
            return payload
        raise AssertionError("unreachable")

    async def upload(self, pack_id: int, tgs: bytes, filename: str) -> str:
        def form() -> aiohttp.FormData:
            fd = aiohttp.FormData()
            fd.add_field("file", tgs, filename=filename, content_type="application/x-tgsticker")
            fd.add_field("method", "upload")
            fd.add_field("target", "emoji")
            return fd

        res = await self._api(f"{APP}/editpack/{pack_id}", form)
        return str(res["doc"])

    async def update_sticker_set(self, pack: Pack, stickers: list[dict[str, str]]) -> str:
        form: list[tuple[str, str]] = [("pack_id", str(pack.id))]
        for i, s in enumerate(stickers):
            key = "sticker_raw" if s.get("sticker_raw") else "doc_id"
            form += [(f"stickers[{i}][{key}]", s[key]), (f"stickers[{i}][emoji]", s["emoji"])]
        form += [("title", pack.title), ("method", "updateStickerSet")]
        res = await self._api(f"{APP}/editpack/{pack.id}", lambda: form, idempotent=False)
        return re.sub(r"<[^>]+>", "", str(res.get("msg", "")))
