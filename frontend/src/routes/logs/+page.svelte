<!-- Log browser: merged logs from all reachable sources, with per-file and zip downloads. -->
<script lang="ts">
	import { onMount } from 'svelte';
	import { api, ApiError, encodePath, type LogListResponse, type MergedLog, type MergedLogPart } from '$lib/api/client';
	import Badge from '$lib/components/Badge.svelte';
	import { formatBytes, formatDuration, formatLocal, formatUtc, localDateToNs } from '$lib/format';
	import { appState } from '$lib/state.svelte';

	let from = $state('');
	let until = $state('');
	let source = $state('');
	let result = $state<LogListResponse | null>(null);
	let loading = $state(false);
	let error = $state<string | null>(null);
	let expanded = $state<Record<string, boolean>>({});

	const sourceNames = $derived(appState.snapshot?.sources.map((s) => s.name) ?? []);

	async function load() {
		loading = true;
		error = null;
		const params = new URLSearchParams();
		if (from) params.set('since', localDateToNs(from));
		if (until) params.set('until', localDateToNs(until, true));
		if (source) params.set('source', source);
		try {
			result = await api.get<LogListResponse>(`/logs${params.size ? `?${params}` : ''}`);
		} catch (e) {
			error = e instanceof ApiError ? e.message : String(e);
		} finally {
			loading = false;
		}
	}

	onMount(() => {
		void load();
	});

	function zipUrl(log: MergedLog, sourceName?: string): string {
		const base = `/api/v1/logs/${encodeURIComponent(log.log_id)}/download.zip`;
		return sourceName ? `${base}?sources=${encodeURIComponent(sourceName)}` : base;
	}

	function fileUrl(log: MergedLog, part: MergedLogPart, path: string): string {
		return `/api/v1/logs/${encodeURIComponent(log.log_id)}/sources/${encodeURIComponent(part.source)}/files/${encodePath(path)}`;
	}

	function gaps(part: MergedLogPart): { after: number; durationNs: number }[] {
		const result = [];
		for (let i = 1; i < part.segments.length; i++) {
			const previous = part.segments[i - 1].end_unix_ns;
			const next = part.segments[i].start_unix_ns;
			if (previous != null && next != null) result.push({ after: part.segments[i - 1].index, durationNs: next - previous });
		}
		return result;
	}
</script>

<svelte:head><title>log-master — Logs</title></svelte:head>

<section class="card border-surface-200-800 flex flex-wrap items-end gap-3 border p-3">
	<label class="label w-full sm:w-auto">
		<span class="label-text">From</span>
		<input type="date" class="input touch-target" bind:value={from} onchange={load} />
	</label>
	<label class="label w-full sm:w-auto">
		<span class="label-text">Until</span>
		<input type="date" class="input touch-target" bind:value={until} onchange={load} />
	</label>
	<label class="label w-full sm:w-auto">
		<span class="label-text">Source</span>
		<select class="select touch-target" bind:value={source} onchange={load}>
			<option value="">All sources</option>
			{#each sourceNames as name (name)}<option value={name}>{name}</option>{/each}
		</select>
	</label>
	<button class="btn preset-filled-primary-500 touch-target" onclick={load} disabled={loading}>{loading ? 'Loading…' : 'Refresh'}</button>
</section>

{#if error}
	<div class="card preset-filled-error-500 p-3">{error}</div>
{/if}
{#if result?.unreachable_sources?.length}
	<div class="card preset-tonal-warning border-warning-500 border p-3" data-testid="unreachable-sources">
		{result.unreachable_sources.join(', ')}
		{result.unreachable_sources.length === 1 ? 'is' : 'are'} offline — {result.unreachable_sources.length === 1 ? 'its' : 'their'} logs are not listed.
	</div>
{/if}

{#if result && result.logs.length === 0}
	<p class="p-4 opacity-75">No logs found.</p>
{/if}

<ul class="space-y-3" data-testid="log-list">
	{#each result?.logs ?? [] as log (log.log_id)}
		<li class="card border-surface-200-800 bg-surface-50-950 border" data-testid="log-row">
			<button
				class="flex w-full flex-col gap-2 p-3 text-left sm:flex-row sm:items-center"
				onclick={() => (expanded[log.log_id] = !expanded[log.log_id])}
				aria-expanded={expanded[log.log_id] ?? false}
			>
				<div class="min-w-0 flex-1">
					<div class="mono text-sm font-semibold break-all">{log.log_id}</div>
					<div class="text-sm" title={formatUtc(log.start_unix_ns)}>
						{formatLocal(log.start_unix_ns)} · {formatDuration(log.duration_ns)} · {formatBytes(log.total_bytes)}
					</div>
				</div>
				<div class="flex flex-wrap items-center gap-1">
					{#each log.parts as part (part.source)}
						<Badge value={part.state} label="{part.source}{part.segments.length > 1 ? ' ⧖' : ''}" />
					{/each}
					{#each log.missing_sources ?? [] as missing (missing)}
						<Badge value="lost" label="{missing} (missing)" />
					{/each}
					{#if log.standalone}<span class="badge preset-outlined-surface-500">standalone</span>{/if}
					{#if log.recording}<span class="badge preset-filled-error-500">recording</span>{/if}
				</div>
			</button>

			{#if expanded[log.log_id]}
				<div class="border-surface-200-800 space-y-4 border-t p-3">
					<div class="flex flex-wrap gap-2">
						{#if log.recording}
							<button class="btn preset-filled-primary-500 touch-target" disabled>Download all (.zip)</button>
							<span class="self-center text-sm opacity-75">Downloads are available once recording has stopped.</span>
						{:else}
							<a class="btn preset-filled-primary-500 touch-target" href={zipUrl(log)} download>Download all (.zip)</a>
						{/if}
					</div>
					{#each log.parts as part (part.source)}
						<section class="space-y-2">
							<div class="flex flex-wrap items-center gap-2">
								<h3 class="h5">{part.source}</h3>
								<Badge value={part.state} />
								<span class="text-sm opacity-75">{part.origin ?? 'unknown'} · {formatBytes(part.total_bytes)}</span>
								{#if part.state === 'recording'}
									<button class="btn btn-sm preset-tonal-primary" disabled>{part.source}.zip</button>
								{:else}
									<a class="btn btn-sm preset-tonal-primary touch-target" href={zipUrl(log, part.source)} download>{part.source}.zip</a>
								{/if}
							</div>
							<ol class="space-y-1 text-sm">
								{#each part.segments as segment, i (segment.index)}
									<li>
										Segment {segment.index}:
										<span title={formatUtc(segment.start_unix_ns)}>{formatLocal(segment.start_unix_ns)}</span> →
										<span title={formatUtc(segment.end_unix_ns)}>{formatLocal(segment.end_unix_ns)}</span>
										{#if !segment.ended_cleanly && part.state !== 'recording'}<span class="badge preset-tonal-warning">unclean end</span>{/if}
									</li>
									{#each gaps(part).filter((g) => g.after === segment.index && i < part.segments.length - 1) as gap (gap.after)}
										<li class="text-warning-800-200 font-semibold">⧖ gap of {formatDuration(gap.durationNs)}</li>
									{/each}
								{/each}
							</ol>
							<ul class="divide-surface-200-800 divide-y text-sm">
								{#each part.files as file (file.path)}
									<li class="flex items-center gap-2 py-1">
										{#if part.state === 'recording'}
											<span class="mono min-w-0 flex-1 break-all opacity-60">{file.path}</span>
										{:else}
											<a class="anchor mono min-w-0 flex-1 break-all" href={fileUrl(log, part, file.path)} download>{file.path}</a>
										{/if}
										<span class="tabular-nums">{formatBytes(file.size)}</span>
									</li>
								{/each}
							</ul>
						</section>
					{/each}
				</div>
			{/if}
		</li>
	{/each}
</ul>
