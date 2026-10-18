/** Transient notifications shown in the corner of the screen. */

export type ToastLevel = 'success' | 'error' | 'info';

export interface Toast {
	id: number;
	level: ToastLevel;
	message: string;
}

let nextId = 1;

export const toasts = $state<Toast[]>([]);

/** Show a toast for `ms` milliseconds. */
export function toast(message: string, level: ToastLevel = 'info', ms = 4000): void {
	const id = nextId++;
	toasts.push({ id, level, message });
	setTimeout(() => dismissToast(id), ms);
}

/** Remove a toast. */
export function dismissToast(id: number): void {
	const index = toasts.findIndex((t) => t.id === id);
	if (index >= 0) toasts.splice(index, 1);
}
