<!-- Source detail: info, full status, actions and config, all rendered generically. -->
<script lang="ts">
	import { page } from '$app/state';
	import { api, ApiError, type ActionList, type ActionResult, type ActionSpec, type ConfigDocument } from '$lib/api/client';
	import Badge from '$lib/components/Badge.svelte';
	import ConfirmDialog from '$lib/components/ConfirmDialog.svelte';
	import DetailsList from '$lib/components/DetailsList.svelte';
	import DiskBar from '$lib/components/DiskBar.svelte';
	import SchemaForm from '$lib/components/SchemaForm.svelte';
	import { formatAge, formatBytes, formatLocal } from '$lib/format';
	import type { JsonSchema, Values } from '$lib/schema';
	import { appState } from '$lib/state.svelte';
	import { toast } from '$lib/toast.svelte';

	const name = $derived(page.params.name ?? '');
	const base = $derived(`/sources/${encodeURIComponent(name)}`);
	const source = $derived(appState.snapshot?.sources.find((s) => s.name === name));
	const status = $derived(source?.status ?? null);
	const recording = $derived(status !== null && status.recording.state !== 'idle');

	let actions = $state<ActionSpec[] | null>(null);
	let config = $state<ConfigDocument | null>(null);
	let loadError = $state<string | null>(null);
	let confirmOpen = $state(false);
	let confirmMessage = $state('');
	let confirmResolve: ((ok: boolean) => void) | null = null;

	async function load() {
		loadError = null;
		try {
			const [actionList, configDocument] = await Promise.all([
				api.get<ActionList>(`${base}/actions`),
				api.get<ConfigDocument>(`${base}/config`)
			]);
			actions = actionList.actions;
			config = configDocument;
		} catch (e) {
			loadError = e instanceof ApiError ? e.message : String(e);
		}
	}

	$effect(() => {
		void name;
		actions = null;
		config = null;
		void load();
	});

	function confirm(message: string): Promise<boolean> {
		confirmMessage = message;
		confirmOpen = true;
		return new Promise((resolve) => (confirmResolve = resolve));
	}

	$effect(() => {
		if (!confirmOpen && confirmResolve) {
			const resolve = confirmResolve;
			confirmResolve = null;
			resolve(false);
		}
	});

	async function runAction(action: ActionSpec, params: Values) {
		if (action.confirm && !(await confirm(action.confirm))) return;
		const result = await api.post<ActionResult>(`${base}/actions/${encodeURIComponent(action.name)}`, params);
		toast(result.message, 'success');
		await Promise.all([load(), appState.refresh()]);
	}

	async function saveConfig(patch: Values) {
		config = await api.patch<ConfigDocument>(`${base}/config`, patch);
		toast(`Config of ${name} saved`, 'success');
		await appState.refresh();
	}
</script>

<svelte:head><title>log-master — {name}</title></svelte:head>

<a href="/" class="anchor">‹ Back to dashboard</a>

{#if !source}
	<p class="card preset-tonal-warning p-4">No source named “{name}” is configured.</p>
{:else}
	<section class="card border-surface-200-800 bg-surface-50-950 space-y-3 border p-4">
		<div class="flex flex-wrap items-center gap-3">
			<h1 class="h3">{source.name}</h1>
			<Badge value={source.online ? (status?.health.level ?? 'ok') : 'offline'} label={source.online ? `health: ${status?.health.level}` : 'offline'} />
			{#if status}<Badge value={source.log_status ?? status.recording.state} />{/if}
		</div>
		{#if status?.health.message}<p>{status.health.message}</p>{/if}
		{#if !source.online}
			<p class="text-sm">
				{source.last_seen_unix_ns ? `Last seen ${formatAge(appState.serverNowNs - source.last_seen_unix_ns)} ago.` : 'Never seen.'}
				{source.error ?? ''}
			</p>
		{/if}
		<dl class="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
			<dt class="opacity-75">URL</dt>
			<dd class="mono break-all">{source.url}</dd>
			{#if source.info}
				<dt class="opacity-75">Reported name</dt>
				<dd>{source.info.name}{source.name_mismatch ? ' (differs from configured name)' : ''}</dd>
				<dt class="opacity-75">Type</dt>
				<dd>{source.info.source_type}</dd>
				<dt class="opacity-75">Version</dt>
				<dd>{source.info.server_version} (protocol {source.info.protocol_version})</dd>
				<dt class="opacity-75">Host</dt>
				<dd>{source.info.hostname}</dd>
				<dt class="opacity-75">Log directory</dt>
				<dd class="mono break-all">{source.info.log_directory}</dd>
			{/if}
			{#if status && status.recording.log_id}
				<dt class="opacity-75">Log</dt>
				<dd class="mono break-all">{status.recording.log_id} ({status.recording.origin}, segment {status.recording.segment_index})</dd>
				<dt class="opacity-75">Started</dt>
				<dd>{formatLocal(status.recording.actual_start_unix_ns)}</dd>
				<dt class="opacity-75">Written</dt>
				<dd>{formatBytes(status.recording.bytes_written)}</dd>
			{/if}
		</dl>
		{#if status}
			<DiskBar disk={status.disk} low={source.low_disk} />
			<DetailsList details={status.details} />
		{/if}
	</section>

	{#if loadError}
		<div class="card preset-tonal-error flex items-center gap-3 p-3">
			<span class="flex-1">{loadError}</span>
			<button class="btn preset-tonal-surface touch-target" onclick={load}>Retry</button>
		</div>
	{/if}

	{#if actions}
		<section class="space-y-3" aria-label="Actions">
			<h2 class="h4">Actions</h2>
			{#if actions.length === 0}<p class="opacity-75">This source has no actions.</p>{/if}
			<div class="grid grid-cols-1 gap-4 md:grid-cols-2">
				{#each actions as action (action.name)}
					<article class="card border-surface-200-800 bg-surface-50-950 space-y-2 border p-4" data-testid="action-{action.name}">
						<h3 class="h5">{action.label}</h3>
						{#if action.description}<p class="text-sm opacity-75">{action.description}</p>{/if}
						{#if !action.available}
							<p class="text-warning-800-200 text-sm font-semibold">Unavailable: {action.unavailable_reason ?? 'not now'}</p>
						{/if}
						<SchemaForm
							schema={(action.params_schema ?? { type: 'object', properties: {} }) as JsonSchema}
							mode="full"
							submitLabel="Run"
							disabled={!action.available || !source.online}
							onsubmit={(params) => runAction(action, params)}
						/>
					</article>
				{/each}
			</div>
		</section>
	{/if}

	{#if config}
		<section class="card border-surface-200-800 bg-surface-50-950 space-y-3 border p-4" aria-label="Configuration">
			<h2 class="h4">Configuration</h2>
			{#if recording}
				<p class="preset-tonal-warning rounded p-2 text-sm">The configuration cannot be changed while {name} is recording.</p>
			{/if}
			<SchemaForm
				schema={config.schema as JsonSchema}
				values={config.values as Values}
				mode="patch"
				disabled={recording || !source.online}
				onsubmit={saveConfig}
			/>
		</section>
	{/if}
{/if}

<ConfirmDialog
	bind:open={confirmOpen}
	title="Confirm action"
	message={confirmMessage}
	confirmLabel="Run"
	onconfirm={() => {
		const resolve = confirmResolve;
		confirmResolve = null;
		resolve?.(true);
	}}
/>
