/** What a host's `UsageLimits` response says, in the app's terms. */
import {type Timestamp, timestampMs} from '@bufbuild/protobuf/wkt';
import type {
  UsageAccountLimitsEntry,
  UsageLimitKindName,
  UsageLimitsEntry,
  UsageLimitsStatusName,
  UsageLimitsUnavailableName,
  UsageProviderLimitsEntry,
} from '@vibesys/hosts';
import {
  type UsageAccountLimits,
  UsageLimitKind,
  type UsageLimitsResponse,
  UsageLimitsStatus,
  UsageLimitsUnavailable,
} from '@vibesys/proto';

const KINDS: ReadonlyMap<UsageLimitKind, UsageLimitKindName> = new Map([
  [UsageLimitKind.SESSION, 'session'],
  [UsageLimitKind.WEEKLY, 'weekly'],
  [UsageLimitKind.MONTHLY, 'monthly'],
  [UsageLimitKind.OTHER, 'other'],
]);

const STATUSES: ReadonlyMap<UsageLimitsStatus, UsageLimitsStatusName> = new Map([
  [UsageLimitsStatus.LIVE, 'live'],
  [UsageLimitsStatus.STORED, 'stored'],
  [UsageLimitsStatus.STALE, 'stale'],
  [UsageLimitsStatus.UNAVAILABLE, 'unavailable'],
]);

const REASONS: ReadonlyMap<UsageLimitsUnavailable, UsageLimitsUnavailableName> = new Map([
  [UsageLimitsUnavailable.UNSUPPORTED, 'unsupported'],
  [UsageLimitsUnavailable.API_KEY, 'api-key'],
  [UsageLimitsUnavailable.PROBE_FAILED, 'probe-failed'],
]);

function named<K, V>(table: ReadonlyMap<K, V>, key: K, what: string): V {
  const found = table.get(key);
  if (found === undefined) throw new Error(`the host sent an unknown ${what}: ${String(key)}`);
  return found;
}

const ms = (stamp: Timestamp | undefined): number | null =>
  stamp === undefined ? null : timestampMs(stamp);

function accountEntry(account: UsageAccountLimits): UsageAccountLimitsEntry {
  const readAt = ms(account.readAt);
  if (readAt === null) throw new Error('the host sent a limits reading without its time');
  return {
    provider: account.provider,
    account: account.account ?? null,
    plan: account.plan ?? null,
    windows: account.windows.map(window => ({
      id: window.id,
      kind: named(KINDS, window.kind, 'limit kind'),
      label: window.label,
      usedFraction: window.usedFraction,
      resetsAt: ms(window.resetsAt),
      windowMinutes: window.windowMinutes ?? null,
    })),
    resetCreditsAvailable: account.resetCreditsAvailable ?? null,
    resetCredits: account.resetCredits.map(credit => ({
      id: credit.id,
      expiresAt: ms(credit.expiresAt),
      title: credit.title ?? null,
    })),
    usageUrl: account.usageUrl ?? null,
    readAt,
  };
}

/** What a usage limits response says: each provider's reading or why it has none. */
export function usageLimitsEntry(response: UsageLimitsResponse): UsageLimitsEntry {
  return {
    providers: response.providers.map(
      (each): UsageProviderLimitsEntry => ({
        provider: each.provider,
        status: named(STATUSES, each.status, 'limits status'),
        limits: each.limits === undefined ? null : accountEntry(each.limits),
        unavailable:
          each.unavailable === undefined ? null : named(REASONS, each.unavailable, 'reason'),
      }),
    ),
    readAt: ms(response.readAt) ?? 0,
  };
}
