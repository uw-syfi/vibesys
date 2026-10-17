import {describe, expect, test} from 'bun:test';
import type {RunEvent} from '@vibesys/contracts';
import {event as fixtureEvent} from '@vibesys/contracts/testing';
import {
  type CoreRunStatus,
  type CoreState,
  initialCoreState,
  reduceEvent,
} from '@vibesys/run-state';
import {quotaBanner} from './quota-banner.js';

/** Every status core state can hold, so a new one is a compile error here. */
const RUN_STATUSES: readonly CoreRunStatus[] = [
  'connecting',
  'starting',
  'running',
  'pausing',
  'paused',
  'stopping',
  'stopped',
  'completed',
  'failed',
  'interrupted',
];

const PAUSED: RunEvent = fixtureEvent(1, 'quota_paused', {
  data: {
    kind: 'quota_paused',
    provider: 'claude',
    condition: 'rate_limited',
    detail: 'too many requests',
    resets_at: 3_600,
    resumes_at: 7_200,
    policy: 'wait',
    fallback_provider: 'codex',
    fallback_model: 'gpt-5.4',
  },
});

const PAUSED_NO_FALLBACK: RunEvent = fixtureEvent(1, 'quota_paused', {
  data: {
    kind: 'quota_paused',
    provider: 'claude',
    condition: 'quota_exhausted',
    detail: 'daily budget spent',
  },
});

const SWITCHED: RunEvent = fixtureEvent(2, 'provider_switched', {
  data: {
    kind: 'provider_switched',
    from_provider: 'claude',
    to_provider: 'codex',
    to_model: 'gpt-5.4',
    reason: 'policy',
    detail: 'too many requests',
  },
});

function runWith(status: CoreRunStatus, events: readonly RunEvent[] = []): CoreState {
  const folded = events.reduce(reduceEvent, initialCoreState());
  return {...folded, status};
}

describe('quotaBanner', () => {
  test('shows nothing before any quota event', () => {
    for (const status of RUN_STATUSES) {
      expect(quotaBanner(runWith(status))).toBeNull();
    }
  });

  test('shows the pause only while the run status is pausing or paused', () => {
    for (const status of RUN_STATUSES) {
      const banner = quotaBanner(runWith(status, [PAUSED]));
      if (status === 'pausing' || status === 'paused') {
        expect(banner?.kind).toBe('pause');
      } else {
        expect(banner).toBeNull();
      }
    }
  });

  test('names the provider, the condition, the reset time and the fallback', () => {
    const banner = quotaBanner(runWith('paused', [PAUSED]));
    expect(banner).toMatchObject({
      kind: 'pause',
      message: 'claude is rate limited. The run is paused until it resumes.',
      detail: 'too many requests Capacity returns at 1970-01-01 01:00 UTC.',
      wait: 'The run resumes by itself at 1970-01-01 02:00 UTC.',
      fallback: {provider: 'codex', model: 'gpt-5.4'},
    });
  });

  test('says the operator must act, and offers no fallback, when the run names neither', () => {
    const banner = quotaBanner(runWith('paused', [PAUSED_NO_FALLBACK]));
    expect(banner).toMatchObject({
      kind: 'pause',
      message: 'claude is out of quota. The run is paused until it resumes.',
      detail: 'daily budget spent',
      wait: 'The run stays paused until an operator resumes it.',
      fallback: null,
    });
  });

  test('shows the switch that settled the pause, once, whatever status follows it', () => {
    for (const status of RUN_STATUSES) {
      const banner = quotaBanner(runWith(status, [PAUSED, SWITCHED]));
      expect(banner).toMatchObject({
        kind: 'switch',
        message: 'Switched from claude to codex (gpt-5.4).',
        detail: 'the quota policy made the switch: too many requests',
      });
    }
  });

  test('retires the switch banner once a later quota event supersedes it, though the fact survives', () => {
    const resumed: RunEvent = fixtureEvent(3, 'quota_resumed', {
      data: {kind: 'quota_resumed', provider: 'codex'},
    });
    const run = runWith('running', [PAUSED, SWITCHED, resumed]);
    expect(quotaBanner(run)).toBeNull();
    // The switch fact itself survives (clients/libs/run-state's own regression for it); only the
    // banner built from it retires once something newer settles the quota state.
    expect(run.quota.lastSwitch).not.toBeNull();
  });

  test('an outage on another turn neither replaces nor settles the pause', () => {
    const outage = {provider: 'mock', condition: 'unavailable', detail: '503'} as const;
    const events: RunEvent[] = [
      PAUSED,
      fixtureEvent(2, 'quota_paused', {data: {kind: 'quota_paused', ...outage, policy: 'wait'}}),
      fixtureEvent(3, 'quota_resumed', {
        data: {
          kind: 'quota_resumed',
          provider: 'mock',
          condition: 'unavailable',
          waited_seconds: 60,
        },
      }),
      fixtureEvent(4, 'quota_abandoned', {
        data: {kind: 'quota_abandoned', ...outage, reason: 'the outage budget is spent'},
      }),
    ];
    expect(quotaBanner(runWith('paused', events))).toMatchObject({
      kind: 'pause',
      message: 'claude is rate limited. The run is paused until it resumes.',
    });
  });

  test('shows nothing once a plain resume settles the pause', () => {
    const resumed: RunEvent = fixtureEvent(2, 'quota_resumed', {
      data: {kind: 'quota_resumed', provider: 'claude'},
    });
    for (const status of RUN_STATUSES) {
      expect(quotaBanner(runWith(status, [PAUSED, resumed]))).toBeNull();
    }
  });
});
