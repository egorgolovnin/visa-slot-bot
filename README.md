# Visa Slot Bot — фаза 0

Telegram-бот следит за публичным API VFS Global (Кипр) и пишет, когда у страны появляется
ближайшая дата подачи. Без логина и капчи. Копит историю, чтобы понять, когда выбрасывают слоты.

Стек: Python 3.12, aiogram 3, asyncpg, curl_cffi (TLS как у Chrome), Postgres.

## Запуск на Railway
1. Залей репозиторий на GitHub (приватный).
2. Railway → New Project → Deploy from GitHub repo.
3. В проект добавь Postgres (+ New → Database → PostgreSQL).
4. В переменные сервиса бота внеси значения из `.env.example`.
5. Открой бота в Telegram, нажми /start. Первая разведка стран займёт 2–3 минуты.

Если через 3 цикла придёт «бот ослеп» с ошибками 403 — Cloudflare режет IP Railway.
Пропиши `PROXY_URL` с резидентным прокси (Кипр или ЕС) и сделай /discover.

## Команды
/sub, /window, /status, /history, /stats — для всех.
/health, /discover, /users, /allow, /deny — для админа.
Друзья жмут /start, тебе приходит кнопка «Разрешить».

## Локально
```
pip install -r requirements.txt
cp .env.example .env  # заполни, DATABASE_URL на локальный Postgres
set -a; . ./.env; set +a; python -m app.main
pytest -q tests
```
