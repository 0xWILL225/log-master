<!-- Toast notifications. -->
<script lang="ts">
	import { dismissToast, toasts } from '$lib/toast.svelte';

	const styles = { success: 'bg-green-700 text-white', error: 'bg-red-600 text-white', info: 'preset-filled-surface-950-50' };
</script>

<div class="pointer-events-none fixed inset-x-0 bottom-0 z-50 flex flex-col items-center gap-2 p-4" aria-live="polite">
	{#each toasts as t (t.id)}
		<button
			class="card pointer-events-auto max-w-lg px-4 py-3 text-left shadow-lg {styles[t.level]}"
			onclick={() => dismissToast(t.id)}
			data-testid="toast">{t.message}</button
		>
	{/each}
</div>
