import {describe, expect, test} from 'bun:test';
import {Seeded} from '@vibesys/store/testing';
import {
  READER_IDLE,
  type ReaderReply,
  type ReaderRequest,
  type ReaderState,
  type ReaderStep,
  readerAttached,
  readerHeld,
  readerWanted,
  reconcileReader,
} from './reader.js';

describe('the reader link', () => {
  test('opening Chat on an ended run attaches once; closing lets it go', () => {
    const opened = readerWanted(READER_IDLE, true, true, 1);
    expect(opened.requests).toEqual([{kind: 'attach-reader', token: 1}]);
    const again = reconcileReader(opened.reader, true, opened.next);
    expect(again.requests).toEqual([]);
    const attached = readerAttached(again.reader, 1, {kind: 'attached'}, again.next);
    expect(attached.reader.link).toEqual({kind: 'attached'});
    const closed = readerWanted(attached.reader, false, true, attached.next);
    expect(closed.requests).toEqual([{kind: 'release-reader'}]);
    expect(closed.reader.link).toEqual({kind: 'none'});
  });

  test('Chat on a run that is not ended attaches nothing', () => {
    expect(readerWanted(READER_IDLE, true, false, 1).requests).toEqual([]);
  });

  test('a host that cannot serve an ended run is not asked again, and Chat closing keeps that', () => {
    let step = readerWanted(READER_IDLE, true, true, 1);
    step = readerAttached(step.reader, 1, {kind: 'unsupported'}, step.next);
    step = readerWanted(step.reader, false, true, step.next);
    step = readerWanted(step.reader, true, true, step.next);
    expect(step.requests).toEqual([]);
    expect(step.reader.link).toEqual({kind: 'unsupported'});
  });

  test('a failed attach is tried again when Chat is opened again', () => {
    let step = readerWanted(READER_IDLE, true, true, 1);
    step = readerAttached(step.reader, 1, {kind: 'failed', message: 'no'}, step.next);
    step = readerWanted(step.reader, false, true, step.next);
    step = readerWanted(step.reader, true, true, step.next);
    expect(step.requests).toEqual([{kind: 'attach-reader', token: 2}]);
  });

  test('property: whatever is opened, closed and answered late, nothing stays bound once Chat is closed', () => {
    for (let seed = 0; seed < 300; seed += 1) {
      const world = new World();
      const random = new Seeded(seed);
      for (let turn = 0; turn < 40; turn += 1) world.act(random);
      world.finish();
      expect(readerHeld(world.reader)).toBe(false);
      expect(world.bound).toBe(false);
    }
  });
});

/** The page's reader and the app's side of it: a binding exists once an attach's reply is delivered. */
class World {
  reader: ReaderState = READER_IDLE;
  bound = false;
  #ended = true;
  #next = 1;
  readonly #pending: number[] = [];

  act(random: Seeded): void {
    const choice = random.int(3);
    if (choice === 0)
      this.#apply(readerWanted(this.reader, random.bool(), this.#ended, this.#next));
    else if (choice === 1) {
      this.#ended = random.bool();
      this.#apply(reconcileReader(this.reader, this.#ended, this.#next));
    } else if (this.#pending.length > 0) this.#answer(random.int(this.#pending.length));
  }

  /** Chat is closed and every reply that is still on its way arrives. */
  finish(): void {
    this.#apply(readerWanted(this.reader, false, this.#ended, this.#next));
    while (this.#pending.length > 0) this.#answer(0);
  }

  #apply(step: ReaderStep): void {
    this.reader = step.reader;
    this.#next = step.next;
    for (const request of step.requests as readonly ReaderRequest[]) {
      if (request.kind === 'release-reader') this.bound = false;
      else this.#pending.push(request.token);
    }
  }

  #answer(index: number): void {
    const [token] = this.#pending.splice(index, 1);
    if (token === undefined) return;
    const reply: ReaderReply = {kind: 'attached'};
    this.bound = true;
    this.#apply(readerAttached(this.reader, token, reply, this.#next));
  }
}
