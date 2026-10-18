<!-- Free-space bar, coloured when below the warning thresholds. -->
<script lang="ts">
	import type { Schemas } from '$lib/api/client';
	import { formatBytes } from '$lib/format';

	let { disk, low }: { disk: Schemas['Disk']; low: boolean } = $props();
	const used = $derived(disk.total_bytes > 0 ? 1 - disk.free_bytes / disk.total_bytes : 0);
</script>

<div class="space-y-1" title={disk.path}>
	<div class="bg-surface-200-800 h-2 w-full overflow-hidden rounded">
		<div class="h-full {low ? 'bg-warning-500' : 'bg-primary-500'}" style="width: {Math.round(used * 100)}%"></div>
	</div>
	<div class="text-xs {low ? 'text-warning-800-200 font-semibold' : 'opacity-75'}">
		{formatBytes(disk.free_bytes)} free of {formatBytes(disk.total_bytes)}{low ? ' — low disk space' : ''}
	</div>
</div>
