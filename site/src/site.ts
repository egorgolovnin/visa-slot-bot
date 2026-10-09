/**
 * Single source of truth for facts shown on the site.
 * Everything here mirrors the bot (visa-slot-bot/app). If the bot changes, change it here too:
 *   prices      -> PRICE_EUR / FAMILY_PRICE / DOCS_PRICE_EUR in the bot's .env
 *                  (the person price also appears in src/content/gid/*.md — grep "99 €")
 *   wheel       -> WHEEL in app/promo.py
 *   referral    -> REFERRAL_PERCENT in app/orders.py
 *   countries   -> HELP text in app/bot.py
 */

export const BRAND = 'Easy Visa';
export const BOT_USERNAME = 'Easyvisaforyoubot';
export const BOT_URL = `https://t.me/${BOT_USERNAME}`;

/** Client-facing manager on Telegram (second contact channel besides the bot's /support). */
export const MANAGER_USERNAME = 'bestvisamanager';
export const MANAGER_NAME = 'Алена';
export const MANAGER_URL = `https://t.me/${MANAGER_USERNAME}`;

/** Facts last checked against the bot code and official pages. Shown on pages as "проверено". */
export const FACTS_CHECKED = '2026-10-08';

export const PRICE = {
  person: 99,
  family: 149, // 2 adults + at least one child under 18, flat for all
  docs: 250,
};

/** Fortune wheel sectors exactly as in app/promo.py WHEEL: [discount %, weight]. Weights sum to 100. */
export const WHEEL: { pct: number; chance: number }[] = [
  { pct: 5, chance: 34 },
  { pct: 10, chance: 30 },
  { pct: 15, chance: 20 },
  { pct: 20, chance: 12 },
  { pct: 30, chance: 3 },
  { pct: 100, chance: 1 },
];

export const REFERRAL_PCT = 50;

/** Public e-mail on the site's domain. Empty until the domain and mailbox exist — then pages show it. */
export const CONTACT_EMAIL = process.env.PUBLIC_CONTACT_EMAIL || '';

/** Countries the bot books in Cyprus, and through which system. */
export type Country = {
  name: string;
  system: 'VFS Global' | 'посольство Германии' | 'посольство Финляндии';
  note?: string;
};
export const BOT_COUNTRIES: Country[] = [
  { name: 'Австрия', system: 'VFS Global' },
  { name: 'Венгрия', system: 'VFS Global', note: 'сбор визового центра 22,80 € оплачивается при записи' },
  { name: 'Словакия', system: 'VFS Global' },
  { name: 'Словения', system: 'VFS Global' },
  { name: 'Хорватия', system: 'VFS Global' },
  { name: 'Чехия', system: 'VFS Global' },
  { name: 'Финляндия', system: 'VFS Global' },
  { name: 'Швеция', system: 'VFS Global' },
  { name: 'Германия', system: 'посольство Германии' },
  { name: 'Нидерланды', system: 'посольство Германии' },
  { name: 'Бельгия', system: 'посольство Германии' },
  { name: 'Дания', system: 'посольство Германии' },
  { name: 'Норвегия', system: 'посольство Германии' },
  { name: 'Исландия', system: 'посольство Германии' },
  { name: 'Люксембург', system: 'посольство Германии' },
];

/** Countries where you book yourself — the bot doesn't book them, but we point to the official route. */
export const DIRECT_COUNTRIES = [
  { name: 'Франция', how: 'заявка на demarche.numerique.gouv.fr, форма «Demande de rendez-vous visas — Chypre»', url: 'https://france-visas.gouv.fr' },
  { name: 'Испания', how: 'письмо в консульский отдел посольства, тема CITA PREVIA', url: 'mailto:emb.nicosia.sc@maec.es' },
  { name: 'Италия', how: 'запись в системе Prenot@mi', url: 'https://prenotami.esteri.it' },
];

/**
 * Telegram deep link. The bot parses start=<action>-<tag> (app/deeplink.py):
 * action opens a screen right away, tag is saved as the traffic source in /admin.
 * Allowed chars: A-Z a-z 0-9 _ -, max 64. tracking.ts may append the UTM source to the tag.
 */
export type BotAction = 'wheel' | 'invite' | 'order' | 'start';
export function botLink(action: BotAction, place: string): string {
  const tag = `site_${place}`.replace(/[^\w]/g, '_').slice(0, 40);
  const payload = action === 'start' ? tag : `${action}-${tag}`;
  return `${BOT_URL}?start=${payload}`;
}
