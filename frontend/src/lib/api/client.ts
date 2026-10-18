/**
 * Typed client of the log-master REST API (`/api/v1`). Types are generated from
 * `spec/log-master.openapi.yaml` by `npm run gen:api`.
 */
import type { components } from './schema';

export type Schemas = components['schemas'];
export type StateSnapshot = Schemas['StateSnapshot'];
export type SourceSummary = Schemas['SourceSummary'];
export type ActiveLogView = Schemas['ActiveLogView'];
export type Participant = Schemas['Participant'];
export type WarningItem = Schemas['WarningItem'];
export type Conflict = Schemas['Conflict'];
export type StartRecordingRequest = Schemas['StartRecordingRequest'];
export type StartRecordingResponse = Schemas['StartRecordingResponse'];
export type StopRecordingResponse = Schemas['StopRecordingResponse'];
export type MergedLog = Schemas['MergedLog'];
export type MergedLogPart = Schemas['MergedLogPart'];
export type LogListResponse = Schemas['LogListResponse'];
export type ConfigDocument = Schemas['ConfigDocument'];
export type ActionSpec = Schemas['ActionSpec'];
export type ActionList = Schemas['ActionList'];
export type ActionResult = Schemas['ActionResult'];
export type SourceList = Schemas['SourceList'];

/** An error response (standard envelope) or a network failure (`status` 0, code `unreachable`). */
export class ApiError extends Error {
	constructor(
		public status: number,
		public code: string,
		message: string,
		public details: Record<string, unknown> = {}
	) {
		super(message);
	}

	/** Field errors of a 422 `validation_error`, keyed by dotted path. */
	fieldErrors(): Record<string, string> {
		const errors = (this.details.errors ?? []) as { path: string; message: string }[];
		return Object.fromEntries(errors.map((e) => [e.path, e.message]));
	}
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
	let response: Response;
	try {
		response = await fetch(`/api/v1${path}`, {
			method,
			headers: body === undefined ? {} : { 'content-type': 'application/json' },
			body: body === undefined ? undefined : JSON.stringify(body)
		});
	} catch {
		throw new ApiError(0, 'unreachable', 'log-master is unreachable');
	}
	if (!response.ok) {
		let envelope: { error?: { code: string; message: string; details?: Record<string, unknown> } } = {};
		try {
			envelope = await response.json();
		} catch {
			// Not JSON (e.g. a proxy error page); reported below with the status text.
		}
		const error = envelope.error;
		throw new ApiError(
			response.status,
			error?.code ?? 'http_error',
			error?.message ?? `${response.status} ${response.statusText}`,
			error?.details ?? {}
		);
	}
	if (response.status === 204) return undefined as T;
	return (await response.json()) as T;
}

export const api = {
	get: <T>(path: string) => request<T>('GET', path),
	post: <T>(path: string, body?: unknown) => request<T>('POST', path, body),
	put: <T>(path: string, body: unknown) => request<T>('PUT', path, body),
	patch: <T>(path: string, body: unknown) => request<T>('PATCH', path, body),
	delete: <T>(path: string) => request<T>('DELETE', path)
};

/** Random request ID; `crypto.randomUUID` is unavailable on plain-HTTP LAN origins, getRandomValues is not. */
export function newRequestId(): string {
	const bytes = crypto.getRandomValues(new Uint8Array(16));
	return Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
}

/** URL path segment encoding that keeps `/` (for file paths inside a log). */
export function encodePath(path: string): string {
	return path.split('/').map(encodeURIComponent).join('/');
}
