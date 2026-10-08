# Эксплуатация: бэкапы и безопасность

Всё запускается на VPS (82.26.171.55), из папки проекта `/root/visa-slot-bot`.

## 0. Обновить код и .env

```bash
cd /root/visa-slot-bot
git pull
# добавь в .env новые переменные (возьми из deploy.env.template):
#   ADMIN_CHAT_ID=-1003979376615
#   CHANNEL=@bestvisaeurope
nano .env
docker compose up -d --build bot
```

После этого вся админка, аналитика и алерты уходят в группу `-1003979376615`
(бот уже её админ), а не в личку. Команды админа работают прямо в группе.

## 1. Бэкапы базы в Telegram

`ops/backup.sh` делает `pg_dump`, жмёт в gzip, шлёт файл в админ-чат и хранит
последние 14 копий локально в `/root/backups`.

Проверить вручную:

```bash
cd /root/visa-slot-bot
bash ops/backup.sh
```

Поставить на автозапуск каждый день в 04:00 (по времени сервера):

```bash
( crontab -l 2>/dev/null; echo "0 4 * * * cd /root/visa-slot-bot && bash ops/backup.sh >> /root/backup.log 2>&1" ) | crontab -
crontab -l   # проверить
```

Восстановление из бэкапа:

```bash
gunzip -c /root/backups/visabot-ГГГГММДД-ЧЧММСС.sql.gz | docker compose exec -T db psql -U bot -d visabot
```

## 2. Безопасность сервера (один раз)

```bash
cd /root/visa-slot-bot
bash ops/harden.sh
```

Что делает: ufw (пускает только 22 и 8080), fail2ban против перебора SSH,
автоматические security-обновления, ротация docker-логов, `chmod 600 .env`.

Проверить после:

```bash
ufw status verbose
fail2ban-client status sshd
```

## 3. ВАЖНО: сменить утёкшие секреты

В git-историю когда-то попал `deploy.env.template` с реальными ключами.
Сейчас в файле только плейсхолдеры, но старые значения остались в истории —
их нужно **сменить** (боевые значения живут только в `.env`, он в .gitignore):

- `BOT_TOKEN` — перевыпустить у @BotFather (`/revoke` → `/token`)
- `CRYPTO_PAY_TOKEN` — перевыпустить в @CryptoBot
- `PANEL_KEY`, `FORM_SECRET`, `DATA_KEY` — сгенерировать новые
  (`openssl rand -base64 24`), `FORM_SECRET` обновить и в Apps Script
- пароль почты `SERVICE_EMAIL_PASSWORD` — новый app-password в Gmail
- пароль VFS-аккаунта — сменить на сайте VFS
- пароль root на сервере и GitHub-токен доступа

После смены — обновить `.env` и `docker compose up -d --build bot`.

## 4. Что ещё стоит знать

- Postgres (5432) и FlareSolverr (8191) наружу не торчат — только внутри docker-сети.
- Наружу открыт только веб на 8080: `/panel` закрыт `PANEL_KEY`, `/api/form` — `FORM_SECRET`, `/cases` публичный.
- Docker публикует порты в обход ufw, поэтому 8080 доступен всегда; это ок, т.к. защищён ключами.
