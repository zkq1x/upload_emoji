#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

from stickers_api import (DEFAULT_UA, ApiError, FloodWait, PageFormatChanged, SessionExpired,
                          StickersClient, StickersError, UncertainResult)
from tgs_convert import convert_svg

MAX_PACK_SIZE = 200
DOC_TTL = 3600
MAX_UNCERTAIN = 3

log = logging.getLogger("upload_emoji")

SESSION_HELP = """Сессия @Stickers протухла (сервер ответил «Session expired»).
  1. Открой @Stickers в Telegram и запусти мини-апп.
  2. В Web Inspector возьми заголовок Cookie любого запроса к webappinternal.telegram.org.
  3. Вставь его в .env → STICKERS_COOKIE="…".
Прогресс сохранён в {state} — запусти ту же команду, продолжится с места остановки."""


@dataclass
class Job:
    name: str
    emoji: str
    sha: str
    tgs: bytes


class State:
    def __init__(self, path: Path):
        self.path = path
        self.data: dict = {"version": 1, "pack_id": None, "items": {}, "pending": None}
        if path.exists():
            self.data = json.loads(path.read_text("utf-8"))

    @property
    def items(self) -> dict[str, dict]:
        return self.data["items"]

    def save(self) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), "utf-8")
        os.replace(tmp, self.path)


def load_mapping(path: Path, svg_dir: Path) -> dict[str, str]:
    if not path.is_file():
        raise SystemExit(f"Нет файла маппинга {path} (пример — mapping.example.json)")
    data = json.loads(path.read_text("utf-8"))
    if not isinstance(data, dict):
        raise SystemExit(f"{path}: нужен JSON-объект «файл → эмодзи»")
    mapping: dict[str, str] = {}
    problems = []
    for name, emoji in data.items():
        file = name if name.lower().endswith(".svg") else name + ".svg"
        if isinstance(emoji, list):
            emoji = "".join(map(str, emoji))
        emoji = re.sub(r"\s+", "", emoji) if isinstance(emoji, str) else ""
        if not emoji:
            problems.append(f"{name}: пустой эмодзи")
        elif re.search(r"[A-Za-z]", emoji):
            problems.append(f"{name}: {emoji!r} не похоже на эмодзи")
        elif not (svg_dir / file).is_file():
            problems.append(f"{name}: нет файла {svg_dir / file}")
        else:
            mapping[file] = emoji
    for p in problems:
        log.error("Маппинг: %s", p)
    if problems:
        raise SystemExit("Исправь маппинг и запусти снова")
    unmapped = sorted(p.name for p in svg_dir.glob("*.svg") if p.name not in mapping)
    if unmapped:
        log.warning("Нет в маппинге, пропускаю: %s", ", ".join(unmapped))
    return mapping


def build(args: argparse.Namespace, mapping: dict[str, str], state: State | None = None) -> tuple[list[Job], int]:
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    jobs: list[Job] = []
    invalid = 0
    for name, emoji in mapping.items():
        path = args.svg_dir / name
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        item = state.items.get(name, {}) if state else {}
        if item.get("status") == "added":
            if item.get("sha") != sha:
                log.warning("%s: уже в паке, но SVG изменился — не трогаю "
                            "(чтобы добавить заново, удали запись из %s)", name, state.path)
            elif item.get("emoji") != emoji:
                log.warning("%s: уже в паке с %s, в маппинге %s — эмодзи не меняю", name, item["emoji"], emoji)
            continue
        if item.get("status") == "failed" and item.get("sha") == sha and item.get("emoji") == emoji:
            log.warning("%s: пропускаю, сервер уже отклонял его: %s (исправь файл или эмодзи)", name, item["error"])
            continue

        res = convert_svg(path, duration=args.duration, fit=args.fit, padding=args.padding)
        for w in res.warnings:
            log.warning("%s: %s", name, w)
        if not res.ok:
            invalid += 1
            log.error("%s: не проходит требования Telegram: %s", name, "; ".join(res.errors))
            continue
        assert res.data is not None
        (out / (path.stem + ".tgs")).write_bytes(res.data)
        log.info("%s → %s (%.1f КБ) %s", name, out / (path.stem + ".tgs"), len(res.data) / 1024, emoji)
        jobs.append(Job(name, emoji, sha, res.data))
    return jobs, invalid


async def reconcile(api: StickersClient, state: State) -> str:
    pending = state.data.get("pending")
    if not pending:
        return "none"
    pack = await api.get_pack(state.data["pack_id"])
    before, files, after = pending["before"], pending["files"], pack.raws
    lost = [r for r in before if r not in set(after)]
    new = [r for r in after if r not in set(before)]
    if lost:
        raise StickersError(
            f"из пака пропали стикеры, которые были до обновления: {lost}. Останавливаюсь. "
            f"Если ты удалял их вручную — убери поле pending из {state.path}")
    if not new:
        state.data["pending"] = None
        state.save()
        return "not_applied"
    if len(new) == len(files) and after[len(before):] == new:
        emoji_by_raw = {s.raw: s.emoji for s in pack.stickers}
        for name, raw in zip(files, new):
            item = state.items[name]
            item.update(status="added", sticker_raw=raw, added_at=time.time())
            item.pop("doc", None)
            item.pop("uploaded_at", None)
            log.info("%s: в паке %s", name, emoji_by_raw[raw])
        state.data["pending"] = None
        state.save()
        return "applied"
    raise StickersError(
        f"пак изменился не так, как ожидалось: ждал {len(files)} новых в конце, вижу {new}. "
        f"Проверь пак вручную; запись pending оставлена в {state.path}")


async def upload_one(api: StickersClient, state: State, pack_id: int, job: Job, *,
                     force: bool = False) -> str | None:
    item = state.items.get(job.name, {})
    if (not force and item.get("status") == "uploaded" and item.get("sha") == job.sha
            and time.time() - item.get("uploaded_at", 0) < DOC_TTL):
        return "reused"
    try:
        doc = await api.upload(pack_id, job.tgs, Path(job.name).stem + ".tgs")
    except ApiError as e:
        mark_failed(state, job, f"upload: {e.message}")
        return None
    state.items[job.name] = {"status": "uploaded", "doc": doc, "uploaded_at": time.time(),
                             "sha": job.sha, "emoji": job.emoji}
    state.save()
    log.info("%s: загружен", job.name)
    return "uploaded"


def mark_failed(state: State, job: Job, error: str) -> None:
    log.error("%s: сервер отклонил: %s", job.name, error)
    state.items[job.name] = {"status": "failed", "error": error, "sha": job.sha, "emoji": job.emoji}
    state.save()


async def add_to_pack(api: StickersClient, state: State, pack_id: int, jobs: list[Job],
                      fresh: set[str]) -> None:
    uncertain = 0
    while jobs:
        pack = await api.get_pack(pack_id)
        stickers = [{"sticker_raw": s.raw, "emoji": s.emoji} for s in pack.stickers]
        stickers += [{"doc_id": state.items[j.name]["doc"], "emoji": j.emoji} for j in jobs]
        state.data["pending"] = {"before": pack.raws, "files": [j.name for j in jobs], "at": time.time()}
        state.save()

        error: ApiError | None = None
        try:
            log.info("Обновляю пак: %d + %d", len(pack.stickers), len(jobs))
            msg = await api.update_sticker_set(pack, stickers)
            log.info("Сервер: %s", msg)
        except UncertainResult as e:
            uncertain += 1
            log.warning("Ответ на обновление пака не получен (%s) — сверяю пак", e)
            if uncertain > MAX_UNCERTAIN:
                raise StickersError("обновление пака не удаётся подтвердить, попробуй позже") from e
        except ApiError as e:
            error = e

        if await reconcile(api, state) == "applied":
            return
        if error is None:
            if uncertain:
                continue
            raise StickersError("сервер ответил ok, но новых эмодзи в паке нет")

        field = error.field or ""
        culprit = next((j for j in jobs if field in (f"i{state.items[j.name]['doc']}", state.items[j.name]["doc"])), None)
        if culprit:
            mark_failed(state, culprit, error.message)
            jobs = [j for j in jobs if j is not culprit]
            continue
        if field.startswith("s"):
            raise StickersError(f"сервер ругается на стикер, который уже в паке ({field}): {error.message}")
        if len(jobs) > 1:
            log.warning("Ошибка «%s» без указания файла — добавляю по одному", error.message)
            for j in jobs:
                await add_to_pack(api, state, pack_id, [j], fresh)
            return
        job = jobs[0]
        if job.name not in fresh:
            log.warning("%s: %s — перезагружаю файл и пробую ещё раз", job.name, error.message)
            fresh.add(job.name)
            if await upload_one(api, state, pack_id, job, force=True) is None:
                return
            continue
        mark_failed(state, job, error.message)
        return


async def resolve_pack(api: StickersClient, ref: str) -> int:
    if ref.isdigit():
        return int(ref)
    name = ref.rstrip("/").rsplit("/", 1)[-1].lower()
    for p in await api.list_packs():
        if (p.short_name or "").lower() == name:
            return p.id
    raise SystemExit(f"Пак {ref!r} не найден среди твоих паков — см. `python upload_emoji.py packs`")


def make_client(args: argparse.Namespace) -> StickersClient:
    cookie = os.environ.get("STICKERS_COOKIE", "").strip()
    if not cookie:
        raise SystemExit(f"В {args.env} нет STICKERS_COOKIE — см. .env.example")
    return StickersClient(cookie, user_agent=os.environ.get("STICKERS_USER_AGENT") or DEFAULT_UA,
                          delay=args.delay, retries=args.retries, max_flood_wait=args.max_flood_wait)


async def cmd_packs(args: argparse.Namespace) -> None:
    async with make_client(args) as api:
        packs = await api.list_packs()
    print(f"{'id':>4}  {'тип':8}  {'short name':24}  название")
    for p in packs:
        print(f"{p.id:>4}  {p.kind or '?':8}  {p.short_name or '—':24}  {p.title}")


async def cmd_convert(args: argparse.Namespace) -> None:
    mapping = load_mapping(args.mapping, args.svg_dir)
    jobs, invalid = build(args, mapping)
    log.info("Готово: %d TGS в %s, не прошли проверку: %d", len(jobs), args.out, invalid)


async def cmd_upload(args: argparse.Namespace) -> None:
    mapping = load_mapping(args.mapping, args.svg_dir)
    state = State(args.state)
    async with make_client(args) as api:
        pack_id = await resolve_pack(api, args.pack)
        if state.data["pack_id"] not in (None, pack_id):
            raise SystemExit(f"{args.state} относится к паку {state.data['pack_id']}, "
                             f"а сейчас выбран {pack_id}. Укажи другой --state")
        state.data["pack_id"] = pack_id

        if not args.dry_run and await reconcile(api, state) != "none":
            log.info("Сверка с прошлым запуском выполнена")

        pack = await api.get_pack(pack_id)
        if not pack.is_emoji:
            raise SystemExit(f"Пак {pack_id} — это стикеры, а не эмодзи")
        log.info("Пак «%s» (id %d): %d эмодзи%s", pack.title, pack.id, len(pack.stickers),
                 f", {pack.share_link}" if pack.share_link else " (черновик, не опубликован)")

        jobs, invalid = build(args, mapping, state)
        room = MAX_PACK_SIZE - len(pack.stickers)
        if len(jobs) > room:
            log.warning("В паке место только для %d из %d — остальные пропускаю", max(room, 0), len(jobs))
            jobs = jobs[:max(room, 0)]

        if args.dry_run:
            log.info("--dry-run: добавил бы %d в конец пака (стало бы %d), ничего не отправляю:",
                     len(jobs), len(pack.stickers) + len(jobs))
            for j in jobs:
                log.info("  + %s %s", j.name, j.emoji)
            return

        fresh: set[str] = set()
        for i in range(0, len(jobs), args.batch):
            batch = []
            for job in jobs[i:i + args.batch]:
                result = await upload_one(api, state, pack_id, job)
                if result == "uploaded":
                    fresh.add(job.name)
                if result:
                    batch.append(job)
            if batch:
                await add_to_pack(api, state, pack_id, batch, fresh)

        added = sum(1 for n in mapping if state.items.get(n, {}).get("status") == "added")
        failed = {n: state.items[n]["error"] for n in mapping if state.items.get(n, {}).get("status") == "failed"}
        log.info("Итого: в паке %d из %d по маппингу; не прошли локальную проверку: %d; отклонены сервером: %d",
                 added, len(mapping), invalid, len(failed))
        for n, e in failed.items():
            log.info("  ✗ %s: %s", n, e)


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Загрузка custom emoji из SVG в пак Telegram через мини-апп @Stickers")
    ap.add_argument("--env", type=Path, default=Path(__file__).resolve().with_name(".env"),
                    help="файл с секретами (по умолчанию .env рядом со скриптом)")
    ap.add_argument("--log", type=Path, default=Path("upload_emoji.log"), help="подробный лог")
    ap.add_argument("--delay", type=float, default=3.0, help="пауза между запросами, с (по умолчанию 3)")
    ap.add_argument("--retries", type=int, default=5, help="повторы при сетевых ошибках и 5xx")
    ap.add_argument("--max-flood-wait", type=int, default=900,
                    help="максимум ожидания при флуд-лимите, с; дольше — выход с сохранением прогресса")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("packs", help="показать твои паки").set_defaults(func=cmd_packs)

    def conv_opts(p: argparse.ArgumentParser) -> None:
        p.add_argument("svg_dir", type=Path, help="папка с SVG")
        p.add_argument("-m", "--mapping", type=Path, help="JSON «файл → эмодзи» (по умолчанию SVG_DIR/mapping.json)")
        p.add_argument("--out", type=Path, default=Path("build"), help="куда класть TGS (по умолчанию build/)")
        p.add_argument("--duration", type=float, default=1.0, help="длительность, с (≤ 3)")
        p.add_argument("--fit", choices=["content", "viewbox"], default="content",
                       help="content — вписать сами фигуры; viewbox — сохранить рамку SVG")
        p.add_argument("--padding", type=float, default=0.0, help="отступ от края канваса 512, px")

    p = sub.add_parser("convert", help="SVG → TGS и проверка, без сети")
    conv_opts(p)
    p.set_defaults(func=cmd_convert)

    p = sub.add_parser("upload", help="конвертировать и загрузить в пак")
    conv_opts(p)
    p.add_argument("--pack", required=True, help="id пака, short name или ссылка t.me/addemoji/…")
    p.add_argument("--state", type=Path, default=Path("state.json"), help="файл прогресса")
    p.add_argument("--batch", type=int, default=10, help="сколько эмодзи добавлять за одно обновление пака")
    p.add_argument("--dry-run", action="store_true", help="только показать план, ничего не загружать")
    p.set_defaults(func=cmd_upload)

    args = ap.parse_args()
    if getattr(args, "svg_dir", None) and args.mapping is None:
        args.mapping = args.svg_dir / "mapping.json"
    return args


def setup_logging(path: Path) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S")
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    console.setLevel(logging.INFO)
    file = logging.FileHandler(path, encoding="utf-8")
    file.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.DEBUG, handlers=[console, file])
    logging.getLogger("asyncio").setLevel(logging.WARNING)


def main() -> None:
    args = parse_args()
    setup_logging(args.log)
    load_dotenv(args.env)
    state_path = getattr(args, "state", "state.json")
    try:
        asyncio.run(args.func(args))
    except SessionExpired:
        log.error(SESSION_HELP.format(state=state_path))
        sys.exit(2)
    except FloodWait as e:
        resume = datetime.now() + timedelta(seconds=e.seconds)
        log.error("%s. Прогресс сохранён — запусти ту же команду после %s", e, resume.strftime("%H:%M"))
        sys.exit(3)
    except PageFormatChanged as e:
        log.error("Разметка мини-аппа не та, что ожидалась: %s. Останавливаюсь, чтобы не повредить пак.", e)
        sys.exit(1)
    except StickersError as e:
        log.error("%s", e)
        sys.exit(1)
    except KeyboardInterrupt:
        log.warning("Прервано. Прогресс в %s — запусти ту же команду, чтобы продолжить.", state_path)
        sys.exit(130)


if __name__ == "__main__":
    main()
