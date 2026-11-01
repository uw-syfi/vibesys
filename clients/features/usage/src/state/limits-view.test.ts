import {describe, expect, test} from 'bun:test';
import type {
  HostKey,
  UsageAccountLimitsEntry,
  UsageLimitsStatusName,
  UsageLimitWindowEntry,
  UsageProviderLimitsEntry,
} from '@vibesys/hosts';
import fc from 'fast-check';
import {ago, type HostLimits, limitsView, span} from './limits-view.js';

const NOW = Date.UTC(2026, 9, 31, 12, 0, 0);
const HOUR = 3_600_000;
const HOSTS: readonly HostKey[] = ['local', 'ssh:host-a', 'ssh:host-b'];

const window = (
  id: string,
  used: number,
  resetsIn: number | null = 3 * HOUR,
): UsageLimitWindowEntry => ({
  id,
  kind: 'weekly',
  label: id,
  usedFraction: used,
  resetsAt: resetsIn === null ? null : NOW + resetsIn,
  windowMinutes: 10_080,
});

const reading = (
  provider: string,
  account: string | null,
  windows: readonly UsageLimitWindowEntry[],
  readAt: number,
  credits: number | null = null,
): UsageAccountLimitsEntry => ({
  provider,
  account,
  plan: 'pro',
  windows,
  resetCreditsAvailable: credits,
  resetCredits: [],
  usageUrl: null,
  readAt,
});

const entry = (
  provider: string,
  limits: UsageAccountLimitsEntry | null,
  status: UsageLimitsStatusName = limits === null ? 'unavailable' : 'live',
  unavailable: UsageProviderLimitsEntry['unavailable'] = null,
): UsageProviderLimitsEntry => ({provider, status, limits, unavailable});

const answer = (host: HostKey, ...providers: UsageProviderLimitsEntry[]): HostLimits => ({
  host,
  limits: {providers, readAt: NOW},
});

describe('a bar', () => {
  test('property: the share left is a whole percent between 0 and 100 whatever the host reports', () => {
    fc.assert(
      fc.property(
        fc.oneof(fc.double({min: -5, max: 1e6, noNaN: true}), fc.constant(Number.NaN)),
        used => {
          const [card] = limitsView(
            [answer('local', entry('codex', reading('codex', 'a', [window('w', used)], NOW)))],
            NOW,
          ).cards;
          const bar = card?.bars[0];
          expect(bar).toBeDefined();
          expect(Number.isInteger(bar?.left)).toBe(true);
          expect(bar?.left).toBeGreaterThanOrEqual(0);
          expect(bar?.left).toBeLessThanOrEqual(100);
          expect(bar?.leftText).toBe(`${bar?.left}% left`);
          expect(bar?.used).toBeGreaterThanOrEqual(0);
          expect(bar?.used).toBeLessThanOrEqual(1);
          expect(bar?.exhausted).toBe(bar?.left === 0);
        },
      ),
    );
  });

  test('a span is rounded up and an age down, in the coarsest whole unit', () => {
    expect(span(0)).toBe('1 min');
    expect(span(61_000)).toBe('2 min');
    expect(span(HOUR)).toBe('1 h');
    expect(span(HOUR + 1)).toBe('2 h');
    expect(span(30 * HOUR)).toBe('2 d');
    expect(ago(59_999)).toBe('0 min');
    expect(ago(2 * HOUR + 59_000)).toBe('2 h');
    expect(ago(-5)).toBe('0 min');
  });

  test('property: a span never says a window refills before it does', () => {
    fc.assert(
      fc.property(fc.integer({min: 1, max: 40 * 24 * HOUR}), ms => {
        const [value = '0', unit = ''] = span(ms).split(' ');
        const scale = {min: 60_000, h: HOUR, d: 24 * HOUR}[unit as 'min' | 'h' | 'd'];
        expect(Number(value) * scale).toBeGreaterThanOrEqual(ms);
        expect((Number(value) - 1) * scale).toBeLessThan(ms);
      }),
    );
  });
});

describe('the cards', () => {
  test('one account on several machines is one card with each window from the machine that read it last', () => {
    const view = limitsView(
      [
        answer(
          'local',
          entry(
            'claude',
            reading('claude', 'me', [window('a', 0.1), window('b', 0.9)], NOW - HOUR),
          ),
        ),
        answer('ssh:host-a', entry('claude', reading('claude', 'me', [window('a', 0.5)], NOW))),
      ],
      NOW,
    );
    expect(view.cards).toHaveLength(1);
    const card = view.cards[0];
    expect(card?.hosts).toEqual(['local', 'ssh:host-a']);
    expect(Object.fromEntries(card?.bars.map(bar => [bar.id, bar.left]) ?? [])).toEqual({
      a: 50,
      b: 10,
    });
  });

  test('two accounts of one provider are two cards, and a machine with no account is its own', () => {
    const view = limitsView(
      [
        answer('local', entry('codex', reading('codex', 'one', [window('w', 0.1)], NOW))),
        answer('ssh:host-a', entry('codex', reading('codex', 'two', [window('w', 0.2)], NOW))),
        answer('ssh:host-b', entry('codex', reading('codex', null, [window('w', 0.3)], NOW))),
      ],
      NOW,
    );
    expect(view.cards.map(card => card.hosts)).toEqual([['local'], ['ssh:host-a'], ['ssh:host-b']]);
  });

  test('a machine with no reading never hides another machine’s', () => {
    const view = limitsView(
      [
        answer('local', entry('claude', null, 'unavailable', 'probe-failed')),
        answer('ssh:host-a', entry('claude', reading('claude', 'me', [window('w', 0.4)], NOW))),
      ],
      NOW,
    );
    expect(view.cards.map(card => [card.provider, card.status, card.bars.length])).toEqual([
      ['claude', null, 1],
    ]);
  });

  test('a provider no machine has a reading of says why in a word', () => {
    const words = (reason: 'unsupported' | 'api-key' | 'probe-failed') =>
      limitsView([answer('local', entry('claude', null, 'unavailable', reason))], NOW).cards[0]
        ?.status;
    expect(words('unsupported')).toBe('Unsupported');
    expect(words('api-key')).toBe('API key');
    expect(words('probe-failed')).toBe('Unavailable');
  });

  test('a stale reading says so with its age, and a current one says nothing', () => {
    const stale = limitsView(
      [
        answer(
          'local',
          entry(
            'codex',
            reading('codex', 'me', [window('w', 0.2)], NOW - 3 * HOUR),
            'stale',
            'probe-failed',
          ),
        ),
      ],
      NOW,
    ).cards[0];
    expect([stale?.status, stale?.age]).toEqual(['Stale', '3 h']);
    const fresh = limitsView(
      [answer('local', entry('codex', reading('codex', 'me', [window('w', 0.2)], NOW), 'stored'))],
      NOW,
    ).cards[0];
    expect([fresh?.status, fresh?.age]).toEqual([null, null]);
  });

  test('the credits are the newest reading’s, and nothing answered is empty', () => {
    const view = limitsView(
      [
        answer('local', entry('codex', reading('codex', 'me', [window('w', 0.2)], NOW - HOUR, 2))),
        answer('ssh:host-a', entry('codex', reading('codex', 'me', [window('w', 0.2)], NOW, 1))),
      ],
      NOW,
    );
    expect(view.cards[0]?.credits).toBe(1);
    expect(limitsView([], NOW)).toEqual({cards: [], empty: true});
  });

  test('property: the answers in any order give the same cards', () => {
    const entries = fc.constantFrom('claude', 'codex').chain(provider =>
      fc.record({
        provider: fc.constant(provider),
        account: fc.constantFrom('a', 'b', null),
        ids: fc.subarray(['x', 'y', 'z'], {minLength: 1}),
        used: fc.double({min: 0, max: 1, noNaN: true}),
        age: fc.integer({min: 0, max: 5}),
      }),
    );
    fc.assert(
      fc.property(
        fc.array(entries, {minLength: 1, maxLength: 6}),
        fc.integer(),
        (made, shuffle) => {
          const replies = made.map((each, at) =>
            answer(
              HOSTS[at % HOSTS.length] ?? 'local',
              entry(
                each.provider,
                reading(
                  each.provider,
                  each.account,
                  each.ids.map(id => window(id, each.used)),
                  NOW - each.age * HOUR - at,
                ),
              ),
            ),
          );
          const rotated = [
            ...replies.slice(shuffle % replies.length),
            ...replies.slice(0, shuffle % replies.length),
          ];
          const shape = (cards: ReturnType<typeof limitsView>['cards']) =>
            cards
              .map(card => ({
                key: card.key,
                bars: Object.fromEntries(card.bars.map(bar => [bar.id, bar.left])),
                hosts: [...card.hosts].sort(),
              }))
              .sort((left, right) => left.key.localeCompare(right.key));
          const a = shape(limitsView(replies, NOW).cards);
          const b = shape(limitsView(rotated, NOW).cards);
          expect(b).toEqual(a);
        },
      ),
    );
  });
});
