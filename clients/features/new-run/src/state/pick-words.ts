/**
 * The words of the pick and review pages that come from the catalogue and the machine: what an
 * example task is, what it needs, how it fits the machine the form is on, which tasks a search and
 * a domain filter keep, and the goal's opening paragraph. Pure; the pages only lay them out.
 */
import {
  type CatalogueEntry,
  DOMAIN_WORDS,
  DOMAINS,
  type Domain,
  exampleLabel,
  hardwareLabel,
} from './catalogue.js';
import {type Fit, machineHardwareWords} from './fit.js';

/** What the task is, in a few words: the model it serves, the workload it drives, or its domain. */
export function whatItIs(entry: CatalogueEntry): string {
  const details = entry.details;
  if (details === null) return '';
  if (details.model !== null) return `Serve ${details.model.name}`;
  if (details.workload !== null) return details.workload.name;
  return DOMAIN_WORDS[details.domain].name;
}

/** The hardware the task targets: "1× AMD MI210", or "CPU". */
export function needsWords(entry: CatalogueEntry): string {
  const hardware = entry.details?.hardware ?? null;
  if (hardware === null) return '';
  return hardware.vendor === 'cpu' ? 'CPU' : hardwareLabel(hardware);
}

/** The example task's name in a list: the example, and its task unless it has only `default`. */
export function entryName(entry: CatalogueEntry): string {
  const name = exampleLabel(entry.example);
  return entry.task === 'default' ? name : `${name} · ${entry.task}`;
}

/**
 * How the task fits the machine named `name`, in a few words; null when nothing is claimed (the
 * results are measured elsewhere, or a side is not known).
 */
export function fitWords(
  fit: Fit,
  name: string,
  hardware: Parameters<typeof machineHardwareWords>[0] | null,
): {readonly text: string; readonly fits: boolean} | null {
  if (fit.kind === 'fits') return {text: `Fits ${name}`, fits: true};
  if (fit.kind === 'mismatch' && hardware !== null) {
    return {text: `${name} has ${machineHardwareWords(hardware)}`, fits: false};
  }
  return null;
}

/** Whether `entry` matches every word of `query`, in its name, task, what it is or what it needs. */
function matchesSearch(entry: CatalogueEntry, query: string): boolean {
  const haystack = [
    entry.example,
    entry.task,
    whatItIs(entry),
    needsWords(entry),
    entry.details?.hardware.accelerator ?? '',
  ]
    .join(' ')
    .toLowerCase();
  return query
    .toLowerCase()
    .split(/\s+/)
    .filter(word => word !== '')
    .every(word => haystack.includes(word));
}

/** One domain filter: a domain (null: every task) and how many tasks it keeps. */
export interface DomainFilter {
  readonly domain: Domain | null;
  readonly label: string;
  readonly count: number;
}

/** "All", then each domain that has a task, with its count of tasks. */
export function domainFilters(entries: readonly CatalogueEntry[]): DomainFilter[] {
  const counted = DOMAINS.map(domain => ({
    domain,
    label: DOMAIN_WORDS[domain].name,
    count: entries.filter(entry => entry.details?.domain === domain).length,
  })).filter(filter => filter.count > 0);
  return [{domain: null, label: 'All', count: entries.length}, ...counted];
}

/** The tasks of `entries` the domain filter and the search keep, in listing order. */
export function shownEntries(
  entries: readonly CatalogueEntry[],
  domain: Domain | null,
  query: string,
): CatalogueEntry[] {
  return entries.filter(
    entry => (domain === null || entry.details?.domain === domain) && matchesSearch(entry, query),
  );
}

/** Markdown's inline marks taken out: emphasis, code, and links (their text stays). */
function plain(text: string): string {
  return text
    .replace(/!?\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/(\*\*|__|`)/g, '')
    .replace(/(^|\s)[*_]([^*_\s][^*_]*)[*_](?=\s|[.,;:!?)]|$)/g, '$1$2')
    .replace(/\s+/g, ' ')
    .trim();
}

/**
 * The goal's opening paragraph as plain text: the first block of an `OBJECTIVE.md` that is prose,
 * not a heading, list, table, quote or code; empty when it has none.
 */
export function goalSummary(goal: string): string {
  const blocks = goal
    .replace(/^(```|~~~)[\s\S]*?^\1[^\n]*$/gm, '')
    .split(/\n\s*\n/)
    .map(block => block.trim());
  const prose = blocks.find(
    block => block !== '' && !/^(#|[-*+]\s|\d+\.\s|\||>|```|~~~|<)/.test(block),
  );
  return prose === undefined ? '' : plain(prose);
}
