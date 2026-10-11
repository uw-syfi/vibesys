/**
 * Model-based checking of a screen store's reducer: seeded random sequences of user actions and
 * replies (stale, duplicated, and reordered ones included, since a reply's token is drawn from every
 * token issued so far) are folded through the reducer, and after every step:
 *
 * - every token the state awaits was issued, by a request or by the initial state, and no two
 *   slots await the same token;
 * - a reply whose token is not the one its slot awaits leaves the state as it was and asks nothing;
 * - the store's own `invariants` hold, and its `also` properties pass.
 *
 * A failing seed is reported in the error message, to pin as an example.
 */
import type {Reducer, Step} from '../index.js';
import {SEEDS, Seeded} from './seeded.js';

/** The token each request slot awaits, by slot name; null for a slot that awaits nothing. */
export type AwaitedTokens = Readonly<Record<string, number | null>>;

export interface StoreModel<S, A, R> {
  readonly initial: S;
  readonly step: Reducer<S, A, R>;
  /** One random action; replies take their token from `issued`. */
  readonly action: (random: Seeded, issued: readonly number[], state: S) => A;
  /** The token a request issues, or null for one that awaits no reply. */
  readonly issues: (request: R) => number | null;
  /** The token each of the state's request slots awaits. */
  readonly awaited: (state: S) => AwaitedTokens;
  /** For a reply action, the slot it answers and its token; null for any other action. */
  readonly reply: (action: A) => {readonly slot: string; readonly token: number} | null;
  /** What else must hold after every step; returns the violations (empty when none). */
  readonly invariants?: (state: S) => string[];
  /** More properties of one step; throws when one fails. */
  readonly also?: (before: S, action: A, after: S, requests: readonly R[]) => void;
}

/** Violations of the token bookkeeping in `awaited`, given the tokens issued so far. */
function tokenProblems(awaited: AwaitedTokens, issued: ReadonlySet<number>): string[] {
  const problems: string[] = [];
  const slots = new Map<number, string>();
  for (const [slot, token] of Object.entries(awaited)) {
    if (token === null) continue;
    if (!issued.has(token)) problems.push(`${slot} awaits token ${token}, which was never issued`);
    const other = slots.get(token);
    if (other !== undefined) problems.push(`${other} and ${slot} both await token ${token}`);
    slots.set(token, slot);
  }
  return problems;
}

/** What one step broke: token bookkeeping, the store's invariants, or a stale reply that acted. */
function stepProblems<S, A, R>(
  model: StoreModel<S, A, R>,
  before: S,
  action: A,
  next: Step<S, R>,
  issued: ReadonlySet<number>,
): string[] {
  const problems = [
    ...tokenProblems(model.awaited(next.state), issued),
    ...(model.invariants?.(next.state) ?? []),
  ];
  const reply = model.reply(action);
  if (reply === null) return problems;
  const awaited = model.awaited(before)[reply.slot] ?? null;
  const acted = next.state !== before || next.requests.length > 0;
  if (awaited !== reply.token && acted) {
    problems.push(`a ${reply.slot} reply with token ${reply.token} (awaited ${awaited}) acted`);
  }
  return problems;
}

function checkSeed<S, A, R>(model: StoreModel<S, A, R>, seed: number, steps: number): void {
  const random = new Seeded(seed);
  const initiallyAwaited = Object.values(model.awaited(model.initial)).filter(
    (token): token is number => token !== null,
  );
  // Token 0 is offered to replies even when nothing issued it, as a reply no slot awaits.
  const offered: number[] = [0, ...initiallyAwaited];
  const issued = new Set(initiallyAwaited);
  let state = model.initial;
  for (let step = 0; step < steps; step += 1) {
    const action = model.action(random, offered, state);
    const before = state;
    const next = model.step(state, action);
    state = next.state;
    for (const token of next.requests.map(model.issues)) {
      if (token === null) continue;
      offered.push(token);
      issued.add(token);
    }
    const problems = stepProblems(model, before, action, next, issued);
    try {
      model.also?.(before, action, state, next.requests);
    } catch (error) {
      problems.push(error instanceof Error ? error.message : String(error));
    }
    if (problems.length > 0) {
      throw new Error(`checkModel seed ${seed}, step ${step}: ${problems.join('; ')}`);
    }
  }
}

export function checkModel<S, A, R>(model: StoreModel<S, A, R>, steps = 40): void {
  for (const seed of SEEDS) checkSeed(model, seed, steps);
}
