<!-- One field of a JSON-Schema form (recursive for nested objects). -->
<script lang="ts">
	import { fieldKind, normalize, type JsonSchema, type Values } from '$lib/schema';
	import Self from './SchemaField.svelte';

	let {
		name,
		schema: rawSchema,
		value = $bindable(),
		path,
		idPrefix,
		errors,
		disabled,
		required = false
	}: {
		name: string;
		schema: JsonSchema;
		value: unknown;
		path: string;
		idPrefix: string;
		errors: Record<string, string>;
		disabled: boolean;
		required?: boolean;
	} = $props();

	const normalized = $derived(normalize(rawSchema));
	const schema = $derived(normalized.schema);
	const nullable = $derived(normalized.nullable);
	const kind = $derived(fieldKind(schema));
	const label = $derived(schema.title ?? name);
	const locked = $derived(disabled || schema.readOnly === true);
	const id = $derived(`${idPrefix}-${path.replaceAll('.', '-')}`);
	const error = $derived(errors[path]);

	let jsonText = $state('');
	let jsonError = $state<string | null>(null);
	$effect.pre(() => {
		if (kind === 'json' && jsonError === null) jsonText = value === undefined ? '' : JSON.stringify(value, null, 2);
	});

	function onJsonInput(event: Event) {
		jsonText = (event.target as HTMLTextAreaElement).value;
		try {
			value = jsonText.trim() === '' ? null : JSON.parse(jsonText);
			jsonError = null;
		} catch (e) {
			jsonError = (e as Error).message;
		}
	}

	function onNumberInput(event: Event) {
		const text = (event.target as HTMLInputElement).value;
		value = text === '' ? (nullable ? null : undefined) : kind === 'integer' ? parseInt(text, 10) : Number(text);
	}
</script>

{#snippet header()}
	<span class="label-text flex flex-wrap items-center gap-2">
		<span class="font-semibold">{label}{required ? ' *' : ''}</span>
		{#if schema.readOnly}<span class="badge preset-tonal-surface text-xs">read-only</span>{/if}
		{#if schema['x-restart-required']}<span class="badge preset-tonal-warning text-xs">restart required</span>{/if}
		{#if schema.writeOnly}<span class="badge preset-tonal-surface text-xs">secret</span>{/if}
	</span>
{/snippet}

{#snippet footer()}
	{#if schema.description}<span class="block text-xs opacity-75">{schema.description}</span>{/if}
	{#if error}<span class="text-error-700-300 block text-sm font-semibold" data-testid="field-error">{error}</span>{/if}
{/snippet}

{#if kind === 'object'}
	<fieldset class="border-surface-200-800 space-y-3 rounded border p-3">
		<legend class="px-1 font-bold">{label}</legend>
		{#each Object.entries(schema.properties ?? {}) as [childName, childSchema] (childName)}
			<Self
				name={childName}
				schema={childSchema}
				bind:value={(value as Values)[childName]}
				path="{path}.{childName}"
				{idPrefix}
				{errors}
				{disabled}
				required={schema.required?.includes(childName) ?? false}
			/>
		{/each}
		{@render footer()}
	</fieldset>
{:else if kind === 'boolean'}
	<label class="flex min-h-11 items-center gap-3" for={id}>
		<input {id} type="checkbox" class="checkbox size-6" bind:checked={value as boolean} disabled={locked} />
		{@render header()}
	</label>
	{@render footer()}
{:else}
	<label class="label" for={id}>
		{@render header()}
		{#if kind === 'enum'}
			<select {id} class="select touch-target" bind:value disabled={locked} {required}>
				{#if nullable || value === undefined}<option value={nullable ? null : undefined}>—</option>{/if}
				{#each schema.enum ?? [] as option (option)}
					<option value={option}>{String(option)}</option>
				{/each}
			</select>
		{:else if kind === 'integer' || kind === 'number'}
			<input
				{id}
				type="number"
				class="input touch-target"
				value={value ?? ''}
				oninput={onNumberInput}
				step={kind === 'integer' ? 1 : 'any'}
				min={schema.minimum ?? schema.exclusiveMinimum}
				max={schema.maximum ?? schema.exclusiveMaximum}
				disabled={locked}
				{required}
			/>
		{:else if kind === 'string' && schema.writeOnly}
			<input
				{id}
				type="password"
				class="input touch-target"
				bind:value
				placeholder="(unchanged)"
				autocomplete="new-password"
				disabled={locked}
			/>
		{:else if kind === 'string'}
			<input {id} type="text" class="input touch-target" bind:value disabled={locked} {required} />
		{:else}
			<textarea {id} class="textarea mono min-h-24" value={jsonText} oninput={onJsonInput} disabled={locked}></textarea>
			<span class="block text-xs opacity-75">Raw JSON (unsupported schema construct)</span>
			{#if jsonError}<span class="text-error-700-300 block text-sm">{jsonError}</span>{/if}
		{/if}
	</label>
	{@render footer()}
{/if}
