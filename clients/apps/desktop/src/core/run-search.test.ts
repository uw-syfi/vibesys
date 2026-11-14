import {fromBinary} from '@bufbuild/protobuf';
import {describe, expect, it} from '@effect/vitest';
import {SearchRunsRequestSchema} from '@vibesys/orchestrator-proto';
import * as Effect from 'effect/Effect';
import * as Fiber from 'effect/Fiber';
import * as TestClock from 'effect/testing/TestClock';
import {
  FakeDaemon,
  generateScenario,
  type HostHarness,
  hostHarness,
  methodPathOf,
} from '../testing/orchestrator/index.js';
import type {Recording} from '../testing/transport/index.js';
import {RUN_SEARCH_DEBOUNCE} from './run-search.js';

const scenario = generateScenario({seed: 5, subagents: 2, live: false});

const calls = (h: HostHarness) =>
  h.fake.calls().filter(call => call.target.method === methodPathOf('SearchRuns'));

const pauseForSearch = Effect.gen(function* () {
  yield* TestClock.adjust(RUN_SEARCH_DEBOUNCE);
  yield* Effect.yieldNow;
});

describe('whole-history run search', () => {
  it.effect('asks with the trimmed query and carries the authoritative run-summary pair', () =>
    Effect.gen(function* () {
      const h = yield* hostHarness(new FakeDaemon(scenario));
      const fiber = yield* Effect.forkChild(h.session.runSearch.search('  Whisper  '));
      expect(yield* h.read(h.session.runSearch.state)).toEqual({
        _tag: 'Searching',
        query: 'Whisper',
      });
      yield* pauseForSearch;
      yield* Fiber.join(fiber);

      const state = yield* h.read(h.session.runSearch.state);
      expect(state._tag).toBe('Found');
      if (state._tag !== 'Found') return;
      expect(state.query).toBe('Whisper');
      expect(state.response.sequence).toBeGreaterThan(0n);
      expect(state.response.results).toHaveLength(1);
      expect(state.response.results[0]?.run?.id).toBe(scenario.runId);
      expect(state.response.results[0]?.summary?.runId).toBe(scenario.runId);

      const [call] = calls(h);
      expect(call).toBeDefined();
      if (call !== undefined)
        expect(fromBinary(SearchRunsRequestSchema, call.target.request).query).toBe('Whisper');
    }),
  );

  it.effect('debounces prefixes and lets only the latest request publish an answer', () =>
    Effect.gen(function* () {
      const delayedFirst = (base: Recording): Recording => ({
        respond: (target, nth) =>
          target.method === methodPathOf('SearchRuns') && nth === 0
            ? [{sleep: '5 seconds'}, ...base.respond(target, nth)]
            : base.respond(target, nth),
      });
      const h = yield* hostHarness(new FakeDaemon(scenario), {wrap: delayedFirst});

      const first = yield* Effect.forkChild(h.session.runSearch.search('Whisper'));
      yield* pauseForSearch;
      expect(calls(h)).toHaveLength(1);

      const latest = yield* Effect.forkChild(h.session.runSearch.search(scenario.runId));
      yield* pauseForSearch;
      yield* Fiber.join(latest);
      expect(yield* h.read(h.session.runSearch.state)).toMatchObject({
        _tag: 'Found',
        query: scenario.runId,
      });

      yield* TestClock.adjust('5 seconds');
      yield* Fiber.join(first);
      expect(yield* h.read(h.session.runSearch.state)).toMatchObject({
        _tag: 'Found',
        query: scenario.runId,
      });
      expect(calls(h)).toHaveLength(2);
    }),
  );

  it.effect('does not ask about superseded prefixes that never reached the debounce', () =>
    Effect.gen(function* () {
      const h = yield* hostHarness(new FakeDaemon(scenario));
      const fibers = [];
      for (const query of ['W', 'Wh', 'Whi', 'Whis', 'Whisper'])
        fibers.push(yield* Effect.forkChild(h.session.runSearch.search(query)));
      yield* pauseForSearch;
      for (const fiber of fibers) yield* Fiber.join(fiber);
      expect(calls(h)).toHaveLength(1);
      expect(yield* h.read(h.session.runSearch.state)).toMatchObject({
        _tag: 'Found',
        query: 'Whisper',
      });
    }),
  );

  it.effect('represents an older host failure, then clears it without another call', () =>
    Effect.gen(function* () {
      const h = yield* hostHarness(new FakeDaemon(scenario), {searchRuns: 'unserved'});
      const search = yield* Effect.forkChild(h.session.runSearch.search('Whisper'));
      yield* pauseForSearch;
      yield* Fiber.join(search);
      expect(yield* h.read(h.session.runSearch.state)).toMatchObject({
        _tag: 'Failed',
        query: 'Whisper',
        fault: {kind: 'Unimplemented'},
      });
      expect(calls(h)).toHaveLength(1);

      yield* h.session.runSearch.search('   ');
      expect(yield* h.read(h.session.runSearch.state)).toEqual({_tag: 'Idle'});
      expect(calls(h)).toHaveLength(1);
    }),
  );

  it.effect('uses the proto byte bound locally for multibyte pasted queries', () =>
    Effect.gen(function* () {
      const h = yield* hostHarness(new FakeDaemon(scenario));
      const atLimit = 'é'.repeat(128);
      const overLimit = 'é'.repeat(129);
      expect(new TextEncoder().encode(atLimit)).toHaveLength(256);
      expect(new TextEncoder().encode(overLimit)).toHaveLength(258);

      const accepted = yield* Effect.forkChild(h.session.runSearch.search(atLimit));
      yield* pauseForSearch;
      yield* Fiber.join(accepted);
      expect(calls(h)).toHaveLength(1);

      const rejected = yield* Effect.forkChild(h.session.runSearch.search(overLimit));
      yield* pauseForSearch;
      yield* Fiber.join(rejected);
      expect(yield* h.read(h.session.runSearch.state)).toEqual({
        _tag: 'Invalid',
        query: overLimit,
        reason: 'Use a search query of at most 256 UTF-8 bytes.',
      });
      // Request validation happens before transport, so the host never sees the oversized paste.
      expect(calls(h)).toHaveLength(1);
    }),
  );

  it.effect('starts a preserved query when the run snapshot later says it omitted history', () =>
    Effect.gen(function* () {
      const daemon = new FakeDaemon(scenario);
      daemon.runsLeftOut = 3;
      const h = yield* hostHarness(daemon);
      const stopSearch = h.mount(h.session.runSearch.watch('Whisper'));
      yield* h.elapse(0);
      expect(calls(h)).toHaveLength(0);

      const stopRuns = h.mount(h.session.runs.watch);
      yield* h.elapse(1);
      expect(calls(h)).toHaveLength(1);
      expect(yield* h.read(h.session.runSearch.state)).toMatchObject({
        _tag: 'Found',
        query: 'Whisper',
      });

      stopSearch();
      const stopReopened = h.mount(h.session.runSearch.watch('Whisper'));
      yield* h.elapse(0);
      expect(calls(h)).toHaveLength(1);
      stopReopened();
      stopRuns();
    }),
  );
});
