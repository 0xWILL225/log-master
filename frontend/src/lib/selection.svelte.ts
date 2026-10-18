/** Sources the user explicitly ticked or unticked for the next log; others follow `enabled_by_default`. */
import type { SourceSummary } from './api/client';

const choices = $state<Record<string, boolean>>({});

/** Whether `source` is selected for the next log. */
export function isSelected(source: SourceSummary): boolean {
	return choices[source.name] ?? source.enabled_by_default;
}

/** Record the user's choice for `name`. */
export function select(name: string, selected: boolean): void {
	choices[name] = selected;
}
