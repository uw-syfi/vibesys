import {Refusal, Rejection} from '@vibesys/orchestrator-proto';
import {describe, expect, it} from '@effect/vitest';
import fc from 'fast-check';
import {renderToStaticMarkup} from 'react-dom/server';
import {
  type Fault,
  FAULT_KINDS,
  type FeedStatus,
  type HostConnectionState,
  REFUSAL_CAUSES,
} from '../../state/index.js';
import {causeHint, causeWord} from '../refusal-words.js';
import {type ConnectionNote, ConnectionStatus} from './connection-status.js';
import {faultAdvice, faultText, faultWord, refusalWord, rejectionWord} from './words.js';

const fault = fc.record({
  kind: fc.constantFrom(...FAULT_KINDS),
  detail: fc.string({maxLength: 40}),
  retryable: fc.boolean(),
}) satisfies fc.Arbitrary<Fault>;

const connection: fc.Arbitrary<HostConnectionState> = fc.oneof(
  fc.constant({_tag: 'Idle'} as const),
  fc.integer({min: 1, max: 9}).map(attempt => ({_tag: 'Connecting', attempt}) as const),
  fc.constant({_tag: 'Ready', epoch: 1, daemon: undefined as never} as const),
  fc
    .tuple(fault, fc.integer({min: 1, max: 9}))
    .map(([each, attempt]) => ({_tag: 'Waiting', attempt, fault: each, retryAtMs: 5}) as const),
  fc.constantFrom('client-too-old', 'daemon-too-old').map(
    side => ({_tag: 'Incompatible', side}) as const,
  ),
  fault.map(each => ({_tag: 'Unusable', fault: each}) as const),
);

const feed: fc.Arbitrary<FeedStatus> = fc.oneof(
  fc.constant({_tag: 'Opening'} as const),
  fc.constant({_tag: 'Live'} as const),
  fc.constantFrom('gap', 'fell-behind', 'reconnect').map(
    why => ({_tag: 'Restarting', why}) as const,
  ),
  fault.map(each => ({_tag: 'Failed', fault: each}) as const),
);

const note = fc.record({
  key: fc.constant('runs'),
  what: fc.constant('the runs on host-b'),
  connection,
  feed,
  hasData: fc.boolean(),
  staleForMs: fc.option(fc.integer({min: 0, max: 10_000_000}), {nil: null}),
  tryAgain: fc.constant(() => undefined),
}) satisfies fc.Arbitrary<ConnectionNote>;

/** What a person reads: the markup's text, without the attributes that are for a hover or a reader. */
const visibleText = (html: string): string => html.replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ');

describe('the connection status', () => {
  it('is never a banner or a sentence, whatever state the connection and its feed are in', () => {
    fc.assert(
      fc.property(fc.array(note, {maxLength: 3}), notes => {
        const html = renderToStaticMarkup(<ConnectionStatus notes={notes} />);
        expect(html).not.toMatch(/role="alert"|vs-banner|<p[ >]/);
        // Chips, one Retry at most, and nothing else: no line of the screen ends in a full stop.
        expect(visibleText(html)).not.toMatch(/[.!?]/);
        expect(html.match(/>Retry</g)?.length ?? 0).toBeLessThanOrEqual(1);
      }),
      {numRuns: 300},
    );
  });

  it('keeps every detail on hover and focus, where a person can still read it', () => {
    fc.assert(
      fc.property(fault, fc.integer({min: 1, max: 9}), (each, attempt) => {
        const html = renderToStaticMarkup(
          <ConnectionStatus
            notes={[
              {
                key: 'runs',
                what: 'the runs on host-b',
                connection: {_tag: 'Waiting', attempt, fault: each, retryAtMs: 5},
                feed: {_tag: 'Live'},
                hasData: true,
                staleForMs: null,
                tryAgain: () => undefined,
              },
            ]}
          />,
        );
        expect(html).toContain('aria-description="');
        expect(html).toContain(faultWord(each.kind));
      }),
    );
  });
});

/** The values of a numeric enum, without the reverse names a TypeScript enum also lists. */
const numbers = <T extends number>(each: Record<string, string | T>): T[] =>
  Object.values(each).filter((value): value is T => typeof value === 'number');

describe('the words of a refusal', () => {
  const distinct = (words: readonly string[]): void => {
    expect(new Set(words).size).toBe(words.length);
    for (const word of words) {
      // A label: a few words, no sentence.
      expect(word).not.toMatch(/[.!?]/);
      expect(word.split(' ').length).toBeLessThanOrEqual(5);
    }
  };

  it('are a distinct label for each cause, kind, refusal and rejection, with the sentence kept as a hint', () => {
    distinct(REFUSAL_CAUSES.map(causeWord));
    distinct(FAULT_KINDS.map(faultWord));
    distinct(numbers(Refusal).map(refusalWord));
    distinct(numbers(Rejection).map(rejectionWord));
    for (const cause of REFUSAL_CAUSES) expect(causeHint(cause)).toMatch(/\.$/);
    for (const kind of FAULT_KINDS) expect(faultAdvice(kind)).toMatch(/\.$/);
    expect(faultText({kind: 'Refused', detail: '', retryable: false, cause: REFUSAL_CAUSES[0]})).toBe(
      causeWord(REFUSAL_CAUSES[0]),
    );
  });
});
