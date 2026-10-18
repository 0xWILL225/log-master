<!-- Dashboard: recording control, warnings and one card per source. -->
<script lang="ts">
	import RecordingPanel from '$lib/components/RecordingPanel.svelte';
	import SourceCard from '$lib/components/SourceCard.svelte';
	import WarningsPanel from '$lib/components/WarningsPanel.svelte';
	import { isSelected, select } from '$lib/selection.svelte';
	import { appState } from '$lib/state.svelte';

	const snapshot = $derived(appState.snapshot);
	const active = $derived(snapshot?.active_log ?? null);

	function participant(name: string) {
		return active?.participants.find((p) => p.name === name);
	}
</script>

<svelte:head><title>log-master — Record</title></svelte:head>

{#if snapshot}
	<RecordingPanel {snapshot} disabled={!appState.connected} />
	<WarningsPanel warnings={snapshot.warnings ?? []} disabled={!appState.connected} />
	{#if snapshot.sources.length === 0}
		<p class="card preset-tonal-surface p-4">No sources configured. Add some under <a class="anchor" href="/settings">Settings</a>.</p>
	{/if}
	<section class="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3" aria-label="Sources">
		{#each snapshot.sources as source (source.name)}
			{#if active}
				<SourceCard {source} participant={participant(source.name)} selected={participant(source.name) !== undefined} locked nowNs={appState.serverNowNs} />
			{:else}
				<SourceCard
					{source}
					participant={undefined}
					selected={isSelected(source)}
					onselect={(value) => select(source.name, value)}
					locked={false}
					nowNs={appState.serverNowNs}
				/>
			{/if}
		{/each}
	</section>
{:else}
	<p class="p-4 opacity-75">Connecting to log-master…</p>
{/if}
