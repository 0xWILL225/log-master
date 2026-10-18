import { defineConfig, devices } from '@playwright/test';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

/**
 * Smoke tests against a real log-master daemon with two logserver-fake instances
 * (started by scripts/dev_stack.py). Set PLAYWRIGHT_CHANNEL=chrome to use an installed
 * Google Chrome instead of Playwright's bundled Chromium.
 */
const port = Number(process.env.E2E_PORT ?? 18480);
const python = process.env.PYTHON ?? '../.venv/bin/python';
const channel = process.env.PLAYWRIGHT_CHANNEL;
const workdir = join(tmpdir(), `log-master-e2e-${port}`);

export default defineConfig({
	testDir: 'tests',
	workers: 1,
	fullyParallel: false,
	timeout: 60_000,
	expect: { timeout: 10_000 },
	reporter: process.env.CI ? 'line' : 'list',
	use: { baseURL: `http://127.0.0.1:${port}`, trace: 'retain-on-failure' },
	projects: [
		{ name: 'desktop', use: { ...devices['Desktop Chrome'], channel } },
		{ name: 'phone', use: { ...devices['Pixel 7'], channel } }
	],
	webServer: {
		command: `${python} ../scripts/dev_stack.py --port ${port} --fake-port ${port + 1} --dir ${workdir} --fresh --poll-interval 0.3`,
		url: `http://127.0.0.1:${port}/api/v1/state`,
		reuseExistingServer: false,
		timeout: 60_000,
		env: { PYTHONPATH: '' }
	}
});
