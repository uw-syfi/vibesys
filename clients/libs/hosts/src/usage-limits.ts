/** What a host answers asking for each provider's plan limits, in the app's terms. */

export type UsageLimitKindName = 'session' | 'weekly' | 'monthly' | 'other';

/**
 * How a provider's limits were come by: `live` read by this call; `stored` a reading the host held
 * that was young enough; `stale` the read failed, so this is the last good reading, as old as its
 * `readAt`; `unavailable` the read failed and there is no earlier reading.
 */
export type UsageLimitsStatusName = 'live' | 'stored' | 'stale' | 'unavailable';

/** Why a provider has no limits to show: it has none to read, bills by token, or could not be read. */
export type UsageLimitsUnavailableName = 'unsupported' | 'api-key' | 'probe-failed';

/** One limit of an account: a share of an allowance and when it refills. */
export interface UsageLimitWindowEntry {
  /** Stable for the same window across reads. */
  readonly id: string;
  readonly kind: UsageLimitKindName;
  readonly label: string;
  /** The share used, 0 to 1; above 1 when the provider reports over-use. */
  readonly usedFraction: number;
  readonly resetsAt: number | null;
  readonly windowMinutes: number | null;
}

/** A credit that refills the account's limits at once when spent. */
export interface UsageResetCreditEntry {
  readonly id: string;
  readonly expiresAt: number | null;
  readonly title: string | null;
}

/** One provider's account as a read found it. */
export interface UsageAccountLimitsEntry {
  readonly provider: string;
  /** A digest of the account, equal across hosts for one account; never an email or an id. */
  readonly account: string | null;
  readonly plan: string | null;
  readonly windows: readonly UsageLimitWindowEntry[];
  /** The count the provider states; `resetCredits` can list fewer. */
  readonly resetCreditsAvailable: number | null;
  readonly resetCredits: readonly UsageResetCreditEntry[];
  readonly usageUrl: string | null;
  /** When the windows were read. */
  readonly readAt: number;
}

export interface UsageProviderLimitsEntry {
  readonly provider: string;
  readonly status: UsageLimitsStatusName;
  /** Absent exactly when the provider is `unavailable`. */
  readonly limits: UsageAccountLimitsEntry | null;
  /** Why the last read failed, when it did. */
  readonly unavailable: UsageLimitsUnavailableName | null;
}

export interface UsageLimitsEntry {
  readonly providers: readonly UsageProviderLimitsEntry[];
  readonly readAt: number;
}

/** What a host answers a limits question: its providers' limits, or that its VibeSys offers none. */
export type UsageLimitsOutcome =
  | {readonly kind: 'limits'; readonly limits: UsageLimitsEntry}
  | {readonly kind: 'unsupported'};
