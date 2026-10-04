import type {RunEvent} from '@vibesys/backend-client';

/** A round identity derived from a backend event's scope. */
export type RoundKey =
  | {readonly kind: 'number'; readonly number: number}
  | {readonly kind: 'label'; readonly label: string};

/**
 * Returns the stable round identity carried by an event-like protocol value.
 *
 * Numbered label families retain their established numeric identity. Every
 * other non-empty label is still a round: exact label equality groups its
 * events, and callers order first-seen label keys rather than inventing a
 * number. This is the sole label-to-round identity seam beneath the fold.
 */
export function roundKeyFor(
  event: Pick<RunEvent, 'round_label'> | {readonly round_label?: string | null | undefined},
): RoundKey | null {
  const label = event.round_label;
  if (!label) return null;
  const match = label.match(/(?:round|iter(?:ation)?)\D*(\d+)/i);
  return match === null ? {kind: 'label', label} : {kind: 'number', number: Number(match[1])};
}

/** Numeric compatibility/display metadata for a key, absent for fallback keys. */
export function roundNumberFor(key: RoundKey | null): number | null {
  return key?.kind === 'number' ? key.number : null;
}

/** Structural equality for tagged keys created by independent folds. */
export function sameRoundKey(left: RoundKey | null, right: RoundKey | null): boolean {
  if (left === null || right === null) return left === right;
  return left.kind === right.kind && roundKeyToken(left) === roundKeyToken(right);
}

/** Collision-free scalar form used only by internal indexes. */
export function roundKeyToken(key: RoundKey): string {
  return key.kind === 'number' ? `number:${key.number}` : `label:${JSON.stringify(key.label)}`;
}
