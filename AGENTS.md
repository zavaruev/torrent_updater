# AGENTS.md

## Быстрый старт
- **Локально**: `python main.py` (требуется `.env`)
- **Docker**: `docker-compose up -d --build`
- **Веб-интерфейс**: http://localhost:6050

## Обязательные переменные (`.env`)
```
LOGIN_RUTRACKER=<логин>
PASSWORD_RUTRACKER=<пароль>
TR_HOST=<хост transmission>
TR_PORT=9091
TR_USER=<пользователь transmission>
TR_PASSWORD=<пароль transmission>
DOWNLOAD_DIR=/путь/к/загрузкам
```

## Опционально: вход по кукам (обход капчи)
Если Rutracker требует капчу на форме входа, скопируй куки из своего браузера
(F12 → Application → Cookies → `https://rutracker.org`: `bb_session`, `bb_data`):
```
RUTRACKER_BB_SESSION=<значение bb_session>
RUTRACKER_BB_DATA=<значение bb_data>
```
После добавления — `docker compose up -d`. Вход по кукам пробуется первым,
форма логина — запасной вариант.

## Известное ограничение (сент. 2026)
Cloudflare показывает интерактивную проверку на страницах раздач/трекера для
автоматизированного Chrome (датацентр-IP + флаги автоматизации): клик по
чекбоксу серверно отклоняется. Проверки дат/поиск/скачивание на этих страницах
сейчас упираются в неё; индекс, статусы, история, рекомендации — работают.
Поиск по запросу (`tracker.php?nm=`) реализован (`RutrackerScraper.search_tracker`
+ фильтр слов запроса), но ждёт прохождения проверки для E2E-теста.

## Jellyfin: идентификация медиа (сент. 2026)
- **DNS**: в `docker-compose.yml` Jellyfin добавлен `dns: [<HOME_DNS>]` (IP
  роутера из `.env`) — без этого `api.themoviedb.org` блокируется (DNS отдаёт
  127.0.0.1, даже 8.8.8.8 хакнут), все онлайн-провайдеры падают с
  "Connection refused" и контент не идентифицируется. DNS роутера (fake-ip
  туннеля) отдаёт рабочий адрес. В этом репозитории: `dns: ${HOME_DNS:?}`
  берётся из `.env` — внутренние IP в git не хранятся.
- **NFO для фильмов**: `post_process_downloads.py` создаёт NFO только при
  наличии лейбла `imdb_*` у торрента. Без NFO Jellyfin берёт встроенный Title-тег
  MKV (в релизах spartanec это мусор "Release by spartanec"). См. TODO(imdb-resolve)
  в `main.py` — резолв imdb_id при ручном поиске.
- **Корректный imdbid**: The Simpsons Movie (2007) = `tt0462538`
  (НЕ tt0449088 — это Пираты Карибского моря). Проверять через
  `https://v2.sg.media-imdb.com/suggestion/<буква>/<title>.json`.
- **ОПАСНО: `DELETE /Items/{id}?deleteFile=false` в Jellyfin удаляет файлы!**
  Параметр не сработал (июль-версия API), в логе "Deleting item path ... .mkv" —
  файл 25 ГБ и NFO удалились, пришлось перекачивать. Никогда не удалять элементы
  библиотеки через API — только переидентифицировать (Refresh) или править NFO.
- **Refresh API**: `POST /Items/{id}/Refresh?MetadataRefreshMode=2&ImageRefreshMode=2`
  (enum только числом: 2=Full/DownloadAll), auth: `Authorization: MediaBrowser Token=<key>`.
  Ключ в таблице `ApiKeys` (имя `hermes`) в `jellyfin.db`.

## Фильтр совместимости с оборудованием (LE-zal, сент. 2026)
- **LE-zal = Kodi 21.3**, в HomeAssistant запись `kodi` → `<LE_ZAL_IP>:8080`
  (IP приставки хранится в `.env` как `LE_ZAL_HOST`, в git не коммитится;
  есть ещё LE-Kitchen/LE-spalnya/LE-vlada — не путать). Требование пользователя:
  скачивать только раздачи, которые приставка проиграет без проблем.
- **Исключено: HEVC/x265/H265/H.265, 2160p/4K/UHD, HDR/HDR10, DV (Dolby Vision).**
  Единственный источник — `EXCLUDE_KEYWORDS` в `movie-recommender/rutracker_scraper.py`;
  `on_demand_download.py` импортирует его (локальной копии больше нет).
- Матчинг — общий вход `is_excluded_title()`: по началу слова (`_kw_start_re`,
  lookbehind) для основного списка + целое слово (`_whole_word_re`) для коротких
  ключей `EXCLUDE_WHOLE_WORDS` (`TS`, `TC`, `MOD`, `Scr`) — иначе `'MOD'` заденет
  "Modern Family", `'Scr'` — "Scrubs", `'TS'` — "Tsunami". Подстрочный матчинг
  не использовать: `'DV'` поймал бы "Adventure".
- Таблицы скоринга (`quality_rank` в `search_and_download.py`/`recommender.py`/
  `QUALITY_RANK` в `on_demand_download.py`) очищены от 4K/HDR/DV/HEVC-бонусов.
- Пригодны для LE-zal: h264/AVC/x264, 1080p/720p, WEB-DL/BDRip/Remux.
  DTS-аудио: если приставка без ресивера — Kodi делает даунмикс (ок).

## Архитектура
- **Точка входа**: `main.py:562` — `check_and_update_torrents` по расписанию
- **Веб-сервер**: FastAPI на порту 6050, шаблон `templates/index.html`
- **Chrome**: headless undetected-chromedriver для логина и парсинга Rutracker
- **Transmission RPC**: клиент `transmission-rpc` для управления торрентами

## Ключевые особенности
- Проверка обновлений с интервалом `CHECK_INTERVAL` (`hours`/`minutes`/`days`)
- Обнаружение завершённых сезонов (`Серии: 1-X из X`) — автоудаление из Transmission
- Новый торрент скачивается до удаления старого (отказоустойчивость)
- Последние 100 логов в буфере для веб-интерфейса

## Важно
- Chrome устанавливается в Docker (Dockerfile строки 9-19)
- `page_load_timeout(60)` — предотвращает зависание на 120+ секунд при Cloudflare-челлендже (`main.py:257`)
- Навигация через `execute_script("window.location.href=...")` в `_try_check_torrent` и `download_and_add_torrent` — избегает блокировки page_load_timeout
- После клика Login: `driver.get("index.php")` обёрнут в `try/except Exception` с `window.stop()` — Cloudflare может задерживать рендер даже после успешного логина, таймаут renderer'а не мешает продолжить
- Telegram-уведомления закомментированы (требуются `TELEGRAM_BOT_TOKEN` и `TELEGRAM_CHAT_ID`)
