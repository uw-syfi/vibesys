/**
 * What the Limits tab shows, folded from what each machine answered: one card per provider
 * account, with a bar per window.
 *
 * An account seen from several machines is one card: machines answer with a digest of the account
 * (never its email), equal for one account, and the card takes each window from the machine that
 * read it last. A machine that has no reading (it lacks the CLI, or its read failed with none
 * stored) never hides another's: a provider is shown as unavailable only when no machine has a
 * reading of it. A reading older than the moment it was asked for is said to be stale, with its
 * age. All words are status words and numbers; the card holds the figures the screen prints.
 */
import type {
  HostKey,
  UsageAccountLimitsEntry,
  UsageLimitKindName,
  UsageLimitsEntry,
  UsageLimitsUnavailableName,
  UsageLimitWindowEntry,
  UsageProviderLimitsEntry,
} from '@vibesys/hosts';
import {plainName} from './format.js';

/** One machine's answer to the limits question. */
export interface HostLimits {
  readonly host: HostKey;
  readonly limits: UsageLimitsEntry;
}

/** The word a card says about itself when it is not a current reading. */
export type LimitsStatusWord = 'Stale' | 'Unsupported' | 'API key' | 'Unavailable';

/** A bar: one window of an account. */
export interface LimitBar {
  readonly id: string;
  readonly kind: UsageLimitKindName;
  readonly label: string;
  /** The share used, 0 to 1, for the bar. */
  readonly used: number;
  /** Whole percent left, 0 to 100, never below zero. */
  readonly left: number;
  /** `61% left`. */
  readonly leftText: string;
  /** When it refills, as a short span from the moment of the question (`3 h`, `2 d`); null if not stated. */
  readonly resets: string | null;
  /** Whether the window is used up. */
  readonly exhausted: boolean;
}

export interface LimitCard {
  /** Stable for the account across reads. */
  readonly key: string;
  readonly provider: string;
  readonly providerName: string;
  readonly plan: string | null;
  readonly bars: readonly LimitBar[];
  /** The machines that read this account, in the order they were asked. */
  readonly hosts: readonly HostKey[];
  readonly status: LimitsStatusWord | null;
  /** How long ago a stale reading was made (`2 h`); null for a current one. */
  readonly age: string | null;
  /** Reset credits the account holds. */
  readonly credits: number;
}

export interface LimitsView {
  readonly cards: readonly LimitCard[];
  /** Whether no machine answered the question at all. */
  readonly empty: boolean;
}

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

/** A span as the tab words it: the coarsest whole unit, rounded up so a window never refills early. */
export function span(milliseconds: number): string {
  const ms = Math.max(0, milliseconds);
  if (ms < HOUR) return `${Math.max(1, Math.ceil(ms / MINUTE))} min`;
  if (ms < DAY) return `${Math.ceil(ms / HOUR)} h`;
  return `${Math.ceil(ms / DAY)} d`;
}

/** An age as the tab words it: the coarsest whole unit, rounded down. */
export function ago(milliseconds: number): string {
  const ms = Math.max(0, milliseconds);
  if (ms < HOUR) return `${Math.floor(ms / MINUTE)} min`;
  if (ms < DAY) return `${Math.floor(ms / HOUR)} h`;
  return `${Math.floor(ms / DAY)} d`;
}

function bar(window: UsageLimitWindowEntry, now: number): LimitBar {
  const used = Number.isFinite(window.usedFraction) ? Math.max(0, window.usedFraction) : 0;
  const left = Math.max(0, Math.min(100, Math.round((1 - used) * 100)));
  return {
    id: window.id,
    kind: window.kind,
    label: window.label,
    used: Math.min(1, used),
    left,
    leftText: `${left}% left`,
    resets: window.resetsAt === null ? null : span(window.resetsAt - now),
    exhausted: left === 0,
  };
}

const REASONS: Readonly<Record<UsageLimitsUnavailableName, LimitsStatusWord>> = {
  unsupported: 'Unsupported',
  'api-key': 'API key',
  'probe-failed': 'Unavailable',
};

interface Reading {
  readonly host: HostKey;
  readonly entry: UsageProviderLimitsEntry;
  readonly limits: UsageAccountLimitsEntry;
}

/** The account a reading belongs to: its digest, or the machine when the provider named none. */
function accountOf(reading: Reading): string {
  return `${reading.entry.provider}\u0000${reading.limits.account ?? reading.host}`;
}

function newest(readings: readonly Reading[]): Reading {
  return readings.reduce((best, each) => (each.limits.readAt > best.limits.readAt ? each : best));
}

/** One account's card: each window from the reading that has it and read it last. */
function card(readings: readonly Reading[], now: number): LimitCard {
  const latest = newest(readings);
  const windows = new Map<string, {window: UsageLimitWindowEntry; readAt: number}>();
  for (const each of readings) {
    for (const window of each.limits.windows) {
      const held = windows.get(window.id);
      if (held === undefined || each.limits.readAt >= held.readAt) {
        windows.set(window.id, {window, readAt: each.limits.readAt});
      }
    }
  }
  const stale = latest.entry.status === 'stale';
  return {
    key: accountOf(latest),
    provider: latest.entry.provider,
    providerName: plainName(latest.entry.provider),
    plan: latest.limits.plan,
    bars: [...windows.values()].map(each => bar(each.window, now)),
    hosts: readings.map(each => each.host),
    status: stale ? 'Stale' : null,
    age: stale ? ago(now - latest.limits.readAt) : null,
    credits: latest.limits.resetCreditsAvailable ?? 0,
  };
}

/** The card of a provider no machine has a reading of: why, in a word, from the first reason given. */
function unavailable(provider: string, reason: UsageLimitsUnavailableName | null): LimitCard {
  return {
    key: `${provider}\u0000unavailable`,
    provider,
    providerName: plainName(provider),
    plan: null,
    bars: [],
    hosts: [],
    status: REASONS[reason ?? 'probe-failed'],
    age: null,
    credits: 0,
  };
}

/**
 * The tab's cards for what the machines answered at `now`: providers in the order first seen,
 * and within one, accounts in the order first read.
 */
export function limitsView(replies: readonly HostLimits[], now: number): LimitsView {
  const readings: Reading[] = [];
  const reasons = new Map<string, UsageLimitsUnavailableName | null>();
  const order: string[] = [];
  for (const {host, limits} of replies) {
    for (const entry of limits.providers) {
      if (!order.includes(entry.provider)) order.push(entry.provider);
      if (entry.limits === null) {
        if (!reasons.has(entry.provider)) reasons.set(entry.provider, entry.unavailable);
      } else {
        readings.push({host, entry, limits: entry.limits});
      }
    }
  }
  const cards = order.flatMap(provider => {
    const own = readings.filter(each => each.entry.provider === provider);
    if (own.length === 0) return [unavailable(provider, reasons.get(provider) ?? null)];
    const accounts = new Map<string, Reading[]>();
    for (const each of own) {
      const key = accountOf(each);
      accounts.set(key, [...(accounts.get(key) ?? []), each]);
    }
    return [...accounts.values()].map(group => card(group, now));
  });
  return {cards, empty: replies.length === 0};
}
