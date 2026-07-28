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
