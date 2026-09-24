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
