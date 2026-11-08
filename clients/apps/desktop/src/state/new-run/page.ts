/**
 * The New run page as data: which step it is on, what the person chose on the way, and the figures
 * its machine strip and catalogue read off a host's answers. Plain reducers and pure functions, so
 * every path through the page is a value a test can walk. The host decides fit; the client only
 * leaves out what the page does not offer (cloud places) and words what the host sent.
 */
import {
  type CardObjective,
  type CatalogueEntry,
  type EvalPlace,
  EvalPlaceKind,
  TaskObjectiveDirection,
} from '@vibesys/orchestrator-proto';
import {fits} from './catalogue.js';

/** The page's steps: the two entry cards, the catalogue, and the person's own repository. */
export type NewRunStep = 'start' | 'catalog' | 'own';

/** What the person brings on the My own step. */
export type OwnCase =
  /** A repository that has tasks: pick one and start. */
  | 'tasks'
  /** A repository without a task: a setup chat makes one. */
  | 'repo'
  /** Nothing yet: the repository is made, then the setup chat. */
  | 'none';

export interface NewRunPage {
  readonly step: NewRunStep;
  readonly own: OwnCase | null;
  /** The task picked in the repository the person named, or null. */
  readonly ownTask: string | null;
  /** The name of the repository to make, for `none`. */
  readonly newName: string;
}

export const firstPage: NewRunPage = {step: 'start', own: null, ownTask: null, newName: ''};

export type PageAction =
  | {readonly type: 'open'; readonly step: NewRunStep}
  | {readonly type: 'own'; readonly own: OwnCase}
  | {readonly type: 'own-task'; readonly task: string | null}
  | {readonly type: 'new-name'; readonly name: string}
  /** The machine changed: what was chosen on the old one no longer stands. */
  | {readonly type: 'machine'}
  /** One step back; at the first step it means leaving the page (`leaves`). */
  | {readonly type: 'back'};

export function pageReducer(page: NewRunPage, action: PageAction): NewRunPage {
  switch (action.type) {
    case 'open':
      return action.step === 'own' ? {...firstPage, step: 'own'} : {...page, step: action.step};
    case 'own':
      return {...page, own: action.own, ownTask: null};
    case 'own-task':
      return {...page, ownTask: action.task};
    case 'new-name':
      return {...page, newName: action.name};
    case 'machine':
      return {...page, ownTask: null};
    case 'back':
      return page.step === 'start' ? page : {...page, step: 'start'};
  }
}

/** Whether going back from `page` leaves the page for the run list. */
export const leaves = (page: NewRunPage): boolean => page.step === 'start';

/** The places the page offers: every one but the cloud's. */
export const offeredPlaces = (places: readonly EvalPlace[]): readonly EvalPlace[] =>
  places.filter(place => place.kind !== EvalPlaceKind.CLOUD);

/**
 * Whether an entry fits the machine: the host says it fits and names at least one place the page
 * offers, since a fit that only a cloud place gives has nowhere to run.
 */
export function fitsHere(entry: CatalogueEntry, places: readonly EvalPlace[]): boolean {
  if (!fits(entry)) return false;
  const offered = new Set(offeredPlaces(places).map(place => place.name));
  return entry.fit?.places.some(name => offered.has(name)) === true;
}

/** The places of the machine that give an entry what it needs, in the host's order. */
export function placesFor(
  entry: CatalogueEntry,
  places: readonly EvalPlace[],
): readonly EvalPlace[] {
  return offeredPlaces(places).filter(place => entry.fit?.places.includes(place.name) === true);
}

/** Words of a task name that tell a hardware (an accelerator count or model) and no variant. */
const HARDWARE_WORD = /^(\d+x)?((mi|h|b|a|l|trn)\d+[a-z]*\d*|gpu|cpu)$/i;

/** The words of a name, lower case: its parts between dashes, dots, underscores and spaces. */
const wordsOf = (name: string): readonly string[] =>
  name
    .toLowerCase()
    .split(/[\s\-_.]+/)
    .filter(word => word !== '');

/**
 * The variant of an example, from the name of its task folder: the words of it that neither name
 * hardware, nor say `default`, nor are already said by the example's folder or by the card's subject
 * (its model, workload or heading, whichever a title leads with), in lower case. None when no word is
 * left, so `default`, `mi300x` or a task named for its example carry no tag. The catalogue has no
 * variant field; this is a reading of folder names.
 */
export function variantOf(entry: Pick<CatalogueEntry, 'card' | 'example'>): string | undefined {
  const card = entry.card;
  const said = new Set([
    ...wordsOf(
      entry.example
        .split('/')
        .filter(part => part !== '')
        .at(-1) ?? '',
    ),
    ...wordsOf(
      [card?.modelName, card?.workloadName, card?.heading].find(
        word => word !== undefined && word.trim() !== '',
      ) ?? '',
    ),
  ]);
  const words = wordsOf(card?.task ?? '').filter(
    word => word !== 'default' && !HARDWARE_WORD.test(word) && !said.has(word),
  );
  return words.length === 0 ? undefined : words.join(' ');
}

/** One objective of a card as a chip: its direction's arrow and its name in words. */
export function objectiveChip(objective: CardObjective): {
  readonly text: string;
  readonly hint: string;
} {
  const arrow = objective.direction === TaskObjectiveDirection.MAX ? '↑' : '↓';
  const name = objective.name.replace(/_/g, ' ');
  return {
    text: `${arrow} ${name}`,
    hint: objective.unit === '' ? name : `${name} (${objective.unit})`,
  };
}

/** What the machine strip counts for the machine shown; `null` is not read yet. */
export interface MachineFacts {
  readonly places: number | null;
  readonly fitting: number | null;
  readonly examples: number | null;
  readonly repositories: number | null;
  /** Repositories that hold tasks. */
  readonly withTasks: number | null;
}

export interface FolderEntryFacts {
  readonly isGitRepository: boolean;
  readonly hasTasks: boolean;
}

/** The strip's figures from what the host said of its places, its catalogue and the Repositories folder. */
export function machineFacts(
  places: readonly EvalPlace[] | null,
  entries: readonly CatalogueEntry[] | null,
  folder: readonly FolderEntryFacts[] | null,
): MachineFacts {
  const offered = places === null ? null : offeredPlaces(places);
  return {
    places: offered === null ? null : offered.length,
    fitting:
      entries === null || offered === null
        ? null
        : entries.filter(entry => fitsHere(entry, offered)).length,
    examples: entries === null ? null : entries.length,
    repositories: folder === null ? null : folder.filter(entry => entry.isGitRepository).length,
    withTasks: folder === null ? null : folder.filter(entry => entry.hasTasks).length,
  };
}
