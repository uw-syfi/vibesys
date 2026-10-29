/**
 * The recorder: turns a run's recorded events (its `run-events.jsonl` journal, or the events of
 * `runs.read_events` pages) into a fixture, with every host name, absolute path and user name
 * removed. Pure over text and values; the command that reads and writes files is a thin shell
 * around it (`clients/tools/e2e/record-run-fixture.mts`).
 */
import {FIXTURE_FORMAT, type FixtureObjective, parseFixture, type RunFixture} from './fixture.js';
import {findLeaks, type ScrubPolicy, scrubValue} from './scrub.js';

export interface RecordRequest {
  /** The fixture's name (kebab case) and what it is for. */
  readonly name: string;
  readonly description: string;
  /** The journal's text: one run event per line (JSON Lines), or one JSON array of events. */
  readonly journal: string;
  /** How the run stands for the replay. */
  readonly state: 'live' | 'finished';
  readonly objective: FixtureObjective;
  /** A finished run's end. */
  readonly end?: RunFixture['end'];
  /** The recording machine's user and host names, which no pattern can know. */
  readonly policy: ScrubPolicy;
}

/** The events a journal's text holds, in order. */
export function journalEvents(journal: string): unknown[] {
  const text = journal.trim();
  if (text === '') throw new Error('the journal holds no events');
  if (text.startsWith('[')) {
    const parsed: unknown = JSON.parse(text);
    if (!Array.isArray(parsed)) throw new Error('the journal is not an array of events');
    return parsed;
  }
  return text.split('\n').flatMap((line, index) => {
    if (line.trim() === '') return [];
    try {
      return [JSON.parse(line) as unknown];
    } catch (error) {
      throw new Error(`journal line ${index + 1} is not JSON: ${String(error)}`);
    }
  });
}

/**
 * The scrubbed fixture of a journal. It refuses a journal whose events the run protocol rejects,
 * and a scrubbed result that still holds a leak (which would be a hole in the scrubber).
 */
export function recordFixture(request: RecordRequest): RunFixture {
  const events = scrubValue(journalEvents(request.journal), request.policy);
  const first = events[0] as {run_id?: unknown} | undefined;
  const runId = typeof first?.run_id === 'string' ? first.run_id : request.name;
  const fixture = parseFixture({
    format: FIXTURE_FORMAT,
    name: request.name,
    description: scrubValue(request.description, request.policy),
    runId,
    state: request.state,
    objective: scrubValue(request.objective, request.policy),
    events,
    ...(request.end === undefined ? {} : {end: scrubValue(request.end, request.policy)}),
  });
  const leaks = findLeaks(fixture, request.policy);
  if (leaks.length > 0) {
    const shown = leaks.slice(0, 5).map(leak => `${leak.kind} at ${leak.path}`);
    throw new Error(
      `the scrubbed fixture still holds ${leaks.length} leak(s): ${shown.join('; ')}`,
    );
  }
  return fixture;
}
