/** True when the build has a real domain. Previews without SITE_URL get noindex everywhere. */
export const INDEXABLE = Boolean(process.env.SITE_URL);
export const GA_ID = process.env.PUBLIC_GA_ID || '';
export const GSC_TOKEN = process.env.PUBLIC_GSC_VERIFICATION || '';
