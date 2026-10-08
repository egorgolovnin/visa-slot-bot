/**
 * Client-side tracking. Small on purpose and safe without consent:
 *  1. UTM: utm_source from the landing URL is kept for the session and appended to the bot's start tag,
 *     so /admin → Источники shows e.g. "site_home_tgads". Referral links (start=ref…) are never touched.
 *  2. GA4: loaded only after the visitor accepts analytics cookies (GDPR, the audience lives in the EU).
 *  3. Event "tg_click" on every bot button: cta_action (wheel/invite/order/start) + cta_place.
 *     A click is not a sign-up — sign-ups are counted in the bot (/admin).
 */

declare global {
  interface Window { dataLayer: unknown[]; gtag?: (...args: unknown[]) => void }
}

const CONSENT_KEY = 'ev_consent';
const UTM_KEY = 'ev_utm';

function store(kind: 'local' | 'session') {
  try { return kind === 'local' ? window.localStorage : window.sessionStorage; } catch { return null; }
}

function readUtm(): string {
  const params = new URLSearchParams(location.search);
  const src = params.get('utm_source');
  const s = store('session');
  if (src) {
    const clean = src.toLowerCase().replace(/[^a-z0-9]/g, '').slice(0, 12);
    if (clean) { try { s?.setItem(UTM_KEY, clean); } catch { /* storage blocked */ } }
    return clean;
  }
  try { return s?.getItem(UTM_KEY) || ''; } catch { return ''; }
}

function tagBotLinks(utm: string) {
  if (!utm) return;
  document.querySelectorAll<HTMLAnchorElement>('a[data-bot]').forEach((a) => {
    const url = new URL(a.href);
    const start = url.searchParams.get('start') || '';
    if (!start || start.startsWith('ref')) return;
    const next = `${start}_${utm}`.slice(0, 64);
    url.searchParams.set('start', next);
    a.href = url.toString();
  });
}

function loadGa(id: string) {
  if (window.gtag) return;
  window.dataLayer = window.dataLayer || [];
  window.gtag = function gtag() { window.dataLayer.push(arguments); };
  window.gtag('js', new Date());
  window.gtag('config', id, { anonymize_ip: true });
  const s = document.createElement('script');
  s.async = true;
  s.src = `https://www.googletagmanager.com/gtag/js?id=${encodeURIComponent(id)}`;
  document.head.appendChild(s);
}

function initConsent(id: string) {
  const box = document.querySelector<HTMLElement>('[data-consent]');
  const ls = store('local');
  let choice: string | null = null;
  try { choice = ls?.getItem(CONSENT_KEY) ?? null; } catch { choice = null; }
  if (choice === 'yes') loadGa(id);
  else if (choice !== 'no' && box) box.hidden = false;

  const set = (v: 'yes' | 'no') => {
    try { ls?.setItem(CONSENT_KEY, v); } catch { /* ignore */ }
    if (box) box.hidden = true;
    if (v === 'yes') loadGa(id);
  };
  document.querySelector('[data-consent-yes]')?.addEventListener('click', () => set('yes'));
  document.querySelector('[data-consent-no]')?.addEventListener('click', () => set('no'));
  document.querySelector('[data-consent-reset]')?.addEventListener('click', () => {
    try { ls?.removeItem(CONSENT_KEY); } catch { /* ignore */ }
    if (box) box.hidden = false;
  });
}

function initClicks() {
  document.addEventListener('click', (e) => {
    const a = (e.target as Element | null)?.closest?.('a[data-bot]') as HTMLAnchorElement | null;
    if (!a) return;
    const detail = { cta_action: a.dataset.bot, cta_place: a.dataset.place, page_path: location.pathname };
    window.gtag?.('event', 'tg_click', { ...detail, transport_type: 'beacon' });
  });
}

export function initTracking() {
  tagBotLinks(readUtm());
  initClicks();
  const id = document.querySelector<HTMLMetaElement>('meta[name="ga-id"]')?.content;
  if (id) initConsent(id);
}
