<!-- State header with the large Start/Stop control, duration input and the conflict dialog. -->
<script lang="ts">
	import {
		api,
		ApiError,
		newRequestId,
		type Conflict,
		type StartRecordingRequest,
		type StartRecordingResponse,
		type StateSnapshot,
		type StopRecordingResponse
	} from '$lib/api/client';
	import { formatDuration, formatLocal, formatUtc, parseDuration } from '$lib/format';
	import { isSelected } from '$lib/selection.svelte';
	import { appState } from '$lib/state.svelte';
	import { toast } from '$lib/toast.svelte';
	import Dialog from './Dialog.svelte';

	let { snapshot, disabled }: { snapshot: StateSnapshot; disabled: boolean } = $props();

	const STOP_CONFIRM_MS = 3000;

	let durationText = $state('');
	let busy = $state(false);
	let stopArmed = $state(false);
	let stopTimer: ReturnType<typeof setTimeout> | undefined;
	let conflicts = $state<Conflict[]>([]);
	let takeover = $state<Record<string, boolean>>({});
	let conflictOpen = $state(false);
	let pendingRequest: StartRecordingRequest | null = null;

	const active = $derived(snapshot.active_log);
	const now = $derived(appState.serverNowNs);
	const arming = $derived(active !== null && active !== undefined && active.actual_start_unix_ns === null && now < active.scheduled_start_unix_ns);
	const durationSeconds = $derived(parseDuration(durationText));
	const durationInvalid = $derived(durationSeconds !== null && (Number.isNaN(durationSeconds) || durationSeconds <= 0));

	function selectedSources(): string[] {
		return snapshot.sources.filter(isSelected).map((s) => s.name);
	}

	async function start() {
		const sources = selectedSources();
		if (sources.length === 0) {
			toast('Select at least one source', 'error');
			return;
		}
		await sendStart({
			request_id: newRequestId(),
			sources,
			duration_s: durationSeconds && !Number.isNaN(durationSeconds) ? durationSeconds : null,
			takeover: [],
			skip_busy: false
		});
	}

	async function sendStart(request: StartRecordingRequest) {
		busy = true;
		try {
			const result = await api.post<StartRecordingResponse>('/recording/start', request);
			toast(`Started log ${result.active_log.log_id}`, 'success');
			for (const warning of result.warnings ?? []) toast(warning.message, warning.level === 'error' ? 'error' : 'info', 6000);
			conflictOpen = false;
		} catch (e) {
			if (e instanceof ApiError && e.code === 'conflicts') {
				conflicts = (e.details.conflicts ?? []) as Conflict[];
				takeover = Object.fromEntries(conflicts.map((c) => [c.source, true]));
				pendingRequest = request;
				conflictOpen = true;
			} else {
				toast(e instanceof ApiError ? e.message : String(e), 'error');
			}
		} finally {
			busy = false;
			await appState.refresh();
		}
	}

	async function resolveConflicts() {
		if (!pendingRequest) return;
		const chosen = conflicts.filter((c) => takeover[c.source]).map((c) => c.source);
		await sendStart({ ...pendingRequest, request_id: newRequestId(), takeover: chosen, skip_busy: true });
	}

	async function stopClicked() {
		if (!stopArmed) {
			stopArmed = true;
			stopTimer = setTimeout(() => (stopArmed = false), STOP_CONFIRM_MS);
			return;
		}
		clearTimeout(stopTimer);
		stopArmed = false;
		busy = true;
		try {
			const result = await api.post<StopRecordingResponse>('/recording/stop');
			toast(`Stopping log ${result.log_id}`, 'success');
			if (result.stop_pending?.length) {
				toast(`Stop not confirmed by ${result.stop_pending.join(', ')}; retrying`, 'error', 6000);
			}
		} catch (e) {
			toast(e instanceof ApiError ? e.message : String(e), 'error');
		} finally {
			busy = false;
			await appState.refresh();
		}
	}
</script>

<section
	class="card border p-4 sm:p-6 {active
		? arming
			? 'border-warning-500 preset-tonal-warning'
			: 'border-error-500 preset-tonal-error'
		: 'border-surface-200-800 bg-surface-50-950'}"
	data-testid="recording-panel"
>
	<div class="flex flex-col gap-4 sm:flex-row sm:items-center">
		<div class="min-w-0 flex-1 space-y-1">
			{#if !active}
				<div class="text-3xl font-black tracking-wide" data-testid="state-label">IDLE</div>
				<div class="text-sm opacity-75">Select sources below and press Start.</div>
			{:else if arming}
				<div class="text-3xl font-black tracking-wide" data-testid="state-label">
					ARMING <span class="tabular-nums">{Math.max(0, (active.scheduled_start_unix_ns - now) / 1e9).toFixed(1)} s</span>
				</div>
				<div class="mono text-sm break-all" data-testid="active-log-id">{active.log_id}</div>
			{:else}
				<div class="flex items-center gap-2 text-3xl font-black tracking-wide" data-testid="state-label">
					<span class="bg-error-500 inline-block size-4 animate-pulse rounded-full"></span> RECORDING
				</div>
				<div class="mono text-sm break-all" data-testid="active-log-id">{active.log_id}</div>
				<div class="text-lg tabular-nums">
					<span title="since {formatUtc(active.actual_start_unix_ns ?? active.scheduled_start_unix_ns)}">
						{formatDuration(now - (active.actual_start_unix_ns ?? active.scheduled_start_unix_ns))} elapsed
					</span>
					{#if active.stop_at_unix_ns}
						· <span title="stops at {formatLocal(active.stop_at_unix_ns)}">{formatDuration(active.stop_at_unix_ns - now)} remaining</span>
					{/if}
				</div>
			{/if}
		</div>

		{#if !active}
			<div class="flex flex-col gap-2 sm:w-72">
				<label class="label">
					<span class="label-text">Duration (optional)</span>
					<input
						class="input touch-target {durationInvalid ? 'border-error-500' : ''}"
						placeholder="e.g. 90, 5m, 1h30m, 1:30:00"
						bind:value={durationText}
						inputmode="text"
						disabled={disabled || busy}
						data-testid="duration-input"
					/>
				</label>
				<button
					class="btn btn-lg touch-target h-16 bg-green-700 text-xl font-bold text-white hover:bg-green-800"
					onclick={start}
					disabled={disabled || busy || durationInvalid}
					data-testid="start-button">Start</button
				>
			</div>
		{:else}
			<button
				class="btn btn-lg touch-target h-16 text-xl font-bold text-white sm:w-72 {stopArmed
					? 'animate-pulse bg-red-800 ring-4 ring-red-300'
					: 'bg-red-600 hover:bg-red-700'}"
				onclick={stopClicked}
				disabled={disabled || busy}
				data-testid="stop-button">{stopArmed ? 'Press again to stop' : 'Stop'}</button
			>
		{/if}
	</div>
</section>

<Dialog bind:open={conflictOpen} title="Sources are busy">
	<p>These sources are already recording another log. Stop that log and include the source, or leave it out?</p>
	<ul class="space-y-3" data-testid="conflict-list">
		{#each conflicts as conflict (conflict.source)}
			<li class="card border-surface-200-800 space-y-2 border p-3">
				<div>
					<strong>{conflict.source}</strong> is recording {conflict.origin ?? 'an unknown'} log
					<span class="mono break-all">{conflict.log_id}</span>
				</div>
				<div class="flex gap-4">
					<label class="flex items-center gap-2">
						<input type="radio" class="radio" name="conflict-{conflict.source}" bind:group={takeover[conflict.source]} value={true} />
						Stop it and include
					</label>
					<label class="flex items-center gap-2">
						<input type="radio" class="radio" name="conflict-{conflict.source}" bind:group={takeover[conflict.source]} value={false} />
						Leave out
					</label>
				</div>
			</li>
		{/each}
	</ul>
	{#snippet actions()}
		<button class="btn preset-tonal-surface touch-target" onclick={() => (conflictOpen = false)}>Cancel</button>
		<button class="btn touch-target bg-green-700 text-white" onclick={resolveConflicts} disabled={busy} data-testid="conflict-start">
			Start
		</button>
	{/snippet}
</Dialog>
