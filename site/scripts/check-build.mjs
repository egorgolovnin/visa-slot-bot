// Post-build SEO/QA check over dist/. Fails the build on errors, prints warnings.
// Checks: internal links resolve, one <h1>, unique <title>/description, lengths, canonical, JSON-LD parses,
// images have alt or are aria-hidden, bot links use the deep-link format the bot understands.
import { readFileSync, readdirSync, statSync, existsSync } from 'node:fs';
import { join } from 'node:path';

const DIST = 'dist';
const files = [];
(function walk(d) {
  for (const f of readdirSync(d)) {
    const p = join(d, f);
    if (statSync(p).isDirectory()) walk(p); else if (p.endsWith('.html')) files.push(p);
  }
})(DIST);

const errors = [], warns = [];
const titles = new Map(), descs = new Map();
const START_RE = /^(ref\d+|(wheel|invite|order)-[\w]+|[\w]+)$/;

const exists = (href) => {
  const clean = href.split('#')[0].split('?')[0];
  if (clean === '' ) return true;
  const p = join(DIST, clean);
  return existsSync(p) && (statSync(p).isFile() || existsSync(join(p, 'index.html')));
};
const redirects = readFileSync('public/_redirects', 'utf8').split('\n').filter((l) => l.trim() && !l.startsWith('#')).map((l) => l.split(/\s+/)[0]);

for (const f of files) {
  const html = readFileSync(f, 'utf8');
  const page = '/' + f.slice(DIST.length + 1).replace(/index\.html$/, '');
  const is404 = page.startsWith('/404');
  const h1 = (html.match(/<h1[\s>]/g) || []).length;
  if (h1 !== 1) errors.push(`${page}: ${h1} <h1>`);
  const title = (html.match(/<title>([^<]*)<\/title>/) || [])[1];
  const desc = (html.match(/<meta name="description" content="([^"]*)"/) || [])[1];
  if (!title) errors.push(`${page}: no <title>`);
  if (!desc) errors.push(`${page}: no meta description`);
  if (title && (title.length < 25 || title.length > 65)) warns.push(`${page}: title ${title.length} chars — "${title}"`);
  if (desc && (desc.length < 90 || desc.length > 170)) warns.push(`${page}: description ${desc.length} chars`);
  if (!is404) {
    if (titles.has(title)) errors.push(`${page}: duplicate title with ${titles.get(title)}`); titles.set(title, page);
    if (descs.has(desc)) errors.push(`${page}: duplicate description with ${descs.get(desc)}`); descs.set(desc, page);
    if (!/<link rel="canonical" href="[^"]+"/.test(html)) errors.push(`${page}: no canonical`);
  }
  for (const m of html.matchAll(/<script type="application\/ld\+json">([\s\S]*?)<\/script>/g)) {
    try { JSON.parse(m[1]); } catch { errors.push(`${page}: invalid JSON-LD`); }
  }
  for (const m of html.matchAll(/href="([^"]+)"/g)) {
    const href = m[1].replace(/&amp;/g, '&');
    if (href.startsWith('/') && !href.startsWith('//')) {
      if (!exists(href) && !redirects.includes(href.replace(/\/$/, ''))) errors.push(`${page}: broken link ${href}`);
      else if (!href.endsWith('/') && !/\.\w+$/.test(href.split(/[?#]/)[0])) warns.push(`${page}: link without trailing slash ${href}`);
    }
    if (href.startsWith('https://t.me/')) {
      const start = new URL(href).searchParams.get('start');
      if (start !== null && (!START_RE.test(start) || start.length > 64)) errors.push(`${page}: bad start payload ${start}`);
    }
  }
  for (const m of html.matchAll(/<img\b[^>]*>/g)) if (!/\balt=/.test(m[0])) errors.push(`${page}: <img> without alt`);
}

console.log(`checked ${files.length} pages`);
warns.forEach((w) => console.log('WARN ', w));
errors.forEach((e) => console.log('ERROR', e));
if (errors.length) process.exit(1);
