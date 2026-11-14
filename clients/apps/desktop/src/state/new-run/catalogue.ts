/**
 * The catalogue as the New run screen lists it: the host's entries grouped by domain, narrowed by
 * what is typed and by the host's own verdict on fit. The client never computes fit; an entry fits
 * when the host's `Fit` says so, and a place fits an entry when the host named it there.
 */
import {
  type CatalogueEntry,
  type CatalogueProblem,
  CatalogueProblemReason,
  type Fit,
  FitState,
  TaskDomain,
} from '@vibesys/orchestrator-proto';

/** The domains in the order the screen lists them; an entry of an unknown domain is listed last. */
const DOMAIN_ORDER: readonly TaskDomain[] = [
  TaskDomain.LLM_SERVING,
  TaskDomain.DATA_STRUCTURES,
  TaskDomain.DATABASES,
  TaskDomain.MICROSERVICES,
  TaskDomain.UNSPECIFIED,
];

/** The domain an entry is listed under: its own when the client knows it, else the last group. */
export function domainOf(entry: CatalogueEntry): TaskDomain {
  const domain = entry.card?.domain ?? TaskDomain.UNSPECIFIED;
  return DOMAIN_ORDER.includes(domain) ? domain : TaskDomain.UNSPECIFIED;
}

/** The folder name of an entry: the last part of its example id, the word that tells two tasks apart. */
export function folderName(entry: Pick<CatalogueEntry, 'example'>): string {
  return (
    entry.example
      .split('/')
      .filter(part => part !== '')
      .at(-1) ?? entry.example
  );
}

/**
 * What tells entries apart: the example (a project root) and the task of it. One example may hold
 * several tasks, each its own entry, so the example alone names none of them.
 */
export const entryKey = (entry: Pick<CatalogueEntry, 'example' | 'card'>): string =>
  `${entry.example}:${entry.card?.task ?? ''}`;

export interface DomainGroup {
  readonly domain: TaskDomain;
  readonly entries: readonly CatalogueEntry[];
}

const byName = (a: CatalogueEntry, b: CatalogueEntry): number =>
  folderName(a).localeCompare(folderName(b)) || entryKey(a).localeCompare(entryKey(b));

/**
 * The entries in domain groups: groups in the screen's order, entries in a group by folder name, no
 * empty group. Every entry is in exactly one group.
 */
export function groupByDomain(entries: readonly CatalogueEntry[]): readonly DomainGroup[] {
  return DOMAIN_ORDER.map(domain => ({
    domain,
    entries: entries.filter(entry => domainOf(entry) === domain).sort(byName),
  })).filter(group => group.entries.length > 0);
}

/** Whether the host says the entry can run on one of its eval places. */
export const fits = (entry: Pick<CatalogueEntry, 'fit'>): boolean =>
  entry.fit?.state === FitState.FITS;

/** The words of a card a search looks in. */
function haystack(entry: CatalogueEntry): string {
  const card = entry.card;
  return [
    entry.example,
    card?.task,
    card?.modelName,
    card?.workloadName,
    card?.heading,
    card?.hardwareName,
  ]
    .filter((word): word is string => word !== undefined && word !== '')
    .join(' ')
    .toLowerCase();
}

export interface CatalogueFilter {
  readonly query: string;
  /** Only the entries the host says fit. */
  readonly fitsOnly: boolean;
}

/** The entries a filter leaves: every word typed is in the entry's words, and the fit when asked. */
export function visibleEntries(
  entries: readonly CatalogueEntry[],
  filter: CatalogueFilter,
): readonly CatalogueEntry[] {
  const words = filter.query.toLowerCase().split(/\s+/).filter(Boolean);
  return entries.filter(entry => {
    if (filter.fitsOnly && !fits(entry)) return false;
    const text = haystack(entry);
    return words.every(word => text.includes(word));
  });
}

/** The host's verdict as a status word. */
export type FitWord = 'Fits' | 'Does not fit' | 'Places unreadable';

export function fitWord(fit: Fit | undefined): FitWord {
  if (fit?.state === FitState.FITS) return 'Fits';
  return fit?.state === FitState.PLACES_UNREADABLE ? 'Places unreadable' : 'Does not fit';
}

/** The host's classified reason the catalogue could not be read, as a title and the host's detail. */
export function catalogueProblemWords(problem: CatalogueProblem): {
  readonly title: string;
  readonly detail: string;
} {
  const title =
    problem.reason === CatalogueProblemReason.NO_RELEASE
      ? 'No release installed'
      : 'Release incomplete';
  return {title, detail: problem.detail};
}
