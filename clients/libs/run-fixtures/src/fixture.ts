/**
 * The replayable run fixture: one run's recorded events, as the run protocol delivers them, plus
 * what a client harness needs to play them back (is the run still live, where does a live stream
 * start and where does it drop). A fixture is plain JSON, so a recording, a synthetic scenario and
 * a hand-written case are the same thing to every consumer.
 */
import {type RunEvent, validateRunEvent} from '@vibesys/contracts';

export const FIXTURE_FORMAT = 1;

/** The run's objective, as the snapshot's overview states it. */
export interface FixtureObjective {
  readonly text: string;
  readonly metric: string;
  readonly unit: string;
  readonly direction: 'max' | 'min';
}

/** How an ended run's record says it ended (the registry's end outcomes). */
export const END_OUTCOMES = [
  'adopted',
  'adopted-without-input-comparison',
  'kept-input',
  'no-winner',
  'deadline',
  'finish-refused',
  'adoption-failed',
  'failed',
  'stopped',
  'force_stopped',
] as const;
export type EndOutcome = (typeof END_OUTCOMES)[number];

export interface RunFixture {
  readonly format: typeof FIXTURE_FORMAT;
  /** Kebab-case, unique among fixtures: the file's name and the key a test asks for. */
  readonly name: string;
  /** What the fixture is for, in a sentence. */
  readonly description: string;
  readonly runId: string;
  /** How the run stands when a client opens it. */
  readonly state: 'live' | 'finished';
  readonly objective: FixtureObjective;
  /** The run's journal in order; sequences strictly increase from 1. */
  readonly events: readonly RunEvent[];
  /**
   * A live run: the first batch holds the events up to this sequence; the rest arrive afterwards
   * as live batches. Absent: the first batch holds every event.
   */
  readonly liveAfter?: number;
  /**
   * A live run: its stream drops after this sequence (at or after `liveAfter`), and the events
   * past it arrive once the client has resubscribed.
   */
  readonly dropAfter?: number;
  /** A finished run: how it ended. */
  readonly end?: {
    readonly outcome: EndOutcome;
    readonly reason: string;
  };
}

const KEYS: ReadonlySet<string> = new Set([
  'format',
  'name',
  'description',
  'runId',
  'state',
  'objective',
  'events',
  'liveAfter',
  'dropAfter',
  'end',
]);
const NAME = /^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$/;

function record(value: unknown, path: string): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new Error(`${path}: expected an object`);
  }
  return value as Record<string, unknown>;
}

function text(object: Record<string, unknown>, key: string, path: string): string {
  const value = object[key];
  if (typeof value !== 'string' || value === '') {
    throw new Error(`${path}.${key}: expected a non-empty string`);
  }
  return value;
}

function sequenceAt(
  object: Record<string, unknown>,
  key: string,
  last: number,
): number | undefined {
  const value = object[key];
  if (value === undefined) return undefined;
  if (typeof value !== 'number' || !Number.isInteger(value) || value < 1 || value > last) {
    throw new Error(`fixture.${key}: expected a sequence from 1 to ${last}, got ${String(value)}`);
  }
  return value;
}

function parseObjective(value: unknown): FixtureObjective {
  const object = record(value, 'fixture.objective');
  const direction = object['direction'];
  if (direction !== 'max' && direction !== 'min') {
    throw new Error('fixture.objective.direction: expected "max" or "min"');
  }
  return {
    text: text(object, 'text', 'fixture.objective'),
    metric: text(object, 'metric', 'fixture.objective'),
    unit: text(object, 'unit', 'fixture.objective'),
    direction,
  };
}

function parseEnd(value: unknown): NonNullable<RunFixture['end']> {
  const object = record(value, 'fixture.end');
  const outcome = object['outcome'];
  const known = END_OUTCOMES.find(each => each === outcome);
  if (known === undefined) {
    throw new Error(`fixture.end.outcome: expected one of ${END_OUTCOMES.join(', ')}`);
  }
  return {outcome: known, reason: text(object, 'reason', 'fixture.end')};
}

function parseEvents(value: unknown): RunEvent[] {
  if (!Array.isArray(value) || value.length === 0) {
    throw new Error('fixture.events: expected a non-empty array');
  }
  let previous = 0;
  return value.map((item, index) => {
    const event = validateRunEvent(item);
    const sequence = event.sequence;
    if (sequence === undefined || sequence <= previous) {
      throw new Error(
        `fixture.events[${index}]: sequence ${String(sequence)} does not follow ${previous}`,
      );
    }
    previous = sequence;
    return event;
  });
}

/** A live run may stream and drop but not end; a finished run ends and does not stream. */
function checkStanding(
  object: Record<string, unknown>,
  state: 'live' | 'finished',
  liveAfter: number | undefined,
  dropAfter: number | undefined,
): void {
  const streams = liveAfter !== undefined || dropAfter !== undefined;
  const ends = object['end'] !== undefined;
  if (state === 'finished' && streams) {
    throw new Error('fixture: a finished run has no live stream (liveAfter, dropAfter)');
  }
  if (dropAfter !== undefined && dropAfter < (liveAfter ?? 1)) {
    throw new Error('fixture.dropAfter: the stream drops before it starts');
  }
  if (state === 'live' && ends) throw new Error('fixture.end: a live run has not ended');
  if (state === 'finished' && !ends) {
    throw new Error('fixture.end: a finished run says how it ended');
  }
}

/**
 * Validate parsed JSON as a fixture, naming the offending key or path: an unknown key, an event
 * the run protocol rejects, a sequence that does not increase, a live marker outside the run.
 */
export function parseFixture(value: unknown): RunFixture {
  const object = record(value, 'fixture');
  for (const key of Object.keys(object)) {
    if (!KEYS.has(key)) throw new Error(`fixture.${key}: unknown key`);
  }
  if (object['format'] !== FIXTURE_FORMAT) {
    throw new Error(`fixture.format: expected ${FIXTURE_FORMAT}`);
  }
  const name = text(object, 'name', 'fixture');
  if (!NAME.test(name)) throw new Error(`fixture.name: ${name} is not kebab case`);
  const state = object['state'];
  if (state !== 'live' && state !== 'finished') {
    throw new Error('fixture.state: expected "live" or "finished"');
  }
  const events = parseEvents(object['events']);
  const last = events.at(-1)?.sequence ?? 0;
  const liveAfter = sequenceAt(object, 'liveAfter', last);
  const dropAfter = sequenceAt(object, 'dropAfter', last);
  checkStanding(object, state, liveAfter, dropAfter);
  return {
    format: FIXTURE_FORMAT,
    name,
    description: text(object, 'description', 'fixture'),
    runId: text(object, 'runId', 'fixture'),
    state,
    objective: parseObjective(object['objective']),
    events,
    ...(liveAfter === undefined ? {} : {liveAfter}),
    ...(dropAfter === undefined ? {} : {dropAfter}),
    ...(object['end'] === undefined ? {} : {end: parseEnd(object['end'])}),
  };
}

/** The events a client's first batch holds, and those that arrive afterwards. */
export function splitLive(fixture: RunFixture): {
  readonly first: readonly RunEvent[];
  readonly beforeDrop: readonly RunEvent[];
  readonly afterDrop: readonly RunEvent[];
} {
  const sequence = (event: RunEvent): number => event.sequence ?? 0;
  const liveAfter = fixture.liveAfter ?? fixture.dropAfter ?? Number.POSITIVE_INFINITY;
  const dropAfter = fixture.dropAfter ?? Number.POSITIVE_INFINITY;
  return {
    first: fixture.events.filter(event => sequence(event) <= liveAfter),
    beforeDrop: fixture.events.filter(
      event => sequence(event) > liveAfter && sequence(event) <= dropAfter,
    ),
    afterDrop: fixture.events.filter(event => sequence(event) > dropAfter),
  };
}
