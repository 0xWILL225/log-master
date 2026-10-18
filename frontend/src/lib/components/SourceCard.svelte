<!-- One configured source on the dashboard. -->
<script lang="ts">
	import type { Participant, SourceSummary } from '$lib/api/client';
	import { formatAge, formatBytes } from '$lib/format';
	import Badge from './Badge.svelte';
	import DetailsList from './DetailsList.svelte';
	import DiskBar from './DiskBar.svelte';

	let {
		source,
		participant,
		selected,
		onselect,
		locked,
		nowNs
	}: {
		source: SourceSummary;
		participant: Participant | undefined;
		selected: boolean;
		onselect?: (selected: boolean) => void;
		locked: boolean;
		nowNs: number;
	} = $props();

	const status = $derived(source.status);
	const logStatus = $derived(participant?.status ?? source.log_status ?? status?.recording.state ?? null);
	const bytes = $derived(participant ? participant.bytes_written : status?.recording.log_id ? status.recording.bytes_written : null);
	const href = $derived(`/sources/${encodeURIComponent(source.name)}`);
</script>

<article class="card border-surface-200-800 bg-surface-50-950 flex flex-col border p-4 {source.online ? '' : 'opacity-80'}" data-testid="source-card-{source.name}">
	<header class="flex items-center gap-3">
		<input
			type="checkbox"
			class="checkbox size-6"
			checked={selected}
			onchange={(event) => onselect?.(event.currentTarget.checked)}
			disabled={locked}
			aria-label="Include {source.name} in the next log"
		/>
		<a {href} class="flex-1 truncate text-lg font-bold">{source.name}</a>
		<span
			class="inline-block size-3 rounded-full {source.online ? 'bg-green-600' : 'bg-surface-400-600'}"
			title={source.online ? 'online' : 'offline'}
		></span>
		<span class="text-sm">
			{#if source.online}
				online
			{:else if source.last_seen_unix_ns}
				offline, seen {formatAge(nowNs - source.last_seen_unix_ns)} ago
			{:else}
				offline
			{/if}
		</span>
	</header>

	<a {href} class="mt-3 block flex-1 space-y-3" aria-label="{source.name} details">
		<div class="flex flex-wrap items-center gap-2">
			<Badge value={status?.health.level ?? 'offline'} label={status ? `health: ${status.health.level}` : 'no status'} />
			{#if logStatus}
				<Badge value={logStatus} />
			{/if}
			{#if bytes !== null}
				<span class="text-sm">{formatBytes(bytes)} written</span>
			{/if}
		</div>
		{#if status?.health.message}
			<p class="text-sm">{status.health.message}</p>
		{/if}
		{#if participant?.reason}
			<p class="text-sm opacity-75">{participant.reason}</p>
		{/if}
		{#if source.name_mismatch && source.info}
			<p class="text-warning-800-200 text-sm">Reports its name as “{source.info.name}”</p>
		{/if}
		{#if status}
			<DiskBar disk={status.disk} low={source.low_disk} />
			<DetailsList details={status.details} />
		{:else if source.error}
			<p class="text-sm opacity-75">{source.error}</p>
		{/if}
	</a>
</article>
