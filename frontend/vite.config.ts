import { sveltekit } from '@sveltejs/kit/vite';
import tailwindcss from '@tailwindcss/vite';
import { defineConfig } from 'vite';

// `npm run dev` proxies the API to a running daemon (default http://localhost:8080).
const target = process.env.LOG_MASTER_URL ?? 'http://localhost:8080';

export default defineConfig({
	plugins: [tailwindcss(), sveltekit()],
	server: { proxy: { '/api': target } }
});
