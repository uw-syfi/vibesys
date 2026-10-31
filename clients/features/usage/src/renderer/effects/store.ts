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
  type Merged,
  mergeReplies,
  modelDetail,
  type PeriodEdges,
  pricesView,
  type ReadAnswer,
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
import {readUsage, refreshRates, savePrices, type UsageBridge} from './load.js';

export interface UsageStoreOptions {
  readonly schedule: ScheduleTimeout;
  /** The clock a question reads: now, and the zone the person reads their days in. */
  readonly clock: () => UsageClock;
  /** The period the page opens on. */
  readonly period?: UsagePeriod;
}

const PHASES: readonly string[] = [
  'loading',
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

const STATUS: Readonly<Record<UsagePhase, UsageStatusWord>> = {
  loading: 'Connecting',
  scanning: 'Scanning',
  refreshing: 'Refreshing',
  rating: 'Refreshing',
  ready: 'Ready',
  saving: 'Saving',
  unsupported: 'Unavailable',
  failed: 'Unavailable',
};

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
    readonly screen: UsageScreenState;
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

  screen(snapshot: {readonly value: unknown; readonly context: UsageContext}): UsageScreenState {
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
      tab: context.tab,
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
          overridden,
        });
        this.#view = {held, filters, overridden, view};
      }
    }
    const screen: UsageScreenState = {
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
    prices: pricesView(
      dialog.key,
      dialog.draft,
      dialog.apply,
      dialog.results,
      replies,
      dialog.problem,
    ),
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
  const actor = createActor(logic, {
    input: {period: options.period ?? '7d', clock: options.clock()},
    clock: clockFrom(options.schedule),
  });
  const folds = new Folds();
  const inner = actorStore(actor, snapshot => folds.screen(snapshot));
  let started = false;
  let watching = 0;
  let listings: readonly HostListing[] = [];
  bridge.onListing(event => {
    const reachable = hostsComeUp(listings, [event.value]).length > 0;
    listings = [...listings.filter(each => each.host !== event.value.host), event.value];
    // A page that is not shown asks again when it is.
    if (reachable && watching > 0) actor.send({type: 'refresh', clock: options.clock()});
  });
  const store: Store<UsageScreenState, never> = {
    getState: inner.getState,
    dispatch: () => undefined,
    subscribe: listener => {
      const unsubscribe = inner.subscribe(listener);
      watching += 1;
      if (!started) {
        started = true;
        actor.start();
      } else if (watching === 1) {
        actor.send({type: 'refresh', clock: options.clock()});
      }
      return () => {
        watching -= 1;
        unsubscribe();
      };
    },
  };
  const current = () => actor.getSnapshot().context;
  return {
    store,
    actions: {
      period: period => actor.send({type: 'period', period, clock: options.clock()}),
      tab: tab => actor.send({type: 'tab', tab}),
      source: source => actor.send({type: 'source', source}),
      provider: provider => actor.send({type: 'provider', provider}),
      host: host => actor.send({type: 'host', host}),
      refresh: () => actor.send({type: 'refresh', clock: options.clock()}),
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
        const dialog = context.dialog;
        const held = folds.merged(context);
        if (dialog?.kind !== 'prices' || held === null) return;
        const destinations = held.answer.replies
          .filter(reply => dialog.apply.includes(reply.host))
          .map(reply => ({host: reply.host, book: reply.summary.prices}));
        const problem = destinations
          .map(each => draftProblem(dialog.draft, each.book))
          .find(each => each !== null);
        if (problem !== undefined || destinations.length === 0) return;
        actor.send({type: 'save', request: {draft: dialog.draft, destinations}});
      },
      close: () => actor.send({type: 'close'}),
    },
    stop: () => actor.stop(),
  };
}
