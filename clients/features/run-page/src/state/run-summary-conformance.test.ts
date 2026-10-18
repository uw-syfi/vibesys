import {describe, expect, test} from 'bun:test';
import {readdirSync, readFileSync} from 'node:fs';
import {resolve} from 'node:path';
import type {RunEvent, RunOverview, Span} from '@vibesys/contracts';
import {event} from '@vibesys/contracts/testing';
import {initialCoreState, reduceEventBatch} from '@vibesys/run-state';
import {CandidateFold, candidateTree} from '@vibesys/run-state/candidates';
import {foldOverview} from '@vibesys/run-state/overview';
import {EMPTY_TRACE, foldTrace, traceSpans} from '@vibesys/run-state/trace';
import {implementerOutcomes} from './idea-outcomes.js';

/**
 * The run summary the host helper's `runs.summary` returns (`RunsSummaryReply`) is the client's own
 * fold of the journal. The fixtures under `tests/conformance/fixtures/run-summary` hold a journal
 * and the summary the helper folds from it (`tests/server/test_run_summary_fixtures.py`); this
 * test folds the same journal with the client's code, so the two folds of one definition cannot
 * drift apart. It also shows what the run level needs from a summary: the idea rows (candidates
 * and each implementer's reply) of a run seeded from the summary are those of the whole journal.
 */

const FIXTURES = resolve(import.meta.dir, '../../../../../tests/conformance/fixtures/run-summary');

interface ClientReply {
  readonly span_id: string;
  readonly kind: string;
  readonly json_schema: Readonly<Record<string, unknown>>;
  readonly value: Readonly<Record<string, unknown>>;
}

interface Fixture {
  readonly events: readonly RunEvent[];
  readonly summary: {
    readonly through_sequence: number;
    readonly event_count: number;
    readonly spans: readonly Span[];
    readonly overview: RunOverview | null;
    readonly replies: readonly ClientReply[];
  };
}

const fixtures: Array<{name: string; fixture: Fixture}> = readdirSync(FIXTURES)
  .filter(name => name.endsWith('.json'))
  .sort()
  .map(name => ({
    name,
    fixture: JSON.parse(readFileSync(resolve(FIXTURES, name), 'utf8')) as Fixture,
  }));

/** The last reply of each turn span in `events`, as the transcript fold holds it, by span id. */
function repliesOf(events: readonly RunEvent[]): ClientReply[] {
  const core = reduceEventBatch(initialCoreState(), events);
  const latest = new Map<string, ClientReply>();
  for (const entry of core.transcript) {
    if (entry.kind !== 'reply' || entry.spanId === undefined || entry.reply === undefined) continue;
    latest.set(entry.spanId, {
      span_id: entry.spanId,
      kind: entry.reply.kind,
      json_schema: entry.reply.json_schema,
      value: entry.reply.value,
    });
  }
  return [...latest.values()];
}

const bySpan = (replies: readonly ClientReply[]): ClientReply[] =>
  [...replies].sort((a, b) => a.span_id.localeCompare(b.span_id));

/** The events a run seeded from a summary holds for its replies: one finished turn per reply. */
function replyEvents(replies: readonly ClientReply[]): RunEvent[] {
  return replies.map((reply, index) =>
    event(index + 1, 'agent_execution_finished', {
      span_id: reply.span_id,
      data: {
        kind: 'agent_execution_finished',
        result: '{}',
        reply: {kind: reply.kind, json_schema: reply.json_schema, value: reply.value},
      },
    } as never),
  );
}

describe('the run summary equals the client fold of the same journal', () => {
  for (const {name, fixture} of fixtures) {
    const {events, summary} = fixture;

    test(`${name}: the spans, the cursor and the overview`, () => {
      const trace = foldTrace(EMPTY_TRACE, events);
      expect(traceSpans(trace)).toEqual(summary.spans);
      expect(trace.sequence).toBe(summary.through_sequence);
      expect(foldOverview(null, events)).toEqual(summary.overview);
    });

    test(`${name}: the count is the events a client does not skip as already folded`, () => {
      let last = 0;
      let folded = 0;
      for (const item of events) {
        if ((item.sequence ?? 0) <= last) continue;
        last = item.sequence ?? 0;
        folded += 1;
      }
      expect(folded).toBe(summary.event_count);
      expect(events.length).toBeGreaterThan(folded); // the fixture holds a repeated sequence
    });

    test(`${name}: each turn's last reply`, () => {
      expect(bySpan(repliesOf(events))).toEqual(bySpan(summary.replies));
    });

    test(`${name}: the idea rows of a run seeded from the summary are the whole journal's`, () => {
      const whole = new CandidateFold();
      whole.apply(events);
      const seeded = candidateTree(summary.spans);
      expect(seeded).toEqual(whole.tree());
      expect(seeded.candidates.length).toBeGreaterThan(1);

      const fromJournal = implementerOutcomes(
        reduceEventBatch(initialCoreState(), events),
        whole.tree(),
      );
      const fromSummary = implementerOutcomes(
        reduceEventBatch(initialCoreState(), replyEvents(summary.replies)),
        seeded,
      );
      expect(fromSummary).toEqual(fromJournal);
      expect(fromSummary.size).toBe(seeded.candidates.length);
    });
  }

  test('the corpus holds a fixture', () => {
    expect(fixtures.length).toBeGreaterThan(0);
  });
});
