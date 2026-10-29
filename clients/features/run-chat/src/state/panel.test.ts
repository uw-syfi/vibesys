import {describe, expect, test} from 'bun:test';
import type {HarnessListing} from '@vibesys/ipc';
import {checkMachine} from '@vibesys/machine/testing';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {getInitialSnapshot, getNextSnapshot} from 'xstate';
import {effectiveHarness, type PanelMachineEvent, panelMachine} from './panel.js';

type Snapshot = ReturnType<typeof getInitialSnapshot<typeof panelMachine>>;

const LISTING: HarnessListing = {
  kind: 'listed',
  harnesses: [
    {provider: 'claude', model: 'opus', default: false},
    {provider: 'claude', model: 'sonnet', default: true},
    {provider: 'codex', model: 'gpt', default: true},
  ],
};

type Settled = {readonly type: string; readonly output?: unknown; readonly error?: unknown};

function step(snapshot: Snapshot, event: PanelMachineEvent | Settled): Snapshot {
  return getNextSnapshot(panelMachine, snapshot, event as PanelMachineEvent);
}

function opened(): Snapshot {
  return step(getInitialSnapshot(panelMachine), {type: 'open'});
}

const done = (id: string, output: unknown) => ({type: `xstate.done.actor.${id}`, output});

describe('panelMachine', () => {
  test('opens and closes, and keeps what was typed while closed', () => {
    let snapshot = step(opened(), {type: 'set-composer', text: 'why?'});
    snapshot = step(snapshot, {type: 'toggle'});
    expect(snapshot.value).toMatchObject({visibility: 'closed'});
    snapshot = step(snapshot, {type: 'toggle'});
    expect(snapshot.value).toMatchObject({chat: 'idle'});
    expect(snapshot.context.composer).toBe('why?');
  });

  test('the catalogue loads on open; a new chat starts on the run own pair, a pick overrides it', () => {
    const loaded = step(opened(), done('loadCatalogue', LISTING));
    expect(loaded.context.catalogue.kind).toBe('listed');
    expect(effectiveHarness(loaded.context)).toEqual({provider: 'claude', model: 'sonnet'});
    // The run's pair may arrive after the catalogue; nothing was picked, so it applies.
    const withRun = step(loaded, {
      type: 'set-preferred',
      preferred: [null, {provider: 'codex', model: 'gpt'}],
    });
    expect(effectiveHarness(withRun.context)).toEqual({provider: 'codex', model: 'gpt'});
    const chosen = step(withRun, {
      type: 'set-harness',
      harness: {provider: 'claude', model: 'opus'},
    });
    expect(effectiveHarness(chosen.context)).toEqual({provider: 'claude', model: 'opus'});
    // A pair the user already chose survives closing and reopening.
    const reopened = step(step(chosen, {type: 'close'}), {type: 'open'});
    expect(effectiveHarness(reopened.context)).toEqual({provider: 'claude', model: 'opus'});
  });

  test('an unreadable catalogue leaves nothing to pick, not a stuck panel', () => {
    const failed = step(opened(), {type: 'xstate.error.actor.loadCatalogue', error: 'no'});
    expect(failed.context.catalogue).toEqual({kind: 'none'});
    expect(failed.value).toMatchObject({catalogue: 'ready'});
  });

  test('closing the panel does not end a question in flight', () => {
    let snapshot = step(opened(), {type: 'set-composer', text: 'why?'});
    snapshot = step(snapshot, {type: 'submit', requestId: 'request-1', threadId: 'thread-1'});
    snapshot = step(snapshot, {type: 'close'});
    expect(snapshot.value).toMatchObject({visibility: 'closed', chat: 'asking'});
    expect(snapshot.context.pending).toEqual({
      requestId: 'request-1',
      threadId: 'thread-1',
      text: 'why?',
    });
  });

  test('a first question creates the thread with the chosen harness, then asks it', () => {
    let snapshot = step(opened(), done('loadCatalogue', LISTING));
    snapshot = step(snapshot, {type: 'new-chat'});
    snapshot = step(snapshot, {type: 'set-composer', text: ' what is best? '});
    snapshot = step(snapshot, {type: 'submit', requestId: 'request-1', threadId: null});
    expect(snapshot.value).toMatchObject({chat: 'creating'});
    expect(snapshot.context.composer).toBe('');
    expect(snapshot.context.pending).toEqual({
      requestId: 'request-1',
      threadId: null,
      text: 'what is best?',
    });
    snapshot = step(snapshot, done('createThread', {ok: true, threadId: 'thread-7'}));
    expect(snapshot.value).toMatchObject({chat: 'asking'});
    expect(snapshot.context.choice).toEqual({kind: 'thread', id: 'thread-7'});
    expect(snapshot.context.pending).toEqual({
      requestId: 'request-1',
      threadId: 'thread-7',
      text: 'what is best?',
    });
    snapshot = step(snapshot, done('ask', {ok: true}));
    expect(snapshot.value).toMatchObject({chat: 'idle'});
    expect(snapshot.context.pending).toBeNull();
  });

  test('a question that failed comes back to the composer with the reason', () => {
    let snapshot = step(opened(), {type: 'set-composer', text: 'why?'});
    snapshot = step(snapshot, {type: 'submit', requestId: 'request-1', threadId: 'thread-1'});
    expect(snapshot.value).toMatchObject({chat: 'asking'});
    snapshot = step(snapshot, done('ask', {ok: false, message: 'the run is not connected'}));
    expect(snapshot.value).toMatchObject({chat: 'idle'});
    expect(snapshot.context.composer).toBe('why?');
    expect(snapshot.context.error).toBe('the run is not connected');
    expect(snapshot.context.pending).toBeNull();
  });

  test('the Steer control turns the draft into a card and asks nothing', () => {
    let snapshot = step(opened(), {type: 'set-composer', text: ' try a smaller batch '});
    snapshot = step(snapshot, {type: 'new-steer'});
    expect(snapshot.value).toMatchObject({chat: 'idle'});
    expect(snapshot.context.userSteers).toEqual([
      {
        id: 'user 1',
        draft: {text: 'try a smaller batch', target: {kind: 'run', id: null}, mode: 'next_turn'},
      },
    ]);
    expect(snapshot.context.composer).toBe('');
    expect(snapshot.context.pending).toBeNull();
    // With nothing typed it is an empty card to write in, and cards get their own ids.
    snapshot = step(snapshot, {type: 'new-steer'});
    expect(snapshot.context.userSteers.map(each => each.id)).toEqual(['user 1', 'user 2']);
    expect(snapshot.context.userSteers[1]?.draft.text).toBe('');
  });

  test('an empty composer submits nothing', () => {
    const snapshot = step(opened(), {type: 'submit', requestId: 'request-1', threadId: 'thread-1'});
    expect(snapshot.value).toMatchObject({chat: 'idle'});
    expect(snapshot.context.pending).toBeNull();
  });

  test('model: work belongs to its state and a pending question exists exactly while asking', () => {
    checkMachine({
      machine: panelMachine,
      input: undefined,
      event: (random, live) =>
        random.pick<PanelMachineEvent | {type: string}>([
          {type: 'toggle'},
          {type: 'open'},
          {type: 'close'},
          {type: 'new-chat'},
          {type: 'choose-thread', id: random.pick(['a', 'b'])},
          {type: 'set-composer', text: random.pick(['', 'why?', 'what changed?'])},
          {type: 'new-steer'},
          {type: 'toggle-list'},
          {type: 'toggle-picker'},
          {type: 'submit', requestId: 'request-1', threadId: random.pick([null, 'a'])},
          ...live.operations.map(operation =>
            live.done(
              operation,
              operation === 'loadCatalogue'
                ? LISTING
                : random.bool()
                  ? {ok: true, threadId: 'new'}
                  : {ok: false, message: 'no'},
            ),
          ),
          ...live.operations.map(operation => live.error(operation, new Error('lost'))),
        ]),
      invariants: ({value, context}) => {
        const chat = typeof value === 'object' ? value.chat : null;
        const busy = chat === 'creating' || chat === 'asking';
        return busy === (context.pending !== null) ? [] : ['pending and busy differ'];
      },
    });
  });
});

test('only close and toggle hide the panel, whatever else the user does or the operations report', () => {
  const events: readonly PanelMachineEvent[] = [
    {type: 'new-chat'},
    {type: 'choose-thread', id: 'a'},
    {type: 'choose-thread', id: 'b'},
    {type: 'set-composer', text: 'why?'},
    {type: 'submit', requestId: 'request-1', threadId: null},
    {type: 'submit', requestId: 'request-2', threadId: 'a'},
    {type: 'new-steer'},
    {type: 'toggle-list'},
    {type: 'toggle-picker'},
  ];
  for (const seed of SEEDS.slice(0, 60)) {
    const random = new Seeded(seed);
    let snapshot = step(opened(), {type: 'open'});
    for (let index = 0; index < 40; index++) {
      snapshot = step(snapshot, random.pick(events));
      expect(snapshot.value).toMatchObject({visibility: 'open'});
    }
  }
});
