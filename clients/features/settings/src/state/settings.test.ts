import {describe, expect, test} from 'bun:test';
import type {PreferencesView} from '@vibesys/ipc';
import {type Appearance, themesOf} from '@vibesys/ui/tokens';
import {
  initialSettings,
  type SettingsAction,
  type SettingsRequest,
  type SettingsState,
  type SettingsStep,
  settingsInvariants,
  settingsStep,
  shownTheme,
} from './index.js';

const STORED: PreferencesView = {
  appearance: {mode: 'system', light: 'paper', dark: 'ink'},
  notifications: {enabled: true},
  archived: [],
};

/** A deterministic stream of numbers in [0, 1), so a property's cases are the same on every run. */
function seeded(seed: number): () => number {
  let state = seed;
  return () => {
    state = (state * 1103515245 + 12345) % 2147483648;
    return state / 2147483648;
  };
}

function pick<T>(random: () => number, values: readonly T[]): T {
  return values[Math.floor(random() * values.length)] as T;
}

function randomAppearance(random: () => number): Appearance {
  return {
    mode: pick(random, ['system', 'light', 'dark'] as const),
    light: pick(random, themesOf('light')),
    dark: pick(random, themesOf('dark')),
  };
}

/** One action: a user change, a reply to any request ever made (stale or not), a push, or the OS. */
function randomAction(random: () => number, issued: readonly SettingsRequest[]): SettingsAction {
  const roll = random();
  const request = issued.length === 0 ? undefined : pick(random, issued);
  const preferences: PreferencesView = {
    ...STORED,
    appearance: randomAppearance(random),
    notifications: {enabled: random() < 0.5},
  };
  if (roll < 0.3) {
    return random() < 0.5
      ? {type: 'change', change: {kind: 'appearance', appearance: randomAppearance(random)}}
      : {type: 'change', change: {kind: 'notifications', enabled: random() < 0.5}};
  }
  if (roll < 0.7 && request !== undefined) {
    const ok = random() < 0.8;
    if (request.kind === 'load') {
      return ok
        ? {type: 'loaded', token: request.token, preferences}
        : {type: 'load-failed', token: request.token, error: 'unreadable'};
    }
    return ok
      ? {type: 'saved', token: request.token, preferences}
      : {type: 'save-failed', token: request.token, error: 'disk full'};
  }
  if (roll < 0.8) return {type: 'retry'};
  if (roll < 0.85) return {type: 'pushed', preferences};
  return {type: 'os', mode: random() < 0.5 ? 'light' : 'dark'};
}

function awaited(state: SettingsState): number | null {
  if (state.kind === 'loading') return state.token;
  return state.kind === 'failed' ? null : (state.saving?.token ?? null);
}

/** What every step must keep: the invariants, issued tokens, new requests, and stale replies ignored. */
function checkStep(
  before: SettingsState,
  action: SettingsAction,
  next: SettingsStep,
  issued: readonly SettingsRequest[],
  context: string,
): void {
  expect(settingsInvariants(next.state), context).toEqual([]);
  const token = awaited(next.state);
  if (token !== null)
    expect(
      issued.some(request => request.token === token),
      context,
    ).toBe(true);
  for (const request of next.requests) {
    const same = issued.filter(each => each.kind === request.kind && each.token === request.token);
    expect(same, context).toHaveLength(1);
  }
  const reply =
    action.type === 'change' ||
    action.type === 'os' ||
    action.type === 'pushed' ||
    action.type === 'retry'
      ? null
      : action.token;
  if (reply !== null && reply !== awaited(before)) expect(next.state, context).toBe(before);
}

describe('settingsStep', () => {
  test('holds its invariants over seeded sequences with stale, duplicated and reordered replies', () => {
    for (let seed = 1; seed <= 200; seed++) {
      const random = seeded(seed);
      const initial = initialSettings('light');
      let state = initial.state;
      const issued: SettingsRequest[] = [...initial.requests];
      for (let step = 0; step < 60; step++) {
        const action = randomAction(random, issued);
        const next = settingsStep(state, action);
        issued.push(...next.requests);
        checkStep(
          state,
          action,
          next,
          issued,
          `seed ${seed}, step ${step}, ${JSON.stringify(action)}`,
        );
        state = next.state;
      }
    }
  });

  test('a read that fails ends failed with no new request, and Retry reads again', () => {
    const {state} = initialSettings('light');
    const failed = settingsStep(state, {type: 'load-failed', token: 1, error: 'unreadable'});
    expect(failed.state).toMatchObject({kind: 'failed', error: 'unreadable'});
    expect(failed.requests).toEqual([]);
    const again = settingsStep(failed.state, {type: 'retry'});
    expect(again.state).toEqual({kind: 'loading', os: 'light', token: 2});
    expect(again.requests).toEqual([{kind: 'load', token: 2}]);
  });

  test('shows a change at once and keeps it over an older save reply', () => {
    let {state} = initialSettings('dark');
    ({state} = settingsStep(state, {type: 'loaded', token: 1, preferences: STORED}));
    const first: Appearance = {mode: 'light', light: 'mist', dark: 'ink'};
    const second: Appearance = {mode: 'light', light: 'sand', dark: 'ink'};
    ({state} = settingsStep(state, {
      type: 'change',
      change: {kind: 'appearance', appearance: first},
    }));
    ({state} = settingsStep(state, {
      type: 'change',
      change: {kind: 'appearance', appearance: second},
    }));
    expect(shownTheme(state, STORED.appearance)).toBe('sand');
    ({state} = settingsStep(state, {
      type: 'saved',
      token: 2,
      preferences: {...STORED, appearance: first},
    }));
    expect(shownTheme(state, STORED.appearance)).toBe('sand');
  });

  test('follows the system appearance only in Match system', () => {
    let {state} = initialSettings('light');
    ({state} = settingsStep(state, {type: 'loaded', token: 1, preferences: STORED}));
    expect(shownTheme(state, STORED.appearance)).toBe('paper');
    ({state} = settingsStep(state, {type: 'os', mode: 'dark'}));
    expect(shownTheme(state, STORED.appearance)).toBe('ink');
    ({state} = settingsStep(state, {
      type: 'pushed',
      preferences: {...STORED, appearance: {mode: 'light', light: 'mist', dark: 'ink'}},
    }));
    expect(shownTheme(state, STORED.appearance)).toBe('mist');
  });
});
