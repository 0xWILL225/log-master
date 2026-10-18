<!-- log-master settings: the source list and the non-source settings of config.yaml. -->
<script lang="ts">
	import { onMount } from 'svelte';
	import { api, ApiError, type ConfigDocument, type SourceSummary } from '$lib/api/client';
	import SchemaForm from '$lib/components/SchemaForm.svelte';
	import type { JsonSchema, Values } from '$lib/schema';
	import { appState } from '$lib/state.svelte';
	import { toast } from '$lib/toast.svelte';

	let settings = $state<ConfigDocument | null>(null);
	let edits = $state<Record<string, { url: string; enabled_by_default: boolean }>>({});
	let newSource = $state({ name: '', url: 'http://', enabled_by_default: true });
	let busy = $state(false);

	const sources = $derived(appState.snapshot?.sources ?? []);
	const participants = $derived(new Set(appState.snapshot?.active_log?.participants.map((p) => p.name) ?? []));

	$effect(() => {
		for (const source of sources) {
			if (!(source.name in edits)) edits[source.name] = { url: source.url, enabled_by_default: source.enabled_by_default };
		}
	});

	onMount(async () => {
		try {
			settings = await api.get<ConfigDocument>('/settings');
		} catch (e) {
			toast(e instanceof ApiError ? e.message : String(e), 'error');
		}
	});

	async function run(action: () => Promise<void>, success: string) {
		busy = true;
		try {
			await action();
			toast(success, 'success');
			await appState.refresh();
		} catch (e) {
			toast(e instanceof ApiError ? e.message : String(e), 'error', 6000);
		} finally {
			busy = false;
		}
	}

	function dirty(source: SourceSummary): boolean {
		const edit = edits[source.name];
		return edit !== undefined && (edit.url !== source.url || edit.enabled_by_default !== source.enabled_by_default);
	}

	function save(source: SourceSummary) {
		return run(async () => {
			await api.put(`/sources/${encodeURIComponent(source.name)}`, edits[source.name]);
		}, `${source.name} saved`);
	}

	function remove(source: SourceSummary) {
		if (!window.confirm(`Remove source ${source.name}? Its logs stay on its host.`)) return;
		return run(async () => {
			await api.delete(`/sources/${encodeURIComponent(source.name)}`);
			delete edits[source.name];
		}, `${source.name} removed`);
	}

	function add(event: SubmitEvent) {
		event.preventDefault();
		return run(async () => {
			await api.post('/sources', newSource);
			newSource = { name: '', url: 'http://', enabled_by_default: true };
		}, `${newSource.name} added`);
	}

	async function saveSettings(patch: Values) {
		settings = await api.patch<ConfigDocument>('/settings', patch);
		toast('Settings saved', 'success');
		await appState.refresh();
	}
</script>

<svelte:head><title>log-master — Settings</title></svelte:head>

<section class="card border-surface-200-800 bg-surface-50-950 space-y-4 border p-4" aria-label="Sources">
	<h2 class="h4">Sources</h2>
	<ul class="space-y-3">
		{#each sources as source (source.name)}
			{@const edit = edits[source.name]}
			{#if edit}
				<li class="border-surface-200-800 flex flex-col gap-2 rounded border p-3 md:flex-row md:items-end" data-testid="source-row-{source.name}">
					<div class="font-bold md:w-40">{source.name}</div>
					<label class="label flex-1">
						<span class="label-text">URL</span>
						<input class="input touch-target mono" bind:value={edit.url} />
					</label>
					<label class="flex min-h-11 items-center gap-2">
						<input type="checkbox" class="checkbox size-6" bind:checked={edit.enabled_by_default} />
						Record by default
					</label>
					<div class="flex gap-2">
						<button class="btn preset-filled-primary-500 touch-target" disabled={busy || !dirty(source)} onclick={() => save(source)}>Save</button>
						<button
							class="btn preset-tonal-error touch-target"
							disabled={busy || participants.has(source.name)}
							title={participants.has(source.name) ? 'Takes part in the active log' : undefined}
							onclick={() => remove(source)}>Remove</button
						>
					</div>
				</li>
			{/if}
		{/each}
	</ul>
	<form class="border-surface-200-800 flex flex-col gap-2 rounded border border-dashed p-3 md:flex-row md:items-end" onsubmit={add}>
		<label class="label md:w-40">
			<span class="label-text">Name</span>
			<input class="input touch-target" bind:value={newSource.name} pattern="[a-z0-9][a-z0-9_\-]{'{'}0,31{'}'}" required placeholder="rover" />
		</label>
		<label class="label flex-1">
			<span class="label-text">URL</span>
			<input class="input touch-target mono" bind:value={newSource.url} required placeholder="http://host:8001" />
		</label>
		<label class="flex min-h-11 items-center gap-2">
			<input type="checkbox" class="checkbox size-6" bind:checked={newSource.enabled_by_default} />
			Record by default
		</label>
		<button class="btn touch-target bg-green-700 text-white" disabled={busy}>Add source</button>
	</form>
</section>

{#if settings}
	<section class="card border-surface-200-800 bg-surface-50-950 space-y-3 border p-4" aria-label="Settings">
		<h2 class="h4">Settings</h2>
		<SchemaForm schema={settings.schema as JsonSchema} values={settings.values as Values} mode="patch" onsubmit={saveSettings} />
	</section>
{/if}
