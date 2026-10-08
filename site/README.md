# Easy Visa — сайт

Статический сайт на Astro для привлечения пользователей в @Easyvisaforyoubot. Стратегия, запуск, домен, почта, аналитика и план — в [docs/SEO-PLAN.md](docs/SEO-PLAN.md).

```bash
cd site
npm install
npm run dev                      # http://localhost:4321
SITE_URL=https://домен npm run build   # dist/ + проверка SEO (scripts/check-build.mjs)
```

- Факты (цены, колесо, рефералка, страны): `src/site.ts`
- Статьи: `src/content/gid/*.md` — новый файл = новая страница в /gid/
- Картинки og.png и иконки: `CHROMIUM_PATH=... node scripts/make-images.mjs`
- Переменные окружения: `SITE_URL` (без неё сборка noindex), `PUBLIC_GA_ID`, `PUBLIC_GSC_VERIFICATION`, `PUBLIC_CONTACT_EMAIL`
