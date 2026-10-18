<!-- All active warnings; pending stops can be dismissed. -->
<script lang="ts">
	import { api, ApiError, type WarningItem } from '$lib/api/client';
	import { appState } from '$lib/state.svelte';
	import { toast } from '$lib/toast.svelte';

	let { warnings, disabled }: { warnings: WarningItem[]; disabled: boolean } = $props();

	async function dismiss(source: string) {
		try {
			await api.post(`/recording/dismiss-pending/${encodeURIComponent(source)}`);
			toast(`Pending stop of ${source} dismissed`, 'success');
			await appState.refresh();
		} catch (e) {
			toast(e instanceof ApiError ? e.message : String(e), 'error');
		}
	}
</script>

{#if warnings.length}
	<section class="space-y-2" aria-label="Warnings" data-testid="warnings">
		{#each warnings as warning, i (i)}
			<div
				class="card flex items-center gap-3 px-4 py-2 {warning.level === 'error'
					? 'preset-filled-error-500'
					: 'preset-tonal-warning border-warning-500 border'}"
			>
				<span aria-hidden="true">{warning.level === 'error' ? '⛔' : '⚠'}</span>
				<span class="flex-1">{warning.message}</span>
				{#if warning.code === 'stop_pending' && warning.source}
					<button class="btn btn-sm preset-tonal-surface touch-target" {disabled} onclick={() => dismiss(warning.source!)}>
						Dismiss
					</button>
				{/if}
			</div>
		{/each}
	</section>
{/if}
