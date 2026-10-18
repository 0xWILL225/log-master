<!-- Generic key/value rows from a source's status `details`. -->
<script lang="ts">
	import type { Schemas } from '$lib/api/client';

	let { details }: { details: Schemas['Detail'][] } = $props();

	const levelClass: Record<string, string> = {
		ok: 'text-success-800-200',
		warn: 'text-warning-800-200 font-semibold',
		error: 'text-error-700-300 font-semibold'
	};
</script>

{#if details.length}
	<dl class="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-sm">
		{#each details as detail (detail.key)}
			<dt class="opacity-75">{detail.label}</dt>
			<dd class="text-right break-words {levelClass[detail.level ?? ''] ?? ''}">
				{detail.value === null ? '–' : String(detail.value)}{detail.unit ? ` ${detail.unit}` : ''}
			</dd>
		{/each}
	</dl>
{/if}
