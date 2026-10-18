/**
 * The aggregate `/api/v1/state` snapshot, polled once per second while the page is visible.
 */
import { api, type StateSnapshot } from './api/client';

const POLL_INTERVAL_MS = 1000;
const TICK_INTERVAL_MS = 250;

class AppState {
	/** Latest snapshot (kept while disconnected so the UI can grey it out). */
	snapshot = $state<StateSnapshot | null>(null);
	/** False after a failed poll until the next successful one. */
	connected = $state(true);
	/** Browser time, updated a few times per second for countdowns and elapsed times. */
	now = $state(Date.now());
	/** `server time - browser time` in milliseconds, so countdowns ignore the browser's clock. */
	private offsetMs = 0;
	private pollTimer: ReturnType<typeof setTimeout> | undefined;
	private tickTimer: ReturnType<typeof setInterval> | undefined;
	private running = false;

	/** Server time in nanoseconds, extrapolated from the last snapshot. */
	get serverNowNs(): number {
		return (this.now + this.offsetMs) * 1e6;
	}

	/** Start polling; pauses while the tab is hidden. */
	start(): () => void {
		const onVisibility = () => (document.hidden ? this.pause() : this.resume());
		document.addEventListener('visibilitychange', onVisibility);
		this.tickTimer = setInterval(() => (this.now = Date.now()), TICK_INTERVAL_MS);
		this.resume();
		return () => {
			document.removeEventListener('visibilitychange', onVisibility);
			clearInterval(this.tickTimer);
			this.pause();
		};
	}

	/** Fetch a fresh snapshot now (e.g. right after start/stop). */
	async refresh(): Promise<void> {
		try {
			const snapshot = await api.get<StateSnapshot>('/state');
			this.offsetMs = snapshot.server_time_unix_ns / 1e6 - Date.now();
			this.snapshot = snapshot;
			this.connected = true;
		} catch {
			this.connected = false;
		}
	}

	private resume(): void {
		if (this.running) return;
		this.running = true;
		const loop = async () => {
			await this.refresh();
			if (this.running) this.pollTimer = setTimeout(loop, POLL_INTERVAL_MS);
		};
		void loop();
	}

	private pause(): void {
		this.running = false;
		clearTimeout(this.pollTimer);
	}
}

export const appState = new AppState();
