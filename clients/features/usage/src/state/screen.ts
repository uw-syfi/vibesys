/** What the Usage page renders and what it can do: the types its store and its screen share. */
import type {HostKey} from '@vibesys/hosts';
import type {UsagePeriod} from './edges.js';
import type {LimitsView} from './limits-view.js';
import type {ModelDetail, PricesView} from './model-view.js';
import type {PriceDraft} from './price-drafts.js';
import type {UsageSourceChoice, UsageTab, UsageView} from './usage-view.js';

/** What the page is doing about its figures. */
export type UsagePhase =
  | 'loading'
  | 'connecting'
  | 'scanning'
  | 'refreshing'
  | 'rating'
  | 'ready'
  | 'saving'
  | 'unsupported'
  | 'failed';

/** One machine in the host filter. */
export interface HostRow {
  readonly host: HostKey;
  readonly label: string;
  readonly on: boolean;
  /** `Ready`, `Scanning`, `Unreachable`, `Update required`, or `Off`. */
  readonly status: string;
}

/** The status word in the page header: said only while the page is working on its figures. */
export type UsageStatusWord = 'Scanning' | 'Refreshing' | 'Saving';

export type DialogState =
  | null
  | {readonly kind: 'model'; readonly detail: ModelDetail | null}
  | {readonly kind: 'prices'; readonly prices: PricesView | null};

/** What the Limits tab renders. */
export interface LimitsScreenState {
  /** The cards of the last answer; null before the first. */
  readonly view: LimitsView | null;
  /** Whether a question is being asked. */
  readonly checking: boolean;
  /** Whether the last question failed; the figures shown are the earlier ones. */
  readonly failed: boolean;
  /** Whether every machine that answered lacks the question: their VibeSys is older. */
  readonly unsupported: boolean;
}

/** What the screen renders. */
export interface UsageScreenState {
  readonly phase: UsagePhase;
  /** What the page is doing, or null when it is idle, still connecting, or has nothing to show. */
  readonly status: UsageStatusWord | null;
  readonly period: UsagePeriod;
  readonly tab: UsageTab;
  readonly source: UsageSourceChoice;
  /** The figures for the period: null while the first are read, and when there are none to show. */
  readonly view: UsageView | null;
  readonly hosts: readonly HostRow[];
  readonly dialog: DialogState;
  /** Why the last question failed, for a pointer to rest on. */
  readonly problem: string | null;
  /** Whether the figures shown are being read again. */
  readonly stale: boolean;
  readonly limits: LimitsScreenState;
}

export interface UsageActions {
  readonly period: (period: UsagePeriod) => void;
  readonly tab: (tab: UsageTab) => void;
  readonly source: (source: UsageSourceChoice) => void;
  readonly provider: (provider: string) => void;
  readonly host: (host: HostKey) => void;
  readonly refresh: () => void;
  readonly model: (key: string) => void;
  readonly setPrice: (key: string) => void;
  readonly edit: (draft: PriceDraft) => void;
  readonly applyTo: (host: HostKey) => void;
  readonly save: () => void;
  readonly close: () => void;
}

/** Whether each phase still waits for a reply (or for the machines to come up), so the page may change. */
const PHASE_AWAITS: Readonly<Record<UsagePhase, boolean>> = {
  loading: true,
  connecting: true,
  scanning: true,
  refreshing: true,
  rating: true,
  ready: false,
  saving: true,
  unsupported: false,
  failed: false,
};

/** Whether the page waits for a reply: its figures (any phase but a resting one) or the Limits question. */
export function usageAwaiting(state: UsageScreenState): boolean {
  return PHASE_AWAITS[state.phase] || state.limits.checking;
}
