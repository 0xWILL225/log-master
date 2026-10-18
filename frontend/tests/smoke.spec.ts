import { expect, test, type APIRequestContext } from '@playwright/test';

const port = Number(process.env.E2E_PORT ?? 18480);
const fake1 = `http://127.0.0.1:${port + 1}`;

async function state(request: APIRequestContext) {
	return (await request.get('/api/v1/state')).json();
}

/** Bring the stack back to idle: no active log, no pending stops, fakes idle and healthy. */
async function reset(request: APIRequestContext) {
	if ((await state(request)).state === 'recording') await request.post('/api/v1/recording/stop');
	for (const port_ of [port + 1, port + 2]) {
		await request.post(`http://127.0.0.1:${port_}/v1/log/stop`, { data: {} });
		await request.post(`http://127.0.0.1:${port_}/v1/actions/set-health`, { data: { level: 'ok' } });
	}
	await expect
		.poll(async () => {
			const s = await state(request);
			return s.sources.every((src: { status: { recording: { state: string } } | null }) => src.status?.recording.state === 'idle');
		})
		.toBe(true);
	for (const source of Object.keys((await state(request)).stop_pending)) {
		await request.post(`/api/v1/recording/dismiss-pending/${source}`);
	}
}

test.beforeEach(async ({ request }) => {
	await reset(request);
});

test('start and stop a log', async ({ page }) => {
	await page.goto('/');
	await expect(page.getByTestId('state-label')).toHaveText('IDLE');
	await expect(page.getByTestId('source-card-fake1').getByRole('checkbox')).toBeChecked();
	await page.getByTestId('duration-input').fill('10m');
	await page.getByTestId('start-button').click();
	await expect(page.getByTestId('state-label')).toContainText(/ARMING|RECORDING/);
	await expect(page.getByTestId('state-label')).toContainText('RECORDING');
	await expect(page.getByTestId('recording-panel')).toContainText('remaining');

	const stop = page.getByTestId('stop-button');
	await stop.click();
	await expect(stop).toHaveText('Press again to stop');
	await stop.click();
	await expect(page.getByTestId('state-label')).toHaveText('IDLE');
});

test('stop needs a second press within 3 s', async ({ page }) => {
	await page.goto('/');
	await page.getByTestId('start-button').click();
	const stop = page.getByTestId('stop-button');
	await stop.click();
	await expect(stop).toHaveText('Press again to stop');
	await expect(stop).toHaveText('Stop', { timeout: 5000 });
	await expect(page.getByTestId('state-label')).not.toHaveText('IDLE');
});

test('conflict dialog takes over a busy source', async ({ page, request }) => {
	const standalone = await (await request.post(`${fake1}/v1/log/start`, { data: {} })).json();
	await expect
		.poll(async () => (await state(request)).sources.find((s: { name: string }) => s.name === 'fake1').status.recording.log_id)
		.toBe(standalone.log_id);

	await page.goto('/');
	await page.getByTestId('start-button').click();
	const conflicts = page.getByTestId('conflict-list');
	await expect(conflicts).toContainText('fake1');
	await expect(conflicts).toContainText(standalone.log_id);
	await page.getByTestId('conflict-start').click();
	await expect(page.getByTestId('state-label')).toContainText('RECORDING');
	await expect(page.getByTestId('source-card-fake1')).toContainText('recording');
});

test('generated action form runs an action', async ({ page }) => {
	await page.goto('/sources/fake1');
	const form = page.getByTestId('action-set-health');
	await form.getByLabel('Level').selectOption('warn');
	await form.getByLabel('Message').fill('e2e warning');
	await form.getByRole('button', { name: 'Run' }).click();
	await expect(page.getByTestId('toast').filter({ hasText: 'health set to warn' })).toBeVisible();
	await expect(page.getByText('e2e warning').first()).toBeVisible();

	const crash = page.getByTestId('action-crash');
	await crash.getByRole('button', { name: 'Run' }).click();
	await expect(page.getByRole('dialog')).toContainText('kills the fake server');
	await page.getByRole('dialog').getByRole('button', { name: 'Cancel' }).click();
	await expect(page.getByRole('dialog')).toBeHidden();
});

test('logs page lists a recorded log with downloads', async ({ page, request }) => {
	const started = await (await request.post('/api/v1/recording/start', { data: { request_id: `e2e-${Date.now()}` } })).json();
	const logId = started.active_log.log_id;
	await expect.poll(async () => (await state(request)).active_log?.actual_start_unix_ns ?? null).not.toBeNull();
	await request.post('/api/v1/recording/stop');
	await expect.poll(async () => (await state(request)).sources.every((s: { status: { recording: { state: string } } }) => s.status.recording.state === 'idle')).toBe(true);

	await page.goto('/logs');
	const row = page.getByTestId('log-row').filter({ hasText: logId });
	await expect(row).toBeVisible();
	await row.getByRole('button').first().click();
	await expect(row.getByRole('link', { name: 'Download all (.zip)' })).toHaveAttribute('href', `/api/v1/logs/${logId}/download.zip`);
	await expect(row.getByRole('link', { name: 'manifest.json' }).first()).toBeVisible();
});
