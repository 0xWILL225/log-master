/** Formatting helpers. Times from the API are nanoseconds since the Unix epoch (UTC). */

const NS_PER_MS = 1e6;

/** Byte count with a binary unit, e.g. `4.2 GiB`. */
export function formatBytes(bytes: number | null | undefined): string {
	if (bytes === null || bytes === undefined) return '–';
	const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
	let value = bytes;
	let unit = 0;
	while (Math.abs(value) >= 1024 && unit < units.length - 1) {
		value /= 1024;
		unit += 1;
	}
	return unit === 0 ? `${value} B` : `${value.toFixed(1)} ${units[unit]}`;
}

/** Duration as `h:mm:ss`. */
export function formatDuration(ns: number | null | undefined): string {
	if (ns === null || ns === undefined) return '–';
	const total = Math.max(0, Math.round(ns / 1e9));
	const h = Math.floor(total / 3600);
	const m = Math.floor((total % 3600) / 60);
	const s = total % 60;
	return `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
}

/** Local date and time, e.g. `2026-10-17 16:03:55`. */
export function formatLocal(ns: number | null | undefined): string {
	if (ns === null || ns === undefined) return '–';
	const d = new Date(ns / NS_PER_MS);
	const pad = (n: number) => String(n).padStart(2, '0');
	return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

/** UTC ISO time for tooltips. */
export function formatUtc(ns: number | null | undefined): string {
	if (ns === null || ns === undefined) return '';
	return new Date(ns / NS_PER_MS).toISOString().replace('T', ' ').replace(/\.\d+Z$/, ' UTC');
}

/** Short age such as `12 s`, `3 min`, `2 h`. */
export function formatAge(ns: number): string {
	const seconds = Math.max(0, Math.round(ns / 1e9));
	if (seconds < 120) return `${seconds} s`;
	if (seconds < 7200) return `${Math.round(seconds / 60)} min`;
	return `${Math.round(seconds / 3600)} h`;
}

/** Parse a duration like `90`, `90s`, `5m`, `1h30m` or `1:30:00` into seconds; `null` if empty, `NaN` if invalid. */
export function parseDuration(text: string): number | null {
	const value = text.trim();
	if (value === '') return null;
	if (/^\d+(\.\d+)?$/.test(value)) return Number(value);
	if (/^\d+(:\d{1,2}){1,2}$/.test(value)) {
		return value.split(':').reduce((acc, part) => acc * 60 + Number(part), 0);
	}
	const match = /^(?:(\d+)h)?\s*(?:(\d+)m)?\s*(?:(\d+)s)?$/.exec(value);
	if (!match || !match[0]) return NaN;
	return Number(match[1] ?? 0) * 3600 + Number(match[2] ?? 0) * 60 + Number(match[3] ?? 0);
}

/** Unix nanoseconds of a local date (`YYYY-MM-DD`), as a decimal string safe for query parameters. */
export function localDateToNs(date: string, endOfDay = false): string {
	const [y, m, d] = date.split('-').map(Number);
	const ms = new Date(y, m - 1, endOfDay ? d + 1 : d).getTime();
	return (BigInt(ms) * 1_000_000n).toString();
}
