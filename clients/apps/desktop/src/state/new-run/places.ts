/**
 * The eval place of a new run. The host says which places an entry fits (`Fit.places`); the screen
 * picks the first fitting place in the host's order until the person picks another, and never
 * keeps a pick that no longer fits.
 */
import {type EvalPlace, type Fit, oneofIs} from '@vibesys/orchestrator-proto';

/** Whether the host could read the place's hardware. */
export const readable = (place: EvalPlace): boolean =>
  oneofIs(place.state, 'known', {known: true, unreadable: true});

/** Whether the host named the place as one that gives the entry what it needs. */
export const placeFits = (place: EvalPlace, fit: Fit | undefined): boolean =>
  fit?.places.includes(place.name) === true;

/** The first place, in the host's order, that fits; undefined when none does. */
export function autoPick(places: readonly EvalPlace[], fit: Fit | undefined): string | undefined {
  return places.find(place => placeFits(place, fit))?.name;
}

/** The pick the screen holds: the person's, while it still fits; else the first fitting place. */
export function placeChoice(
  chosen: string | null,
  places: readonly EvalPlace[],
  fit: Fit | undefined,
): string | undefined {
  const kept = places.find(place => place.name === chosen && placeFits(place, fit));
  return kept?.name ?? autoPick(places, fit);
}

/** The word of one place for one entry. */
export type PlaceWord = 'fits' | 'does not fit' | 'unreadable';

export function placeWord(place: EvalPlace, fit: Fit | undefined): PlaceWord {
  if (!readable(place)) return 'unreadable';
  return placeFits(place, fit) ? 'fits' : 'does not fit';
}
