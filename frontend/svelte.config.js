import adapter from '@sveltejs/adapter-static';
import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';

/** Build output goes straight into the Python package so it ships inside the log-master wheel. */
const outDir = '../packages/log_master/src/log_master/static';

/** @type {import('@sveltejs/kit').Config} */
const config = {
	preprocess: vitePreprocess(),
	kit: {
		adapter: adapter({ pages: outDir, assets: outDir, fallback: 'index.html', precompress: false, strict: true }),
		version: { pollInterval: 0 }
	}
};

export default config;
