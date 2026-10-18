/**
 * Helpers for rendering forms from JSON Schema (the subset used by configs, settings and
 * action parameters: objects, enums, strings, numbers, booleans, nullable variants).
 */

export interface JsonSchema {
	type?: string | string[];
	properties?: Record<string, JsonSchema>;
	required?: string[];
	enum?: unknown[];
	anyOf?: JsonSchema[];
	title?: string;
	description?: string;
	default?: unknown;
	minimum?: number;
	maximum?: number;
	exclusiveMinimum?: number;
	exclusiveMaximum?: number;
	readOnly?: boolean;
	writeOnly?: boolean;
	'x-restart-required'?: boolean;
	[key: string]: unknown;
}

export type FieldKind = 'object' | 'enum' | 'string' | 'integer' | 'number' | 'boolean' | 'json';

export type Values = Record<string, unknown>;

/** Unwrap `anyOf: [X, {type: null}]` and `type: [X, 'null']` into X plus a nullable flag. */
export function normalize(schema: JsonSchema): { schema: JsonSchema; nullable: boolean } {
	if (schema.anyOf) {
		const nonNull = schema.anyOf.filter((option) => option.type !== 'null');
		const nullable = nonNull.length !== schema.anyOf.length;
		if (nonNull.length === 1) {
			const rest = { ...schema };
			delete rest.anyOf;
			return { schema: { ...nonNull[0], ...rest }, nullable };
		}
		return { schema, nullable };
	}
	if (Array.isArray(schema.type)) {
		const nonNull = schema.type.filter((t) => t !== 'null');
		return { schema: { ...schema, type: nonNull.length === 1 ? nonNull[0] : nonNull }, nullable: nonNull.length !== schema.type.length };
	}
	return { schema, nullable: false };
}

/** Which input renders a (normalized) schema; anything unsupported becomes a raw JSON editor. */
export function fieldKind(schema: JsonSchema): FieldKind {
	if (schema.type === 'object' && schema.properties) return 'object';
	if (Array.isArray(schema.enum)) return 'enum';
	if (schema.type === 'string' || schema.type === 'integer' || schema.type === 'number' || schema.type === 'boolean') {
		return schema.type;
	}
	return 'json';
}

/** Initial form values: `values` where given, else schema defaults; nested objects always exist. */
export function initialValues(schema: JsonSchema, values: Values | undefined): Values {
	const result: Values = {};
	for (const [key, raw] of Object.entries(schema.properties ?? {})) {
		const { schema: prop } = normalize(raw);
		const given = values?.[key];
		if (fieldKind(prop) === 'object') {
			result[key] = initialValues(prop, (given as Values | undefined) ?? (prop.default as Values | undefined));
		} else if (values && key in values) {
			result[key] = structuredClone(given);
		} else if ('default' in prop) {
			result[key] = structuredClone(prop.default);
		} else {
			result[key] = fieldKind(prop) === 'boolean' ? false : undefined;
		}
	}
	return result;
}

function emptyToNull(value: unknown): unknown {
	return value === undefined || value === '' ? null : value;
}

/** Only the changed fields (nested objects recursively); secrets only when something was typed. */
export function diffValues(schema: JsonSchema, original: Values, draft: Values): Values {
	const patch: Values = {};
	for (const [key, raw] of Object.entries(schema.properties ?? {})) {
		const { schema: prop } = normalize(raw);
		if (prop.readOnly) continue;
		if (fieldKind(prop) === 'object') {
			const nested = diffValues(prop, (original[key] ?? {}) as Values, (draft[key] ?? {}) as Values);
			if (Object.keys(nested).length) patch[key] = nested;
		} else if (prop.writeOnly) {
			if (draft[key] !== undefined && draft[key] !== null && draft[key] !== '') patch[key] = draft[key];
		} else if (JSON.stringify(emptyToNull(original[key])) !== JSON.stringify(emptyToNull(draft[key]))) {
			patch[key] = emptyToNull(draft[key]);
		}
	}
	return patch;
}

/** All filled-in fields (for action parameters); empty optional fields are omitted. */
export function fullValues(schema: JsonSchema, draft: Values): Values {
	const result: Values = {};
	for (const [key, raw] of Object.entries(schema.properties ?? {})) {
		const { schema: prop, nullable } = normalize(raw);
		const value = draft[key];
		if (fieldKind(prop) === 'object') {
			result[key] = fullValues(prop, (value ?? {}) as Values);
		} else if (value === undefined || value === '' || (value === null && !nullable)) {
			continue;
		} else {
			result[key] = value;
		}
	}
	return result;
}
