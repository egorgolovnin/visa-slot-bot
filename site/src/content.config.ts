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

export const collections = { gid };
