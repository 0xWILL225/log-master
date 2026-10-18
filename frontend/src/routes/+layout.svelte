<script lang="ts">
	import '../app.css';
	import { page } from '$app/state';
	import Toasts from '$lib/components/Toasts.svelte';
	import { appState } from '$lib/state.svelte';

	let { children } = $props();

	$effect(() => appState.start());

	const links = [
		{ href: '/', label: 'Record' },
		{ href: '/logs', label: 'Logs' },
		{ href: '/settings', label: 'Settings' }
	];
	const recording = $derived(appState.snapshot?.state === 'recording');
</script>

<div class="flex min-h-screen flex-col">
	<header class="border-surface-200-800 bg-surface-50-950 sticky top-0 z-40 border-b">
		<nav class="mx-auto flex max-w-6xl items-center gap-1 px-3 py-2">
			<a href="/" class="mr-2 flex items-center gap-2 font-black tracking-tight whitespace-nowrap" aria-label="log-master">
				<span class="inline-block size-3 rounded-full {recording ? 'animate-pulse bg-red-600' : 'bg-surface-400-600'}"></span>
				<span class="hidden sm:inline">log-master</span>
			</a>
			{#each links as link (link.href)}
				<a
					href={link.href}
					class="btn touch-target {page.url.pathname === link.href ? 'preset-filled-primary-500' : 'preset-tonal-surface'}"
					aria-current={page.url.pathname === link.href ? 'page' : undefined}>{link.label}</a
				>
			{/each}
		</nav>
	</header>

	{#if !appState.connected}
		<div class="preset-filled-error-500 px-4 py-2 text-center font-semibold" role="alert" data-testid="unreachable-banner">
			log-master unreachable — retrying…
		</div>
	{/if}

	<main class="mx-auto w-full max-w-6xl flex-1 space-y-4 px-3 py-4 {appState.connected ? '' : 'pointer-events-none opacity-60 grayscale'}">
		{@render children()}
	</main>
</div>

<Toasts />
