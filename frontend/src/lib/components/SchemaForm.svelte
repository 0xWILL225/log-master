<!--
	Form rendered from a JSON Schema. In `patch` mode only changed fields are submitted (config,
	settings); in `full` mode all filled-in fields are (action parameters). 422 validation errors
	thrown by `onsubmit` are shown inline next to their fields.
-->
<script lang="ts">
	import { ApiError } from '$lib/api/client';
	import { diffValues, fullValues, initialValues, type JsonSchema, type Values } from '$lib/schema';
	import { toast } from '$lib/toast.svelte';
	import SchemaField from './SchemaField.svelte';

	let {
		schema,
		values,
		mode,
		disabled = false,
		submitLabel = 'Save',
		onsubmit
	}: {
		schema: JsonSchema;
		values?: Values;
		mode: 'patch' | 'full';
		disabled?: boolean;
		submitLabel?: string;
		onsubmit: (payload: Values) => Promise<void>;
	} = $props();

	const formId = $props.id();
	let original = $state<Values>({});
	let draft = $state<Values>({});
	let errors = $state<Record<string, string>>({});
	let submitting = $state(false);

	$effect.pre(() => {
		original = initialValues(schema, values);
		draft = initialValues(schema, values);
		errors = {};
	});

	const changed = $derived(mode === 'full' || Object.keys(diffValues(schema, original, draft)).length > 0);

	async function submit(event: SubmitEvent) {
		event.preventDefault();
		const payload = mode === 'patch' ? diffValues(schema, original, draft) : fullValues(schema, draft);
		submitting = true;
		errors = {};
		try {
			await onsubmit(payload);
		} catch (e) {
			if (e instanceof ApiError && e.status === 422) errors = e.fieldErrors();
			toast(e instanceof ApiError ? e.message : String(e), 'error', 6000);
		} finally {
			submitting = false;
		}
	}
</script>

<form class="space-y-3" onsubmit={submit} novalidate={mode === 'patch'}>
	{#each Object.entries(schema.properties ?? {}) as [name, propSchema] (name)}
		<SchemaField
			{name}
			schema={propSchema}
			bind:value={draft[name]}
			path={name}
			idPrefix={formId}
			{errors}
			{disabled}
			required={schema.required?.includes(name) ?? false}
		/>
	{/each}
	<div class="flex flex-wrap gap-2 pt-1">
		<button type="submit" class="btn preset-filled-primary-500 touch-target" disabled={disabled || submitting || !changed}>
			{submitting ? 'Working…' : submitLabel}
		</button>
		{#if mode === 'patch' && changed}
			<button type="button" class="btn preset-tonal-surface touch-target" onclick={() => (draft = initialValues(schema, original))}>
				Reset
			</button>
		{/if}
	</div>
</form>
