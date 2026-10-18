<!-- Coloured status badge for health levels and per-source log states. -->
<script lang="ts">
	let { value, label }: { value: string | null | undefined; label?: string } = $props();

	const ok = 'bg-green-700 text-white';
	const warn = 'bg-amber-400 text-black';
	const bad = 'bg-red-600 text-white';
	const muted = 'preset-tonal-surface';
	const styles: Record<string, string> = {
		ok,
		warn,
		error: bad,
		recording: bad,
		armed: warn,
		stopping: warn,
		not_joined: warn,
		busy: muted,
		stop_pending: warn,
		lost: bad,
		idle: muted,
		complete: ok,
		interrupted: warn,
		offline: muted
	};
</script>

<span class="badge {styles[value ?? ''] ?? 'preset-tonal-surface'} font-semibold">{label ?? (value ?? '–').replace('_', ' ')}</span>
