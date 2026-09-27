# upload_emoji

Загружает custom emoji из SVG в свой пак Telegram через мини-апп @Stickers.
SVG конвертируются в TGS без растеризации и проверяются по [требованиям Telegram](https://core.telegram.org/stickers).

```bash
pip install -r requirements.txt
cp .env.example .env   # вставь Cookie из Web Inspector мини-аппа
```

```bash
python upload_emoji.py packs
python upload_emoji.py upload svg/ --pack <id|short_name> --dry-run
python upload_emoji.py upload svg/ --pack <id|short_name>
```

Эмодзи задаются в `svg/mapping.json` (см. `mapping.example.json`).
Прогресс хранится в `state.json`: повторный запуск продолжит с места остановки.
