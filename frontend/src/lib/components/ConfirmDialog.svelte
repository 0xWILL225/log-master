<!-- Yes/no confirmation dialog. -->
<script lang="ts">
	import Dialog from './Dialog.svelte';

	let {
		open = $bindable(false),
		title = 'Are you sure?',
		message,
		confirmLabel = 'Confirm',
		onconfirm
	}: { open?: boolean; title?: string; message: string; confirmLabel?: string; onconfirm: () => void } = $props();
</script>

<Dialog bind:open {title}>
	<p class="whitespace-pre-line">{message}</p>
	{#snippet actions()}
		<button class="btn preset-tonal-surface touch-target" onclick={() => (open = false)}>Cancel</button>
		<button
			class="btn preset-filled-error-500 touch-target"
			onclick={() => {
				open = false;
				onconfirm();
			}}>{confirmLabel}</button
		>
	{/snippet}
</Dialog>
