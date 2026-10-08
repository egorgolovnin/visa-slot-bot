// Renders og.png (1200x630) and app icons from HTML with the site's own fonts. Run: node scripts/make-images.mjs
import { chromium } from 'playwright';
import { readFileSync } from 'node:fs';

const font = (f) => `data:font/woff2;base64,${readFileSync(`public/fonts/${f}`).toString('base64')}`;
const faces = `
@font-face{font-family:U;font-weight:700;src:url(${font('unbounded-cyrillic-700-normal.woff2')})}
@font-face{font-family:U;font-weight:700;src:url(${font('unbounded-latin-700-normal.woff2')});unicode-range:U+0000-00FF,U+20AC}
@font-face{font-family:G;font-weight:400;src:url(${font('golos-text-cyrillic-400-normal.woff2')})}
@font-face{font-family:G;font-weight:400;src:url(${font('golos-text-latin-400-normal.woff2')});unicode-range:U+0000-00FF,U+20AC}`;
const logo = readFileSync('public/favicon.svg', 'utf8');

const og = `<html><head><style>${faces}
body{margin:0;width:1200px;height:630px;background:#0e1a2b;color:#fff;font-family:G;display:flex;overflow:hidden}
.l{padding:72px 0 72px 80px;width:720px;display:flex;flex-direction:column;justify-content:space-between}
h1{font:700 60px/1.12 U;margin:0;letter-spacing:-0.01em}
p{font-size:30px;color:#c9d3e0;margin:0}
.b{display:flex;align-items:center;gap:18px;font:700 30px U}.b svg{width:64px;height:64px}
.w{position:absolute;right:-150px;top:65px;width:500px;height:500px;border-radius:50%;
background:conic-gradient(from -70deg,#1f5fd1 0 122deg,#17803d 0 230deg,#1d2d44 0 302deg,#3c7be0 0 345deg,#0f5c2b 0 356deg,#f2b705 0 360deg);border:10px solid #0a1320}
.w:after{content:'';position:absolute;inset:200px;border-radius:50%;background:#f3f6fa}
</style></head><body><div class="l"><div class="b">${logo}Easy Visa</div>
<h1>Запись на шенгенскую визу с Кипра</h1><p>Ловим слот за вас. Оплата после брони.</p></div><div class="w"></div></body></html>`;

const icon = (size) => `<html><body style="margin:0;width:${size}px;height:${size}px">${logo.replace('<svg ', `<svg width="${size}" height="${size}" `)}</body></html>`;

const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
const page = await browser.newPage();
await page.setViewportSize({ width: 1200, height: 630 });
await page.setContent(og); await page.evaluate(() => document.fonts.ready);
await page.screenshot({ path: 'public/og.png' });
for (const s of [180, 512]) {
  await page.setViewportSize({ width: s, height: s });
  await page.setContent(icon(s));
  await page.screenshot({ path: `public/icon-${s}.png`, omitBackground: true });
}
await browser.close();
console.log('images ok');
