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
      typed: true,
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
      typed: true,
    });
    snapshot = step(snapshot, done('createThread', {ok: true, threadId: 'thread-7'}));
    expect(snapshot.value).toMatchObject({chat: 'asking'});
    expect(snapshot.context.choice).toEqual({kind: 'thread', id: 'thread-7'});
    expect(snapshot.context.pending).toEqual({
      requestId: 'request-1',
      threadId: 'thread-7',
      text: 'what is best?',
      typed: true,
    });
    snapshot = step(snapshot, done('ask', {ok: true}));
    expect(snapshot.value).toMatchObject({chat: 'idle'});
    expect(snapshot.context.pending).toBeNull();
  });

  test('a question that failed comes back to the composer with the reason', () => {
    let snapshot = step(opened(), {type: 'set-composer', text: 'why?'});
    snapshot = step(snapshot, {type: 'submit', requestId: 'request-1', threadId: 'thread-1'});
    expect(snapshot.value).toMatchObject({chat: 'asking'});
    snapshot = step(
      snapshot,
      done('ask', {ok: false, message: 'the run is not connected', detail: 'stderr line'}),
    );
    expect(snapshot.value).toMatchObject({chat: 'idle'});
    expect(snapshot.context.composer).toBe('why?');
    expect(snapshot.context.error).toEqual({
      message: 'the run is not connected',
      detail: 'stderr line',
    });
    expect(snapshot.context.pending).toBeNull();
  });

  test('a question whose call threw is a message without a diagnostic', () => {
    let snapshot = step(opened(), {type: 'set-composer', text: 'why?'});
    snapshot = step(snapshot, {type: 'submit', requestId: 'request-1', threadId: 'thread-1'});
    snapshot = step(snapshot, {type: 'xstate.error.actor.ask', error: new Error('lost')});
    expect(snapshot.context.error).toEqual({message: 'lost', detail: null});
    snapshot = step(snapshot, {type: 'submit', requestId: 'request-2', threadId: 'thread-1'});
    snapshot = step(snapshot, {type: 'xstate.error.actor.ask', error: 'gone'});
    expect(snapshot.context.error).toEqual({message: 'gone', detail: null});
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

  test('asking about an error opens the panel on a new thread and sends only that question', () => {
    const snapshot = step(getInitialSnapshot(panelMachine), {
      type: 'ask-new',
      text: 'Q',
      requestId: 'r-9',
    });
    expect(snapshot.value).toMatchObject({
      visibility: 'open',
      chat: 'preparing',
      catalogue: 'loading',
    });
    expect(snapshot.context.choice).toEqual({kind: 'new'});
    expect(snapshot.context.pending).toEqual({
      threadId: null,
      text: 'Q',
      requestId: 'r-9',
      typed: false,
    });
    // The thread is made once the catalogue says which harness it runs on.
    const listed = step(snapshot, done('loadCatalogue', LISTING));
    expect(listed.value).toMatchObject({chat: 'creating', catalogue: 'ready'});
    expect(listed.context.pending).toEqual({
      threadId: null,
      text: 'Q',
      requestId: 'r-9',
      typed: false,
    });
  });

  test('asking about an error mid-question replaces it and keeps the draft', () => {
    let snapshot = step(step(opened(), done('loadCatalogue', LISTING)), {
      type: 'set-composer',
      text: 'why?',
    });
    snapshot = step(snapshot, {type: 'submit', requestId: 'request-1', threadId: 'thread-1'});
    snapshot = step(snapshot, {type: 'set-composer', text: 'draft'});
    expect(snapshot.value).toMatchObject({chat: 'asking'});
    snapshot = step(snapshot, {type: 'ask-new', text: 'Q', requestId: 'r-9'});
    expect(snapshot.value).toMatchObject({chat: 'creating'});
    expect(snapshot.context.pending?.text).toBe('Q');
    expect(snapshot.context.composer).toBe('draft');
    // Asked again while its thread is still being made, the newer question wins.
    snapshot = step(snapshot, {type: 'ask-new', text: 'Q2', requestId: 'r-10'});
    expect(snapshot.value).toMatchObject({chat: 'creating'});
    expect(snapshot.context.pending).toEqual({
      threadId: null,
      text: 'Q2',
      requestId: 'r-10',
      typed: false,
    });
    // If it fails, the draft is not replaced by the question.
    snapshot = step(snapshot, done('createThread', {ok: false, message: 'no', detail: null}));
    expect(snapshot.context.composer).toBe('draft');
  });

  test("asking about an error mid-question puts the user's question back in an empty composer", () => {
    for (const threadId of [null, 'thread-1']) {
      let snapshot = step(step(opened(), done('loadCatalogue', LISTING)), {
        type: 'set-composer',
        text: 'why?',
      });
      snapshot = step(snapshot, {type: 'submit', requestId: 'request-1', threadId});
      expect(snapshot.context.composer).toBe('');
      snapshot = step(snapshot, {type: 'ask-new', text: 'Q', requestId: 'r-9'});
      expect(snapshot.context.composer).toBe('why?');
    }
  });

  test('a failed question about an error leaves an empty composer empty', () => {
    let snapshot = step(getInitialSnapshot(panelMachine), {
      type: 'ask-new',
      text: 'Q',
      requestId: 'r-9',
    });
    snapshot = step(snapshot, done('loadCatalogue', LISTING));
    snapshot = step(snapshot, done('createThread', {ok: true, threadId: 'thread-7'}));
    snapshot = step(snapshot, done('ask', {ok: false, message: 'no', detail: null}));
    expect(snapshot.value).toMatchObject({chat: 'idle'});
    expect(snapshot.context.error).toEqual({message: 'no', detail: null});
    expect(snapshot.context.composer).toBe('');
  });

  test('asking about an error with no text changes nothing', () => {
    const closed = getInitialSnapshot(panelMachine);
    const after = step(closed, {type: 'ask-new', text: ' ', requestId: 'r-9'});
    expect(after.value).toEqual(closed.value);
    expect(after.context).toEqual(closed.context);
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
          {type: 'ask-new', text: random.pick(['', 'Q']), requestId: 'r-x'},
          ...live.operations.map(operation =>
            live.done(
              operation,
              operation === 'loadCatalogue'
                ? LISTING
                : random.bool()
                  ? {ok: true, threadId: 'new'}
                  : {ok: false, message: 'no', detail: random.bool() ? 'trace' : null},
            ),
          ),
          ...live.operations.map(operation => live.error(operation, new Error('lost'))),
        ]),
      invariants: ({value, context}) => {
        const chat = typeof value === 'object' ? value.chat : null;
        const busy = chat === 'preparing' || chat === 'creating' || chat === 'asking';
        return [
          ...(busy === (context.pending !== null) ? [] : ['pending and busy differ']),
          // Only the user's own words reach the composer, never a question made from an error.
          ...(context.composer === 'Q' ? ['the question about an error is in the composer'] : []),
        ];
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
