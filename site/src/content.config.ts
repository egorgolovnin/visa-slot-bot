import { defineCollection, z } from 'astro:content';
import { glob } from 'astro/loaders';

const gid = defineCollection({
  loader: glob({ pattern: '*.md', base: './src/content/gid' }),
  schema: z.object({
    title: z.string(),          // H1
    seoTitle: z.string(),       // <title>
    description: z.string(),    // meta description
    published: z.string(),
    updated: z.string(),
    order: z.number(),
    cta: z.enum(['order', 'wheel', 'invite']).default('order'),
    sources: z.array(z.object({ name: z.string(), url: z.string().url() })),
    faq: z.array(z.object({ q: z.string(), a: z.string() })).optional(),
  }),
});

const strany = defineCollection({
  loader: glob({ pattern: '*.md', base: './src/content/strany' }),
  schema: z.object({
    name: z.string(),           // H1 / display name, e.g. "Германия"
    seoTitle: z.string(),       // <title>
    description: z.string(),    // meta description
    order: z.number(),
    updated: z.string(),
    system: z.string(),         // "VFS Global" | "посольство Германии"
    consularFee: z.string(),    // "90 €"
    serviceFee: z.string(),     // "25,27 €" | "нет" | "по прайсу центра"
    represents: z.array(z.string()).optional(),  // for Germany: countries it represents in Nicosia
    official: z.object({ name: z.string(), url: z.string().url() }),
    sources: z.array(z.object({ name: z.string(), url: z.string().url() })),
    faq: z.array(z.object({ q: z.string(), a: z.string() })).optional(),
  }),
});

export const collections = { gid, strany };
