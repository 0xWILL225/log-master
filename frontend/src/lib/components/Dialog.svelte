<!-- Modal dialog built on the native <dialog> element (focus trap, Esc to close). -->
<script lang="ts">
	import type { Snippet } from 'svelte';

	let {
		open = $bindable(false),
		title,
		children,
		actions
	}: { open?: boolean; title: string; children: Snippet; actions?: Snippet } = $props();

	let dialog = $state<HTMLDialogElement>();

	$effect(() => {
		if (!dialog) return;
		if (open && !dialog.open) dialog.showModal();
		if (!open && dialog.open) dialog.close();
	});
</script>

<dialog
	bind:this={dialog}
	onclose={() => (open = false)}
	class="card bg-surface-50-950 text-surface-950-50 m-auto w-[min(32rem,calc(100vw-2rem))] space-y-4 p-5 shadow-xl backdrop:bg-black/50"
>
	<h2 class="h4">{title}</h2>
	{@render children()}
	{#if actions}
		<div class="flex flex-wrap justify-end gap-2">{@render actions()}</div>
	{/if}
</dialog>
