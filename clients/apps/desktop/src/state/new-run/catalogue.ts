/**
 * The catalogue as the New run screen lists it: the release registry joined to any entries the host
 * can provision, grouped by domain and narrowed by what is typed. Every release example remains
 * browseable without a connected host; host fit only governs whether an available entry can start.
 */
import {
  type CatalogueEntry,
  type CatalogueProblem,
  CatalogueProblemReason,
  type Fit,
  FitState,
  TaskDomain,
} from '@vibesys/orchestrator-proto';
import {
  REGISTERED_EXAMPLES,
  type RegisteredExample,
  type RegisteredExampleCategory,
} from './generated/registered-examples.js';

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

/**
 * One card in the catalogue browser. A host-backed task carries the host's complete entry and fit
 * verdict. A browse-only example is known from the release registry but has no task on the selected
 * host, so it can be inspected but cannot accidentally be sent to ProvisionRepository.
 */
export type CatalogueItem =
  | {
      readonly _tag: 'Available';
      readonly entry: CatalogueEntry;
      readonly registered: RegisteredExample | null;
    }
  | {readonly _tag: 'BrowseOnly'; readonly example: RegisteredExample};

export interface CatalogueItemGroup {
  readonly domain: TaskDomain;
  readonly items: readonly CatalogueItem[];
}

const CATEGORY_DOMAIN: Readonly<Record<RegisteredExampleCategory, TaskDomain>> = {
  'data-structures': TaskDomain.DATA_STRUCTURES,
  database: TaskDomain.DATABASES,
  microservices: TaskDomain.MICROSERVICES,
  'model-serving': TaskDomain.LLM_SERVING,
};

/** Stable identity for an available task or an offline registry card. */
export const catalogueItemKey = (item: CatalogueItem): string =>
  item._tag === 'Available' ? entryKey(item.entry) : `browse:${item.example.id}`;

/** The example id, independent of whether this machine supplies one of its tasks. */
export const catalogueItemExample = (item: CatalogueItem): string =>
  item._tag === 'Available' ? item.entry.example : item.example.id;

/** The registered category, with a host card's authored domain taking precedence when it has one. */
export function catalogueItemDomain(item: CatalogueItem): TaskDomain {
  if (item._tag === 'BrowseOnly') return CATEGORY_DOMAIN[item.example.category];
  const domain = domainOf(item.entry);
  if (domain !== TaskDomain.UNSPECIFIED) return domain;
  return item.registered === null
    ? TaskDomain.UNSPECIFIED
    : CATEGORY_DOMAIN[item.registered.category];
}

/** The host entry only when this item can participate in fit and provisioning. */
export const availableEntry = (item: CatalogueItem): CatalogueEntry | null =>
  item._tag === 'Available' ? item.entry : null;

/** Whether provisioning this example needs the network. */
export const catalogueItemNeedsClone = (item: CatalogueItem): boolean =>
  item._tag === 'Available' ? item.entry.needsClone : item.example.needsClone;

/**
 * Join the immutable release registry to one host's current catalogue. Registered examples the host
 * serves expand to their task entries; every other registered example remains as one browse-only
 * card. Host entries absent from this app's registry are retained for forward compatibility.
 */
export function catalogueItems(
  entries: readonly CatalogueEntry[],
  registered: readonly RegisteredExample[] = REGISTERED_EXAMPLES,
): readonly CatalogueItem[] {
  const byExample = new Map<string, CatalogueEntry[]>();
  for (const entry of entries) {
    const found = byExample.get(entry.example);
    if (found === undefined) byExample.set(entry.example, [entry]);
    else found.push(entry);
  }
  const known = new Set(registered.map(example => example.id));
  return [
    ...registered.flatMap<CatalogueItem>(example => {
      const matching = byExample.get(example.id) ?? [];
      return matching.length === 0
        ? [{_tag: 'BrowseOnly', example}]
        : matching.map(entry => ({_tag: 'Available', entry, registered: example}));
    }),
    ...entries
      .filter(entry => !known.has(entry.example))
      .map<CatalogueItem>(entry => ({_tag: 'Available', entry, registered: null})),
  ];
}

const byItemName = (a: CatalogueItem, b: CatalogueItem): number =>
  (a._tag === b._tag ? 0 : a._tag === 'Available' ? -1 : 1) ||
  folderName({example: catalogueItemExample(a)}).localeCompare(
    folderName({example: catalogueItemExample(b)}),
  ) ||
  catalogueItemKey(a).localeCompare(catalogueItemKey(b));

/** Domain groups for the complete offline-plus-host catalogue. */
export function groupCatalogueItems(
  items: readonly CatalogueItem[],
): readonly CatalogueItemGroup[] {
  return DOMAIN_ORDER.map(domain => ({
    domain,
    items: items.filter(item => catalogueItemDomain(item) === domain).sort(byItemName),
  })).filter(group => group.items.length > 0);
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
    card?.workloadDescription,
    card?.heading,
    card?.hardwareName,
  ]
    .filter((word): word is string => word !== undefined && word !== '')
    .join(' ')
    .toLowerCase();
}

function itemHaystack(item: CatalogueItem): string {
  if (item._tag === 'Available') return haystack(item.entry);
  return [item.example.id, item.example.category, folderName({example: item.example.id})]
    .join(' ')
    .toLowerCase();
}

export interface CatalogueFilter {
  readonly query: string;
}

/** The entries a filter leaves: every word typed is in the entry's authored catalogue words. */
export function visibleEntries(
  entries: readonly CatalogueEntry[],
  filter: CatalogueFilter,
): readonly CatalogueEntry[] {
  const words = filter.query.toLowerCase().split(/\s+/).filter(Boolean);
  return entries.filter(entry => {
    const text = haystack(entry);
    return words.every(word => text.includes(word));
  });
}

/** The catalogue items a query leaves, retaining their registry/host order. */
export function visibleCatalogueItems(
  items: readonly CatalogueItem[],
  filter: CatalogueFilter,
): readonly CatalogueItem[] {
  const words = filter.query.toLowerCase().split(/\s+/).filter(Boolean);
  return items.filter(item => {
    const text = itemHaystack(item);
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
