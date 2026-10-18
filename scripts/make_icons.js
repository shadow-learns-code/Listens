#!/usr/bin/env node
/**
 * Renders the app icons from the Shadow Listens crescent mark (frontend/public/favicon.svg shapes).
 *
 *   node scripts/make_icons.js
 *
 * Needs Playwright with Chromium (npm i playwright && npx playwright install chromium, or the copy under
 * ~/.claude/skills/playwright-skill). Writes:
 *   frontend/public/icons/            PWA icons (any + maskable), iPhone home-screen icon, small favicon
 * The Android launcher icon is NOT rendered here: it is vector drawables (android/app/src/main/res/drawable/ic_launcher_*.xml).
 *   frontend/public/icons/icon.svg is the vector source for the web app; the PNGs are fallbacks for places that need a bitmap
 *   (iPhone home screen, older browsers).
 */
const fs = require("fs");
const path = require("path");

let chromium;
try {
  ({ chromium } = require("playwright"));
} catch {
  ({ chromium } = require(path.join(process.env.USERPROFILE || process.env.HOME, ".claude/skills/playwright-skill/node_modules/playwright")));
}

const ROOT = path.resolve(__dirname, "..");
const PINK = "#f0587a";
const INK = "#0c0d11";

// The mark, in the 32x32 box of favicon.svg. The crescent's visual centre is about (14.5, 16).
const mark = (fill, bars) => `
  <path d="M22 4.5A12.5 12.5 0 1 0 27.5 22 10 10 0 0 1 22 4.5z" fill="${fill}"/>
  <g fill="${bars}"><rect x="11" y="13" width="2" height="6" rx="1"/><rect x="15" y="10" width="2" height="12" rx="1"/><rect x="19" y="14" width="2" height="4" rx="1"/></g>`;

/** An SVG of `size` px with the mark scaled so its ~25 unit extent fills `fraction` of the canvas. */
function svg(size, { fraction, background, radius = 0, fill = PINK, bars = INK }) {
  const scale = (size * fraction) / 25;
  const tx = size / 2 - 14.5 * scale;
  const ty = size / 2 - 16 * scale;
  const bg = background
    ? `<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#1c1519"/><stop offset="1" stop-color="${INK}"/></linearGradient></defs>
       <rect width="${size}" height="${size}" rx="${radius}" fill="url(#g)"/>`
    : "";
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${size}" height="${size}" viewBox="0 0 ${size} ${size}">${bg}<g transform="translate(${tx} ${ty}) scale(${scale})">${mark(fill, bars)}</g></svg>`;
}

const JOBS = [
  // PWA "any" icons: rounded dark tile, transparent corners
  { out: "frontend/public/icons/icon-192.png", size: 192, opts: { fraction: 0.6, background: true, radius: 192 * 0.22 }, transparent: true },
  { out: "frontend/public/icons/icon-512.png", size: 512, opts: { fraction: 0.6, background: true, radius: 512 * 0.22 }, transparent: true },
  // maskable: full bleed, the mark stays inside the central safe zone (80% circle)
  { out: "frontend/public/icons/icon-maskable-512.png", size: 512, opts: { fraction: 0.46, background: true } },
  // iPhone home screen: opaque square (iOS rounds it itself)
  { out: "frontend/public/icons/apple-touch-icon.png", size: 180, opts: { fraction: 0.56, background: true } },
  { out: "frontend/public/icons/favicon-32.png", size: 32, opts: { fraction: 0.86, background: false }, transparent: true },
];

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ deviceScaleFactor: 1 });
  for (const job of JOBS) {
    await page.setViewportSize({ width: job.size, height: job.size });
    await page.setContent(`<html><body style="margin:0;background:transparent">${svg(job.size, job.opts)}</body></html>`);
    const file = path.join(ROOT, job.out);
    fs.mkdirSync(path.dirname(file), { recursive: true });
    await page.screenshot({ path: file, omitBackground: !!job.transparent, clip: { x: 0, y: 0, width: job.size, height: job.size } });
    console.log("wrote", job.out);
  }
  await browser.close();
})();
