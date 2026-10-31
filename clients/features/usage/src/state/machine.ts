/**
 * The Usage page's control state, as a machine (docs/contributing/clients-architecture.md,
 * "State"). It asks every connected host for the figures of a period, shows what came back, and
 * decides what a late answer means:
 *
 * - Choosing another period leaves the question being asked and enters it again, which stops it,
 *   so an answer for a period the person left never arrives. The question is the operation `read`,
 *   provided by `renderer/effects`; a deadline ends it when a host never answers.
 * - While a host reports runs or transcript files it has not read yet (`pending`), the machine asks
 *   again as soon as the reply is shown, and the status says `Scanning`; figures already shown stay
 *   on screen, dimmed by the screen, until the next arrive.
 * - A shown page asks again every minute, only after the last question returned.
 * - Prices are saved per destination: the operation `save` answers one result per host, so one
 *   host failing or having moved never hides another's. A saved price asks the figures again.
 *
 * What the person chose to look at (tab, source, hidden providers) filters answers already held, so
 * it asks nothing. Time and zone arrive on the events: the machine reads no clock.
 */
import type {HostKey, UsagePriceBookEntry, UsagePricingEntry} from '@vibesys/hosts';
import {unprovided} from '@vibesys/machine';
import {assign, setup} from 'xstate';
import {type PeriodEdges, periodEdges, type UsagePeriod} from './edges.js';
import type {HostReply} from './merge.js';
import {changedBook, checkRate, type PriceDraft} from './price-drafts.js';
import type {ModelName, UsageSourceChoice, UsageTab} from './usage-view.js';

/** What a question about the period came back with. */
export interface ReadAnswer {
  readonly replies: readonly HostReply[];
  /** Hosts that could not be read. */
  readonly unreachable: readonly HostKey[];
  /** Hosts whose VibeSys does not offer the question. */
  readonly unsupported: readonly HostKey[];
  readonly names: readonly ModelName[];
  /** Hosts whose connection is still coming up: not read yet, and not failed. */
  readonly connecting: readonly HostKey[];
  /** Whether a host knows more projects than one question can name, so older runs are not read. */
  readonly olderRunsUnread: boolean;
}

/** One destination's result of a save. */
export type SaveResult =
  | {readonly host: HostKey; readonly outcome: 'saved'; readonly prices: UsagePriceBookEntry}
  /** The host's book had moved: nothing changed; this is its book. */
  | {readonly host: HostKey; readonly outcome: 'moved'; readonly prices: UsagePriceBookEntry}
  | {readonly host: HostKey; readonly outcome: 'failed'; readonly reason: string}
  | {readonly host: HostKey; readonly outcome: 'unsupported'};

/** A save the person asked for: the same change made to each destination's own book. */
export interface SaveRequest {
  readonly draft: PriceDraft;
  /** Each destination and the book the change is made to (read at `prices.revision`). */
  readonly destinations: readonly {readonly host: HostKey; readonly book: UsagePriceBookEntry}[];
}

export type Dialog =
  | null
  | {readonly kind: 'model'; readonly key: string}
  | {
      readonly kind: 'prices';
      readonly key: string;
      readonly draft: PriceDraft;
      readonly apply: readonly HostKey[];
      /** Each destination's result of the last save; empty before one. */
      readonly results: readonly SaveResult[];
      /** Why the draft cannot be saved yet, in a word; null when it can. */
      readonly problem: string | null;
    };

export interface UsageClock {
  readonly now: number;
  readonly zone: string;
}

export interface UsageContext {
  readonly period: UsagePeriod;
  readonly edges: PeriodEdges;
  readonly tab: UsageTab;
  readonly source: UsageSourceChoice;
  readonly hidden: readonly string[];
  readonly hostsOff: readonly HostKey[];
  /** The figures last read, for the period shown; null before any, or after the period changed. */
  readonly answer: ReadAnswer | null;
  /** Why the last question failed. */
  readonly problem: string | null;
  readonly dialog: Dialog;
  /** The price lists the hosts last reported, by host. */
  readonly pricing: Readonly<Record<string, UsagePricingEntry>>;
  /** Hosts a fresh price list was already asked of this visit, so a failed fetch is not retried in a loop. */
  readonly ratesAsked: readonly HostKey[];
  /** How many times the page asked again while every machine was still connecting. */
  readonly polls: number;
}

export type UsageEvent =
  | {readonly type: 'period'; readonly period: UsagePeriod; readonly clock: UsageClock}
  | {readonly type: 'tab'; readonly tab: UsageTab}
  | {readonly type: 'source'; readonly source: UsageSourceChoice}
  | {readonly type: 'provider'; readonly provider: string}
  | {readonly type: 'host'; readonly host: HostKey}
  | {readonly type: 'refresh'; readonly clock: UsageClock}
  | {readonly type: 'model'; readonly key: string}
  | {
      readonly type: 'prices';
      readonly key: string;
      readonly draft: PriceDraft;
      readonly apply: readonly HostKey[];
    }
  | {readonly type: 'edit'; readonly draft: PriceDraft}
  | {readonly type: 'apply'; readonly host: HostKey}
  | {readonly type: 'save'; readonly request: SaveRequest}
  | {readonly type: 'close'};

export interface UsageInput {
  readonly period: UsagePeriod;
  readonly clock: UsageClock;
  readonly tab?: UsageTab;
  readonly source?: UsageSourceChoice;
  readonly hidden?: readonly string[];
}

/** How long a question may take, in milliseconds. */
export const USAGE_DEADLINE_MS = 60_000;
/** How often a shown page asks again, in milliseconds. */
export const USAGE_REFRESH_MS = 60_000;
/** How often a page waiting for its machines to connect asks again, and how many times at most. */
const CONNECT_POLL_MS = 1_500;
const CONNECT_POLLS_MAX = 40;

function toggle<T>(list: readonly T[], item: T): readonly T[] {
  return list.includes(item) ? list.filter(each => each !== item) : [...list, item];
}

/** Whether nothing was read yet because every machine is still connecting. */
function stillConnecting(answer: ReadAnswer | null): boolean {
  return (
    answer !== null &&
    answer.replies.length === 0 &&
    answer.unsupported.length === 0 &&
    answer.connecting.length > 0
  );
}

/** Whether the answer still has work a host left for a later call. */
export function pendingWork(answer: ReadAnswer | null): boolean {
  return (
    answer?.replies.some(
      reply => reply.summary.pendingRuns > 0 || reply.summary.pendingFiles > 0,
    ) === true
  );
}

export const usageMachine = setup({
  types: {
    context: {} as UsageContext,
    events: {} as UsageEvent,
    input: {} as UsageInput,
  },
  actors: {
    read: unprovided<
      ReadAnswer,
      {readonly edges: PeriodEdges; readonly hostsOff: readonly HostKey[]}
    >('read'),
    save: unprovided<readonly SaveResult[], SaveRequest>('save'),
    refreshRates: unprovided<null, {readonly hosts: readonly HostKey[]}>('refreshRates'),
  },
  actions: {
    ask: assign((_, params: {readonly period: UsagePeriod; readonly clock: UsageClock}) => ({
      period: params.period,
      edges: periodEdges(params.period, params.clock.now, params.clock.zone),
      problem: null,
    })),
    forget: assign({answer: null}),
    show: assign((_, params: {readonly answer: ReadAnswer}) => ({
      answer: params.answer,
      polls: 0,
      pricing: Object.fromEntries(
        params.answer.replies.map(reply => [reply.host, reply.summary.pricing]),
      ),
    })),
    fail: assign((_, params: {readonly problem: string}) => ({problem: params.problem})),
  },
  guards: {
    isUnsupported: (_, params: {readonly answer: ReadAnswer}) =>
      params.answer.replies.length === 0 &&
      params.answer.unreachable.length === 0 &&
      params.answer.unsupported.length > 0,
    isPending: (_, params: {readonly answer: ReadAnswer}) => pendingWork(params.answer),
    isConnecting: (_, params: {readonly answer: ReadAnswer}) => stillConnecting(params.answer),
    connectingKept: ({context}, params: {readonly answer: ReadAnswer}) =>
      context.answer !== null && stillConnecting(params.answer),
    pollsSpent: ({context}) => context.polls >= CONNECT_POLLS_MAX,
    ratesDue: ({context}) => hostsDue(context).length > 0,
    holdsAnswer: ({context}) => context.answer !== null,
  },
  delays: {deadline: USAGE_DEADLINE_MS, refresh: USAGE_REFRESH_MS, connectPoll: CONNECT_POLL_MS},
}).createMachine({
  id: 'usage',
  context: ({input}) => ({
    period: input.period,
    edges: periodEdges(input.period, input.clock.now, input.clock.zone),
    tab: input.tab ?? 'cost',
    source: input.source ?? 'all',
    hidden: input.hidden ?? [],
    hostsOff: [],
    answer: null,
    problem: null,
    dialog: null,
    pricing: {},
    ratesAsked: [],
    polls: 0,
  }),
  initial: 'loading',
  states: {
    // The first question, or the first after the period changed: nothing to show yet.
    loading: {
      invoke: {
        src: 'read',
        input: ({context}) => ({edges: context.edges, hostsOff: context.hostsOff}),
        onDone: [
          {
            guard: {type: 'isConnecting', params: ({event}) => ({answer: event.output})},
            target: 'connecting',
          },
          {
            guard: {type: 'isUnsupported', params: ({event}) => ({answer: event.output})},
            target: 'unsupported',
          },
          {
            guard: {type: 'isPending', params: ({event}) => ({answer: event.output})},
            target: 'scanning',
            actions: {type: 'show', params: ({event}) => ({answer: event.output})},
          },
          {target: 'ready', actions: {type: 'show', params: ({event}) => ({answer: event.output})}},
        ],
        onError: {
          target: 'failed',
          actions: {
            type: 'fail',
            params: ({event}) => ({
              problem: event.error instanceof Error ? event.error.message : String(event.error),
            }),
          },
        },
      },
      after: {
        deadline: {target: 'failed', actions: {type: 'fail', params: {problem: 'timed out'}}},
      },
    },
    // Asking again with figures on screen: a host had work left, or the minute passed.
    scanning: {
      invoke: {
        src: 'read',
        input: ({context}) => ({edges: context.edges, hostsOff: context.hostsOff}),
        onDone: [
          {
            guard: {type: 'isConnecting', params: ({event}) => ({answer: event.output})},
            target: 'ready',
          },
          {
            guard: {type: 'isPending', params: ({event}) => ({answer: event.output})},
            target: 'scanning',
            reenter: true,
            actions: {type: 'show', params: ({event}) => ({answer: event.output})},
          },
          {target: 'ready', actions: {type: 'show', params: ({event}) => ({answer: event.output})}},
        ],
        onError: {
          target: 'ready',
          actions: {
            type: 'fail',
            params: ({event}) => ({
              problem: event.error instanceof Error ? event.error.message : String(event.error),
            }),
          },
        },
      },
      after: {deadline: {target: 'ready', actions: {type: 'fail', params: {problem: 'timed out'}}}},
    },
    refreshing: {
      invoke: {
        src: 'read',
        input: ({context}) => ({edges: context.edges, hostsOff: context.hostsOff}),
        onDone: [
          // Machines still coming up: keep what is shown, or wait for them when nothing is.
          {
            guard: {type: 'connectingKept', params: ({event}) => ({answer: event.output})},
            target: 'ready',
          },
          {
            guard: {type: 'isConnecting', params: ({event}) => ({answer: event.output})},
            target: 'connecting',
          },
          {
            guard: {type: 'isPending', params: ({event}) => ({answer: event.output})},
            target: 'scanning',
            actions: {type: 'show', params: ({event}) => ({answer: event.output})},
          },
          {target: 'ready', actions: {type: 'show', params: ({event}) => ({answer: event.output})}},
        ],
        // Figures already shown stay; with none to keep, the page says it cannot read them.
        onError: [
          {
            guard: 'holdsAnswer',
            target: 'ready',
            actions: {
              type: 'fail',
              params: ({event}) => ({
                problem: event.error instanceof Error ? event.error.message : String(event.error),
              }),
            },
          },
          {
            target: 'failed',
            actions: {
              type: 'fail',
              params: ({event}) => ({
                problem: event.error instanceof Error ? event.error.message : String(event.error),
              }),
            },
          },
        ],
      },
      after: {
        deadline: [
          {
            guard: 'holdsAnswer',
            target: 'ready',
            actions: {type: 'fail', params: {problem: 'timed out'}},
          },
          {target: 'failed', actions: {type: 'fail', params: {problem: 'timed out'}}},
        ],
      },
    },
    ready: {
      always: {guard: 'ratesDue', target: 'rating'},
      after: {refresh: {target: 'refreshing'}},
    },
    // Asking the hosts whose price list is due to fetch it, once a visit, before pricing again.
    rating: {
      entry: assign({ratesAsked: ({context}) => [...context.ratesAsked, ...hostsDue(context)]}),
      invoke: {
        src: 'refreshRates',
        input: ({context}) => ({
          hosts: context.ratesAsked.filter(host => context.pricing[host]?.refreshDue === true),
        }),
        onDone: 'refreshing',
        onError: 'ready',
      },
      after: {deadline: 'ready'},
    },
    saving: {
      invoke: {
        src: 'save',
        input: ({event}) => {
          if (event.type !== 'save') throw new Error('saving starts from a save');
          return event.request;
        },
        onDone: {
          target: 'refreshing',
          actions: assign({
            dialog: ({context, event}) =>
              context.dialog?.kind === 'prices'
                ? {...context.dialog, results: event.output}
                : context.dialog,
          }),
        },
        onError: {
          target: 'ready',
          actions: assign({
            dialog: ({context, event}) =>
              context.dialog?.kind === 'prices'
                ? {
                    ...context.dialog,
                    results: context.dialog.apply.map(host => ({
                      host,
                      outcome: 'failed' as const,
                      reason:
                        event.error instanceof Error ? event.error.message : String(event.error),
                    })),
                  }
                : context.dialog,
          }),
        },
      },
      // A host that never answers a save leaves the dialog saying it was not saved, not Saving.
      after: {
        deadline: {
          target: 'ready',
          actions: assign({
            dialog: ({context}) =>
              context.dialog?.kind === 'prices'
                ? {
                    ...context.dialog,
                    results: context.dialog.apply.map(host => ({
                      host,
                      outcome: 'failed' as const,
                      reason: 'timed out',
                    })),
                  }
                : context.dialog,
          }),
        },
      },
    },
    // Every machine is still connecting: that is not a failure. Ask again shortly, until a machine
    // answers, one comes up (its listing asks the page again), or the wait is spent.
    connecting: {
      after: {
        connectPoll: [
          {
            guard: 'pollsSpent',
            target: 'failed',
            actions: {type: 'fail', params: {problem: 'timed out'}},
          },
          {target: 'loading', actions: assign({polls: ({context}) => context.polls + 1})},
        ],
      },
    },
    unsupported: {},
    failed: {},
  },
  on: {
    period: {
      target: '.loading',
      reenter: true,
      actions: [
        {type: 'ask', params: ({event}) => ({period: event.period, clock: event.clock})},
        'forget',
      ],
    },
    refresh: {
      target: '.refreshing',
      reenter: true,
      actions: {
        type: 'ask',
        params: ({context, event}) => ({period: context.period, clock: event.clock}),
      },
    },
    tab: {actions: assign({tab: ({event}) => event.tab})},
    source: {actions: assign({source: ({event}) => event.source})},
    provider: {
      actions: assign({hidden: ({context, event}) => toggle(context.hidden, event.provider)}),
    },
    host: {
      target: '.loading',
      reenter: true,
      actions: [
        assign({hostsOff: ({context, event}) => toggle(context.hostsOff, event.host)}),
        'forget',
      ],
    },
    model: {actions: assign({dialog: ({event}) => ({kind: 'model' as const, key: event.key})})},
    prices: {
      actions: assign({
        dialog: ({event}) => ({
          kind: 'prices' as const,
          key: event.key,
          draft: event.draft,
          apply: event.apply,
          results: [],
          problem: checkRate(event.draft.rate).ok || event.draft.automatic ? null : 'invalid',
        }),
      }),
    },
    edit: {
      actions: assign({
        dialog: ({context, event}) => {
          if (context.dialog?.kind !== 'prices') return context.dialog;
          const checked = checkRate(event.draft.rate);
          return {
            ...context.dialog,
            draft: event.draft,
            results: [],
            problem: event.draft.automatic || checked.ok ? null : checked.field,
          };
        },
      }),
    },
    apply: {
      actions: assign({
        dialog: ({context, event}) =>
          context.dialog?.kind === 'prices'
            ? {...context.dialog, apply: toggle(context.dialog.apply, event.host), results: []}
            : context.dialog,
      }),
    },
    save: {target: '.saving', reenter: true},
    close: {actions: assign({dialog: null})},
  },
});

/** The hosts whose price list is due and was not asked this visit. */
export function hostsDue(context: UsageContext): readonly HostKey[] {
  return Object.entries(context.pricing).flatMap(([host, pricing]) =>
    pricing.refreshDue && !context.ratesAsked.includes(host) ? [host] : [],
  );
}

/** The book a draft makes of `book` for a save, or why it cannot be made; the page's own check. */
export function draftProblem(draft: PriceDraft, book: UsagePriceBookEntry): string | null {
  const checked = checkRate(draft.rate);
  if (!draft.automatic && !checked.ok) return checked.field;
  const changed = changedBook(book, draft, checked.ok ? checked.rate : null);
  return changed.ok ? null : changed.reason;
}
