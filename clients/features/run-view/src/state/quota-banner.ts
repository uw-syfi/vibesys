/**
 * What the quota banner says and offers, decided from `CoreState.quota` alone.
 *
 * Ported from the TUI's `projectQuota` (`clients/tui/src/session-model.ts`): the state projection
 * that decides a pause is current news rather than its terminal-specific rendering, which stays in
 * `clients/tui/src/quota-projection.ts` and is not reused here. A render here is a pure function of
 * the latest `CoreState`, so there is no "already shown" latch to carry forward: each render simply
 * asks whether the current quota fact is still the one worth showing.
 */
import type {CoreState, ProviderSwitch, QuotaPause} from '@vibesys/run-state';

/** The pause banner's own choices: the operator's resume affordances. */
interface QuotaPauseBanner {
  readonly kind: 'pause';
  readonly message: string;
  readonly detail: string;
  readonly wait: string;
  readonly fallback: QuotaPause['fallback'];
}

/** A settled switch, shown once as the reason a pause banner just disappeared. */
interface QuotaSwitchBanner {
  readonly kind: 'switch';
  readonly message: string;
  readonly detail: string;
}

export type QuotaBanner = QuotaPauseBanner | QuotaSwitchBanner;

/**
 * The quota banner to render, or `null` for none.
 *
 * A pause shows only while the run's own status says it is pausing or paused (`QuotaState`'s
 * documented contract: `CoreState.quota.pause` itself outlives the run leaving that status, so a
 * consumer has to apply this gate rather than trusting `pause !== null` alone). Once the pause
 * settles by a provider switch, the switch itself is current news exactly while it is still the
 * newest quota fact folded (`lastSwitch.sequence === quota.sequence`); a later pause or a plain
 * resume moves `quota.sequence` past it, which retires this banner with no extra bookkeeping.
 */
export function quotaBanner(run: CoreState): QuotaBanner | null {
  const {quota, status} = run;
  if (quota.pause !== null) {
    if (status !== 'pausing' && status !== 'paused') return null;
    return {kind: 'pause', ...pauseFacts(quota.pause)};
  }
  const switched = quota.lastSwitch;
  if (switched !== null && switched.sequence === quota.sequence) {
    return {kind: 'switch', ...switchFacts(switched)};
  }
  return null;
}

function pauseFacts(pause: QuotaPause): Omit<QuotaPauseBanner, 'kind'> {
  const why = pause.condition === 'quota_exhausted' ? 'is out of quota' : 'is rate limited';
  const reset =
    pause.resetsAt === null ? '' : ` Capacity returns at ${formatEpoch(pause.resetsAt)}.`;
  return {
    message: `${pause.provider} ${why}. The run is paused until it resumes.`,
    detail: `${pause.detail}${reset}`.trim(),
    wait:
      pause.resumesAt === null
        ? 'The run stays paused until an operator resumes it.'
        : `The run resumes by itself at ${formatEpoch(pause.resumesAt)}.`,
    fallback: pause.fallback,
  };
}

function switchFacts(switched: ProviderSwitch): Omit<QuotaSwitchBanner, 'kind'> {
  const model = switched.toModel === '' ? '' : ` (${switched.toModel})`;
  const who = switched.reason === 'policy' ? 'the quota policy' : 'an operator';
  return {
    message: `Switched from ${switched.fromProvider} to ${switched.toProvider}${model}.`,
    detail: `${who} made the switch: ${switched.detail}`,
  };
}

/** UTC, so the same event reads the same on every machine. */
function formatEpoch(seconds: number): string {
  return `${new Date(seconds * 1000).toISOString().slice(0, 16).replace('T', ' ')} UTC`;
}
