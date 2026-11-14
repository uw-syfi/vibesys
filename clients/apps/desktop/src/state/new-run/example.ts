/**
 * The person's choices on the catalogue half of New run, and what they come to: the example, the
 * eval place, the name and the folder it is copied to. Nothing here decides fit: the example fits
 * when the host's verdict on its entry says so, and a place when the host named it there.
 */
import type {CatalogueEntry, EvalPlace} from '@vibesys/orchestrator-proto';
import {entryKey, fits, fitWord, folderName} from './catalogue.js';
import {placeChoice} from './places.js';
import {defaultName, parentOf} from './provision.js';

export interface ExampleDraft {
  readonly query: string;
  /** The entry picked, by `entryKey`, or null. */
  readonly picked: string | null;
  /** The eval place the person picked, or null for the first fitting one. */
  readonly place: string | null;
  /** The repository name the person typed, or null for the example's folder name. */
  readonly name: string | null;
  /** The folder to copy into as typed (`~` allowed); empty is the host's own place. */
  readonly parent: string;
}

export const emptyExampleDraft: ExampleDraft = {
  query: '',
  picked: null,
  place: null,
  name: null,
  parent: '',
};

/** Pick an entry (by `entryKey`): the place and the name go back to their own defaults for it. */
export const pickExample = (draft: ExampleDraft, example: string): ExampleDraft => ({
  ...draft,
  picked: example,
  place: null,
  name: null,
});

/** What a Start from an example would send, or why it cannot yet. */
export type ExampleTarget =
  | {readonly ready: false; readonly reason: string}
  | {
      readonly ready: true;
      readonly entry: CatalogueEntry;
      readonly place: string;
      readonly name: string;
      readonly parent: string;
    };

/** The name the example is copied to: what was typed, trimmed, else the folder name. */
export const nameOfExample = (draft: ExampleDraft, entry: CatalogueEntry): string =>
  (draft.name ?? defaultName(folderName(entry))).trim();

export function exampleTarget(
  entries: readonly CatalogueEntry[],
  places: readonly EvalPlace[],
  draft: ExampleDraft,
): ExampleTarget {
  const entry = entries.find(each => entryKey(each) === draft.picked);
  if (entry === undefined) return {ready: false, reason: 'Pick an example'};
  if (!fits(entry)) return {ready: false, reason: fitWord(entry.fit)};
  const place = placeChoice(draft.place, places, entry.fit);
  if (place === undefined) return {ready: false, reason: 'No place'};
  const name = nameOfExample(draft, entry);
  if (name === '') return {ready: false, reason: 'Name the repository'};
  return {ready: true, entry, place, name, parent: parentOf(draft.parent)};
}
