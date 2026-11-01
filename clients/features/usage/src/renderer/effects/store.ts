/**
 * The Usage page's shell: its machine, with its operations provided over the bridge, seen as a
 * store the screen renders. The machine starts when the screen first subscribes and asks again each
 * time the screen is shown after it was left, so the page never opens on figures from an earlier
 * visit without saying it is reading them again. A machine whose connection comes up while the page
 * is shown asks again, so figures that left it out, or a question that failed because it was
 * unreachable, are read afresh without the user pressing Retry.
 *
 * The screen state is a fold of the machine's context: the answers merged once per answer, the view
 * once per filter, the dialog once per change, each memoized on what it reads.
 */
import {type HostKey, type HostListing, hostFromKey, hostLabel, hostsComeUp} from '@vibesys/hosts';
import {actorStore, clockFrom, type ScheduleTimeout} from '@vibesys/machine';
import type {Store} from '@vibesys/store';
import {createActor, fromPromise} from 'xstate';
import {
  type Claimed,
  claimSources,
  type DialogState,
  draftOf,
  draftProblem,
  type HostReply,
  type HostRow,
  type LimitsContext,
  type LimitsScreenState,
  limitsMachine,
  limitsView,
  type Merged,
  mergeReplies,
  modelDetail,
  type PeriodEdges,
  pricesView,
  type ReadAnswer,
  type SaveRequest,
  type UsageActions,
  type UsageClock,
  type UsageContext,
  type UsagePeriod,
  type UsagePhase,
  type UsageScreenState,
  type UsageStatusWord,
  type UsageView,
  usageMachine,
  usageView,
  type ViewFilters,
} from '../../state/index.js';
import {readLimits, readUsage, refreshRates, savePrices, type UsageBridge} from './load.js';

export interface UsageStoreOptions {
  readonly schedule: ScheduleTimeout;
  /** The clock a question reads: now, and the zone the person reads their days in. */
  readonly clock: () => UsageClock;
  /** The period the page opens on. */
  readonly period?: UsagePeriod;
}

const PHASES: readonly string[] = [
  'loading',
  'connecting',
  'scanning',
  'refreshing',
  'rating',
  'ready',
  'saving',
  'unsupported',
  'failed',
];

function phaseOf(value: unknown): UsagePhase {
  if (typeof value === 'string' && PHASES.includes(value)) return value as UsagePhase;
  throw new Error(`the usage machine is in an unknown state: ${JSON.stringify(value)}`);
}

/** The header's word for each phase: none while the body says what it needs to (Loading, Retry). */
const STATUS: Readonly<Record<UsagePhase, UsageStatusWord | null>> = {
  loading: null,
  connecting: null,
  scanning: 'Scanning',
  refreshing: 'Refreshing',
  rating: 'Refreshing',
  ready: null,
  saving: 'Saving',
  unsupported: null,
  failed: null,
};

/** What the usage machine alone says of the screen; the store adds the Limits tab's. */
type UsageBody = Omit<UsageScreenState, 'limits'>;

interface Held {
  readonly answer: ReadAnswer;
  readonly edges: PeriodEdges;
  readonly claimed: readonly Claimed[];
  readonly merged: Merged;
}

/** The screen state of a machine snapshot, folding only what changed. */
class Folds {
  #held: Held | null = null;
  #view: {
    readonly held: Held;
    readonly filters: ViewFilters;
    readonly overridden: ReadonlySet<string>;
    readonly view: UsageView;
  } | null = null;
  #last: {
    readonly value: unknown;
    readonly context: UsageContext;
    readonly screen: UsageBody;
  } | null = null;

  merged(context: UsageContext): Held | null {
    const {answer, edges} = context;
    if (answer === null) return null;
    if (this.#held?.answer === answer && this.#held.edges === edges) return this.#held;
    const claimed = claimSources(answer.replies);
    this.#held = {
      answer,
      edges,
      claimed,
      merged: mergeReplies(claimed, Math.max(0, edges.edges.length - 1)),
    };
    return this.#held;
  }

  screen(snapshot: {readonly value: unknown; readonly context: UsageContext}): UsageBody {
    if (
      this.#last !== null &&
      this.#last.value === snapshot.value &&
      this.#last.context === snapshot.context
    ) {
      return this.#last.screen;
    }
    const {context} = snapshot;
    const phase = phaseOf(snapshot.value);
    const held = this.merged(context);
    const filters: ViewFilters = {
      // The Limits tab shows no figure of the period; the figures stay as the cost tab has them.
      tab: context.tab === 'limits' ? 'cost' : context.tab,
      source: context.source,
      hidden: new Set(context.hidden),
    };
    const overridden = new Set(
      held?.answer.replies.flatMap(reply =>
        reply.summary.prices.overrides.map(entry => entry.model.trim().toLowerCase()),
      ) ?? [],
    );
    let view: UsageView | null = null;
    if (held !== null) {
      const same =
        this.#view !== null &&
        this.#view.held === held &&
        this.#view.filters.tab === filters.tab &&
        this.#view.filters.source === filters.source &&
        this.#view.filters.hidden.size === filters.hidden.size &&
        [...filters.hidden].every(each => this.#view?.filters.hidden.has(each) === true);
      if (same && this.#view !== null) view = this.#view.view;
      else {
        view = usageView(held.merged, filters, {
          names: held.answer.names,
          edges: held.edges,
          pricing: held.answer.replies.map(reply => reply.summary.pricing),
          unreadablePrices: held.answer.replies.some(
            reply => reply.summary.pricesStatus === 'unreadable',
          ),
          olderRunsUnread: held.answer.olderRunsUnread,
          overridden,
        });
        this.#view = {held, filters, overridden, view};
      }
    }
    const screen: UsageBody = {
      phase,
      status: STATUS[phase],
      period: context.period,
      tab: context.tab,
      source: context.source,
      view,
      hosts: hostRows(context),
      dialog: dialogOf(context, held, filters),
      problem: context.problem,
      stale:
        held !== null && (phase === 'refreshing' || phase === 'scanning' || phase === 'rating'),
    };
    this.#last = {value: snapshot.value, context: snapshot.context, screen};
    return screen;
  }
}

function hostRows(context: UsageContext): readonly HostRow[] {
  const answer = context.answer;
  const label = (host: HostKey) => hostLabel(hostFromKey(host));
  const rows: HostRow[] = [];
  const seen = new Set<HostKey>();
  for (const reply of answer?.replies ?? []) {
    seen.add(reply.host);
    const working = reply.summary.pendingRuns > 0 || reply.summary.pendingFiles > 0;
    rows.push({
      host: reply.host,
      label: label(reply.host),
      on: true,
      status: working ? 'Scanning' : 'Ready',
    });
  }
  for (const host of answer?.unreachable ?? []) {
    seen.add(host);
    rows.push({host, label: label(host), on: true, status: 'Unreachable'});
  }
  for (const host of answer?.unsupported ?? []) {
    seen.add(host);
    rows.push({host, label: label(host), on: true, status: 'Update required'});
  }
  for (const host of context.hostsOff) {
    if (!seen.has(host)) rows.push({host, label: label(host), on: false, status: 'Off'});
  }
  return rows.sort((a, b) => a.label.localeCompare(b.label));
}

function dialogOf(context: UsageContext, held: Held | null, filters: ViewFilters): DialogState {
  const dialog = context.dialog;
  if (dialog === null) return null;
  const replies: readonly HostReply[] = held?.answer.replies ?? [];
  if (dialog.kind === 'model') {
    return {
      kind: 'model',
      detail:
        held === null
          ? null
          : modelDetail(held.merged, replies, filters, dialog.key, held.answer.names, held.edges),
    };
  }
  return {
    kind: 'prices',
    prices: pricesView(dialog.key, dialog.draft, {
      apply: dialog.apply,
      results: dialog.results,
      replies,
      problem: dialog.problem,
      names: held?.answer.names ?? [],
    }),
  };
}

/** The Limits tab's screen state of a machine snapshot, folded once per change. */
function limitsFolds(): (snapshot: {
  readonly value: unknown;
  readonly context: LimitsContext;
}) => LimitsScreenState {
  let last: {
    readonly value: unknown;
    readonly context: LimitsContext;
    readonly screen: LimitsScreenState;
  } | null = null;
  return snapshot => {
    const held = last;
    if (held !== null && held.value === snapshot.value && held.context === snapshot.context) {
      return held.screen;
    }
    const {context} = snapshot;
    const screen: LimitsScreenState = {
      view:
        context.answer === null ? null : limitsView(context.answer.replies, context.askedAt ?? 0),
      checking: snapshot.value === 'checking' || snapshot.value === 'connecting',
      failed: context.failed,
      unsupported:
        context.answer !== null &&
        context.answer.replies.length === 0 &&
        context.answer.unsupported.length > 0,
    };
    last = {value: snapshot.value, context, screen};
    return screen;
  };
}

/** The save the open prices dialog asks for, or null when it is not open or a destination refuses it. */
function saveRequestOf(context: UsageContext, held: Held | null): SaveRequest | null {
  const dialog = context.dialog;
  if (dialog?.kind !== 'prices' || held === null) return null;
  const destinations = held.answer.replies
    .filter(reply => dialog.apply.includes(reply.host))
    .map(reply => ({host: reply.host, book: reply.summary.prices}));
  const problem = destinations
    .map(each => draftProblem(dialog.draft, each.book))
    .find(each => each !== null);
  if (problem !== undefined || destinations.length === 0) return null;
  return {draft: dialog.draft, destinations};
}

/** The usage machine with its operations provided over `bridge`. */
function usageActor(bridge: UsageBridge, options: UsageStoreOptions) {
  const logic = usageMachine.provide({
    actors: {
      read: fromPromise(({input, signal}) =>
        readUsage(bridge, input.edges, options.clock().now, input.hostsOff, signal),
      ),
      refreshRates: fromPromise(({input, signal}) =>
        refreshRates(bridge, input.hosts, options.clock().now, signal),
      ),
      save: fromPromise(({input, signal}) => savePrices(bridge, input, signal)),
    },
  });
  return createActor(logic, {
    input: {period: options.period ?? '7d', clock: options.clock()},
    clock: clockFrom(options.schedule),
  });
}

/** The page's state: the usage machine's with the Limits tab's, the same object while neither changed. */
function joinScreens(
  body: () => UsageBody,
  limits: () => LimitsScreenState,
): () => UsageScreenState {
  let joined: {
    readonly body: UsageBody;
    readonly limits: LimitsScreenState;
    readonly screen: UsageScreenState;
  } | null = null;
  return () => {
    const own = {body: body(), limits: limits()};
    if (joined?.body !== own.body || joined.limits !== own.limits) {
      joined = {...own, screen: {...own.body, limits: own.limits}};
    }
    return joined.screen;
  };
}

/** The Limits tab's machine over `bridge`, as a store and the three things the page does to it. */
function limitsStore(
  bridge: UsageBridge,
  options: UsageStoreOptions,
  hostsOff: () => readonly HostKey[],
) {
  const actor = createActor(
    limitsMachine.provide({
      actors: {
        check: fromPromise(({input, signal}) =>
          readLimits(bridge, input.maxAgeS, input.now ?? options.clock().now, hostsOff(), signal),
        ),
      },
    }),
    {clock: clockFrom(options.schedule)},
  );
  const store = actorStore(actor, limitsFolds());
  return {
    getState: store.getState,
    subscribe: store.subscribe,
    start: () => actor.start(),
    stop: () => actor.stop(),
    open: (now: number) => actor.send({type: 'open', now}),
    refresh: (now: number) => actor.send({type: 'refresh', now}),
  };
}

/** The Usage page's store over `bridge`. `stop` ends the machine and the question it is asking. */
export function createUsageStore(
  bridge: UsageBridge,
  options: UsageStoreOptions,
): {
  readonly store: Store<UsageScreenState, never>;
  readonly actions: UsageActions;
  readonly stop: () => void;
} {
  const actor = usageActor(bridge, options);
  const limits = limitsStore(bridge, options, () => actor.getSnapshot().context.hostsOff);
  const folds = new Folds();
  const inner = actorStore(actor, snapshot => folds.screen(snapshot));
  const getState = joinScreens(inner.getState, limits.getState);
  const onLimitsTab = () => actor.getSnapshot().context.tab === 'limits';
  let started = false;
  let watching = 0;
  let listings: readonly HostListing[] = [];
  bridge.onListing(event => {
    const reachable = hostsComeUp(listings, [event.value]).length > 0;
    listings = [...listings.filter(each => each.host !== event.value.host), event.value];
    // A page that is not shown asks again when it is.
    if (reachable && watching > 0) {
      actor.send({type: 'refresh', clock: options.clock()});
      if (onLimitsTab()) limits.refresh(options.clock().now);
    }
  });
  const store: Store<UsageScreenState, never> = {
    getState,
    dispatch: () => undefined,
    subscribe: listener => {
      const unsubscribes = [inner.subscribe(listener), limits.subscribe(listener)];
      watching += 1;
      if (!started) {
        started = true;
        actor.start();
        limits.start();
      } else if (watching === 1) {
        actor.send({type: 'refresh', clock: options.clock()});
        if (onLimitsTab()) limits.open(options.clock().now);
      }
      return () => {
        watching -= 1;
        for (const unsubscribe of unsubscribes) unsubscribe();
      };
    },
  };
  const current = () => actor.getSnapshot().context;
  return {
    store,
    actions: {
      period: period => actor.send({type: 'period', period, clock: options.clock()}),
      tab: tab => {
        actor.send({type: 'tab', tab});
        if (tab === 'limits') limits.open(options.clock().now);
      },
      source: source => actor.send({type: 'source', source}),
      provider: provider => actor.send({type: 'provider', provider}),
      host: host => actor.send({type: 'host', host}),
      refresh: () => {
        if (onLimitsTab()) limits.refresh(options.clock().now);
        else actor.send({type: 'refresh', clock: options.clock()});
      },
      model: key => actor.send({type: 'model', key}),
      setPrice: key => {
        const held = folds.merged(current());
        const [provider = '', id = ''] = key.split('\u0000');
        const replies = held?.answer.replies ?? [];
        const resolved = replies
          .flatMap(reply => reply.summary.rates)
          .find(
            each => each.provider === provider && each.model === id && each.speed === 'standard',
          );
        const alias = replies
          .flatMap(reply => reply.summary.prices.aliases)
          .find(each => each.from.trim().toLowerCase() === id.trim().toLowerCase());
        const rate = resolved?.rate ?? null;
        actor.send({
          type: 'prices',
          key,
          draft: draftOf(id, rate, alias?.to ?? ''),
          apply: replies.map(reply => reply.host),
        });
      },
      edit: draft => actor.send({type: 'edit', draft}),
      applyTo: host => actor.send({type: 'apply', host}),
      save: () => {
        const context = current();
        const request = saveRequestOf(context, folds.merged(context));
        if (request !== null) actor.send({type: 'save', request});
      },
      close: () => actor.send({type: 'close'}),
    },
    stop: () => {
      limits.stop();
      actor.stop();
    },
  };
}
