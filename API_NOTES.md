# @Stickers mini-app API — проверенные схемы

Снято 2026-09-27 живыми запросами на тестовом черновике пака и по публичному JS
(`/js/bot-stickers.js?17`, `telegram.org/js/main-aj.js?75`). Секретов здесь нет, они в `.env`.

## Транспорт

- База: `https://webappinternal.telegram.org`
- API: `POST /stickers/api?hash=<HASH>`, заголовки `X-Requested-With: XMLHttpRequest`, `Origin`, `Referer`, `Cookie`.
  Действие — поле `method`. Обычные методы — `application/x-www-form-urlencoded`,
  массивы в стиле jQuery: `stickers[0][emoji]=…`. `upload` — multipart.
- Авторизация — только куки (`stel_bot_stickers_token`, `stel_ssid`; `stel_dt`/`stel_ln` — настройки).
- `HASH` лежит в полной HTML-странице `GET /stickers` (без `X-Requested-With`):
  `ajInit({"version":646,"apiUrl":"\/api?hash=…",…})`. hash привязан к сессии (без кук страница
  отдаёт другой), скрипт берёт его сам и обновляет при `Bad request`.
- Страницы (`GET` с `X-Requested-With` + `X-Aj-Referer`) возвращают JSON
  `{"v","t","h":<html>,"j":<js>,"s":<state>}`.

## Ответы и ошибки

| Ситуация | HTTP | Тело |
|---|---|---|
| Успех | 200 | `{"ok":true, …}` |
| Ошибка метода | 200 | `{"error":"<текст>"}`, иногда + `"field"`: `s<sticker_raw>` (старый стикер) или `i<doc_id>` (новый) |
| Неверный `hash` | 200 | `{"error":"Bad request"}` |
| Сессия протухла / нет кук | **400** | Aj-страница: `"s":{"authPage":true}`, в `h` текст `Session expired`; полная HTML-страница — `div.tm-auth-expired` |

JS клиента ещё обрабатывает 401 → `/auth`, но на практике пришёл 400.
Флуд-лимит поймать не удалось — формат неизвестен (TODO).

## Методы

### upload (multipart)
`file` (`application/x-tgsticker`), `method=upload`, `target=emoji`
→ `{"ok":true,"doc":"<doc_id>"}`.
Невалидный TGS (в т.ч. неверный канвас) → `{"error":"File format is not supported"}` —
причину сервер не пишет, её должен логировать клиент.

### createStickerSet — новый пак (черновик)
`title`, `stickers[i][doc_id]`, `stickers[i][emoji]`, `short_name` (пусто → черновик),
`is_emoji=1`, `text_color` (пусто) → `{"ok":true,"redirect":"/stickers/editpack/<pack_id>?new=1"}`.

### updateStickerSet — изменение пака
`pack_id`, `title`, `stickers[i][sticker_raw]` (уже в паке) **или** `stickers[i][doc_id]` (новый upload),
`stickers[i][emoji]`, опц. `thumb` → `{"ok":true,"msg":"<b>…</b> updated successfully."}`.

**Передаётся ВЕСЬ список.** Удаление стикера в UI — это просто отсутствие его строки
в запросе. Отправить только новые = стереть старые. Порядок массива = порядок в паке
(новые, добавленные в конец, встали в конец).

Несколько эмодзи на стикер — одной строкой (`🔵🔷💙`), работает.
Не-эмодзи → `{"error":"Emoji not supported","field":"s<sticker_raw>"}` для старого стикера,
`"field":"i<doc_id>"` для нового. При ошибке пак не меняется (запрос атомарный).

### Текущий список стикеров
`GET /stickers/editpack/<pack_id>` → в `h` строки
`<div class="… js-sticker-row …" data-raw="<sticker_raw>" …> … <input name="emoji" value="<emoji>">`.
Проверено на черновике и на опубликованных паках (число строк совпадает со страницей пака).
Опубликованный пак отличается наличием `shareLink` в `s` страницы `/stickers/pack/<id>`.
`sticker_raw` после публикации не меняются.

Живой тест на **опубликованном** тестовом паке: 3 старых (`sticker_raw`) + 3 новых (`doc_id`)
в одном `updateStickerSet` → стало 6, старые на месте, новые в конце в том же порядке.

### Прочее
- `checkShortName`: `value` → `{"ok":true,"msg":"… is available."}`
- `publishStickerSet`: `pack_id`, `title`, `short_name` →
  `{"ok":true,"redirect":"/stickers/pack/<id>","msg":"… published successfully."}`.
  Страница `publishpack/<id>` в `s.short_name` подсказывает имя, сделанное из title.
- `deleteStickerSet`: `pack_id`. **В скрипте не используется.**
- `getEmojiKeywords`, `getStickerPicker`, `getStickerHtml` — нужны только интерфейсу.

## Открытые вопросы (TODO)
- Формат флуд-лимита.
- Сколько живёт `doc_id` после upload (для докачки).
- Лимит 200 эмодзи в паке — из документации Telegram, на этом API не проверялся.

## Заметки по python-lottie 0.7.2
- `Animation.scale()` прижимает неквадратный SVG к левому верхнему углу — центрировать самим.
- `TgsValidator` пропускает 30 fps, а Telegram требует 60 — проверять самим.
