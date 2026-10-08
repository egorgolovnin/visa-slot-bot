import { defineConfig } from 'astro/config';
import sitemap from '@astrojs/sitemap';

// Canonical origin. Set SITE_URL in Cloudflare Pages (Settings → Environment variables) once the domain is bought.
// Without it the build is marked noindex (see src/lib/env.ts) so a preview can never get indexed by mistake.
const SITE = process.env.SITE_URL || 'https://easyvisa.example';

// Wrap markdown tables so they scroll inside their own box on phones instead of the page.
function wrapTables() {
  const walk = (node) => {
    if (!node.children) return;
    node.children = node.children.map((c) => {
      if (c.type === 'element' && c.tagName === 'table') {
        return { type: 'element', tagName: 'div', properties: { className: ['table-scroll'] }, children: [c] };
      }
      walk(c);
      return c;
    });
  };
  return (tree) => walk(tree);
}

export default defineConfig({
  markdown: { rehypePlugins: [wrapTables] },
  site: SITE,
  trailingSlash: 'always',
  build: { format: 'directory', inlineStylesheets: 'always' },
  compressHTML: true,
  integrations: [
    sitemap({
      // lastmod is left out on purpose: a build date on every URL is a false signal for Google.
      filter: (page) => !/\/404\/?$/.test(page),
    }),
  ],
});
