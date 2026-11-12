/**
 * An in-memory daemon over a scenario: the part of the contract a screen reads. It commits events
 * in order (`commit` reveals more of the scenario, `dispatch` appends the facts a command causes),
 * answers a snapshot, events after a sequence, and pages of items, and notifies watchers.
 */
import {create} from '@bufbuild/protobuf';
import {timestampFromMs} from '@bufbuild/protobuf/wkt';
import {
  AgentRole,
  AgentSchema,
  AgentStatus,
  AgentStopStage,
  CatchUpRequestSchema,
  type CheckIn,
  CheckInSchema,
  CheckInState,
  type Command,
  Delivery,
  ElisionSchema,
  type GetRunHistoryResponse,
  GetRunHistoryResponseSchema,
  type HostCost,
  type Item,
  ItemSchema,
  ItemStatus,
  type ItemsPage,
  ItemsPageSchema,
  type Message,
  MessageSchema,
  type ModelCatalog,
  oneofIs,
  type Plan,
  type Receipt,
  ReceiptSchema,
  Refusal,
  Rejection,
  ReportKind,
  ReportSchema,
  ReportScope,
  type RunChange,
  RunChangeSchema,
  type RunCost,
  RunStatus,
  type RunSummary,
  RunSummarySchema,
  type RunsSnapshot,
  RunsSnapshotSchema,
  type SearchState,
  type SetupView,
  SnapshotDetail,
  type StartRun,
  type Turn,
  TurnSchema,
  TurnStatus,
  type UpdateRun,
  ViewerSchema,
  type WindowFacts,
} from '@vibesys/orchestrator-proto';
import {applyChange} from '../../state/orchestrator/fold.js';
import {emptyState, type OrchestratorState} from '../../state/orchestrator/state.js';
import {ms} from '../../state/orchestrator/time.js';
import {invokeCallback} from '../callback-errors.js';
import {applyPendingLead, decideUpdate} from './fake-run-settings.js';
import type {Ledger, SetupHost} from './fake-setup.js';
import type {Scenario} from './scenario.js';
import {searchFixture} from './search.js';
import {servedSummary} from './served-summary.js';

/** The size past which a page leaves out reasoning text and tool output, as the real daemon does. */
export const ELIDE_OVER = 4096;

const bytesOf = (text: string): number => new TextEncoder().encode(text).length;

/** `item` as a page carries it: the large reasoning text or tool output left out, with how large it was. */
function elided(item: Item): {readonly item: Item; readonly bytes: number | null} {
  const {body} = item;
  switch (body.case) {
    case 'reasoning':
      return bytesOf(body.value.text) > ELIDE_OVER
        ? {
            item: {...item, body: {case: 'reasoning', value: {...body.value, text: ''}}},
            bytes: bytesOf(body.value.text),
          }
        : {item, bytes: null};
    case 'tool':
      return bytesOf(body.value.output) > ELIDE_OVER
        ? {
            item: {...item, body: {case: 'tool', value: {...body.value, output: ''}}},
            bytes: bytesOf(body.value.output),
          }
        : {item, bytes: null};
    case 'assistant':
    case 'prompt':
    case undefined:
      return {item, bytes: null};
    default:
      body satisfies never;
      return {item, bytes: null};
  }
}

const FINAL = new Set([AgentStatus.ENDED, AgentStatus.FAILED, AgentStatus.LOST]);

/** The stop stages of an agent whose stop the host has started and not yet seen through. */
const STOPPING = new Set([AgentStopStage.INTERRUPTING, AgentStopStage.CLOSING]);

/** Where a message stands while it is still on its way: neither read nor rejected. */
const IN_TRANSIT = new Set([Delivery.QUEUED, Delivery.STEERED, Delivery.NEW_TURN]);

/**
 * How many turns the host starts for one message (`MaxDeliveryAttempts` in the orchestrator's
 * core). The host does not serve it, so the fake holds its own copy.
 */
const MAX_DELIVERY_ATTEMPTS = 3;

/** Every kind of command, so a command is read through `oneofIs` for the one kind it is. */
const COMMAND_KINDS = {
  startRun: true,
  stopRun: true,
  spawnAgent: true,
  stopAgent: true,
  sendMessage: true,
  postHandoff: true,
  postReport: true,
  requestCatchUp: true,
  updateRun: true,
  beginSetup: true,
  completeSetup: true,
  declareTask: true,
} as const;

/** The install of the app that calls: what the main process stamps on every call as metadata. */
export interface FakeViewer {
  readonly id: string;
  readonly label: string;
}

/** The viewer a fake daemon's calls are made as, unless a test says otherwise (`null` is none). */
export const FAKE_VIEWER: FakeViewer = {id: 'install-ana', label: 'ana'};

/** What the host says to a call that needs a viewer and names none. */
export const NO_VIEWER_REFUSAL =
  "the call's vibesys-viewer-id metadata is missing: a catch-up is recorded for a viewer, so the call names one with this field and vibesys-viewer-label";

export class FakeDaemon {
  readonly #log: RunChange[];
  #visible: number;
  readonly #receipts = new Map<string, Receipt>();
  readonly #watchers = new Set<() => void>();
  readonly #costs = new Map<string, RunCost>();
  readonly #hostCosts = new Map<string, HostCost>();
  readonly #started: StartRun[] = [];
  #models: ModelCatalog | undefined;
  readonly #setupViews = new Map<string, SetupView>();

  /** The setup world of this host (`FakeSetup`), when it has one: it answers BeginSetup and CompleteSetup. */
  setup: SetupHost | undefined;

  constructor(
    readonly scenario: Scenario,
    visible: number = scenario.changes.length,
    /** Who the calls are made as; `null` makes calls with no viewer, which a catch-up refuses. */
    readonly viewer: FakeViewer | null = FAKE_VIEWER,
  ) {
    this.#log = [...scenario.changes];
    this.#visible = visible;
  }

  /**
   * What the host says it runs agents on, as its snapshots carry it. A start on a model outside a
   * non-empty allowed list is refused as the host refuses it (`MODEL_NOT_ALLOWED`, naming the model and
   * the list); a host with no catalog serves none and allows everything.
   */
  setModels(models: ModelCatalog | undefined): void {
    this.#models = models;
  }

  /** The starts the host took, in order. */
  get started(): readonly StartRun[] {
    return this.#started;
  }

  get sequence(): bigint {
    return BigInt(this.#visible);
  }

  get plans(): readonly Plan[] {
    return this.scenario.plans;
  }

  /** Reveal up to `count` more events of the scenario. */
  commit(count: number): void {
    this.#visible = Math.min(this.#log.length, this.#visible + count);
    for (const watcher of this.#watchers) invokeCallback(watcher);
  }

  /**
   * Forget the last `count` events committed, as a host does that restarts after a power cut took
   * the tail of its log: a client that read them holds a cursor past the end of the log.
   */
  loseTail(count: number): void {
    this.#visible = Math.max(0, this.#visible - count);
  }

  /**
   * Set the whole cost view the host works out for a run. The Fake computes none: a test says what
   * the host would send, and a stream opened after it starts with this view.
   */
  publishCost(cost: RunCost): void {
    this.#costs.set(cost.runId, cost);
    for (const watcher of this.#watchers) invokeCallback(watcher);
  }

  /** The newest cost view of a run, or undefined when the host has none to send. */
  costOf(runId: string): RunCost | undefined {
    return this.#costs.get(runId);
  }

  /**
   * Set the whole host cost view the host works out for a window: everything it holds when `sinceMs`
   * is null, or the turns that finished at or after that instant. The Fake computes none: a test says
   * what the host would send, and a stream opened after it starts with this view.
   */
  publishHostCost(cost: HostCost, sinceMs: number | null = null): void {
    this.#hostCosts.set(String(sinceMs), cost);
    for (const watcher of this.#watchers) invokeCallback(watcher);
  }

  /** The newest host cost view of a window, or undefined when the host has none to send. */
  hostCostOf(sinceMs: number | null): HostCost | undefined {
    return this.#hostCosts.get(String(sinceMs));
  }

  /** Call `listener` whenever events are committed; returns how to stop. */
  watch(listener: () => void): () => void {
    this.#watchers.add(listener);
    return () => void this.#watchers.delete(listener);
  }

  /** The state the first `upTo` changes make, kept for the next read of the same prefix. */
  #folded: {readonly upTo: number; readonly state: OrchestratorState} | null = null;

  #state(upTo: number): OrchestratorState {
    if (this.#folded?.upTo === upTo) return this.#folded.state;
    // The log only grows at its end, so a shorter prefix already folded is where the next one starts.
    const from = this.#folded !== null && this.#folded.upTo < upTo ? this.#folded : null;
    const state = this.#log
      .slice(from?.upTo ?? 0, upTo)
      .reduce(applyChange, from?.state ?? emptyState);
    this.#folded = {upTo, state};
    return state;
  }

  /** How many runs the host says its snapshot left out, for a test of a host with more runs than it lists. */
  runsLeftOut = 0;

  /**
   * How many turns a run-history page may hold, earliest first, or null for all. This models the
   * real host's bounded timeline independently of the per-agent item pages.
   */
  historyTurnLimit: number | null = null;

  /** The summary the host serves of each run it lists, as it stands now. */
  summaries(): RunSummary[] {
    return [...this.snapshot().summaries];
  }

  /** The summary of each run in `state`, read with the search views the host holds. */
  #summariesOf(state: OrchestratorState, searches: readonly SearchState[]): RunSummary[] {
    return Object.keys(state.runs)
      .sort()
      .map(runId => {
        const summary = servedSummary(
          state,
          runId,
          searches.find(search => search.runId === runId),
        );
        const setup = this.#setupViews.get(runId);
        return setup === undefined ? summary : create(RunSummarySchema, {...summary, setup});
      });
  }

  /** What a setup world needs of this host: to commit facts, read the state, and serve a run's setup view. */
  ledger(): Ledger {
    return {
      record: (atMs, body) => this.#append(atMs, body),
      state: () => this.#state(this.#visible),
      publishSetup: (runId, view) => {
        this.#setupViews.set(runId, view);
        for (const watcher of this.#watchers) invokeCallback(watcher);
      },
    };
  }

  /**
   * The snapshot of runs. With detail SUMMARY it lists the runs and their summaries and nothing of their
   * histories, as the host does; otherwise it holds the whole histories too.
   */
  snapshot(detail: SnapshotDetail = SnapshotDetail.UNSPECIFIED): RunsSnapshot {
    const state = this.#state(this.#visible);
    const whole = create(RunsSnapshotSchema, {
      runs: Object.values(state.runs),
      agents: Object.values(state.agents),
      turns: Object.values(state.turns),
      messages: Object.values(state.messages),
      checkIns: Object.values(state.checkIns),
      sequence: state.sequence,
      searches:
        this.scenario.searches !== undefined
          ? [...this.scenario.searches]
          : this.scenario.search === true
            ? [
                searchFixture(
                  this.scenario.runId,
                  this.scenario.endMs,
                  this.scenario.searchStale ?? false,
                  this.scenario.searchTruncated ?? false,
                ),
              ]
            : [],
    });
    return create(RunsSnapshotSchema, {
      ...whole,
      summaries: this.#summariesOf(state, whole.searches),
      runsLeftOut: this.runsLeftOut,
      truncated: this.runsLeftOut > 0,
      ...(this.#models === undefined ? {} : {models: this.#models}),
      ...(detail === SnapshotDetail.SUMMARY
        ? {agents: [], turns: [], messages: [], checkIns: []}
        : {}),
    });
  }

  /**
   * One page of a run's history. The first page (cursor zero) holds the run's agents, turns and
   * check-ins; every page holds up to `pageMessages` of its messages in the order they were made, and
   * names the cursor of the next, which is zero after the last.
   */
  history(runId: string, cursor: bigint, pageMessages = 200): GetRunHistoryResponse {
    const state = this.#state(this.#visible);
    const all = Object.values(state.messages).filter(message => message.runId === runId);
    const allTurns = Object.values(state.turns).filter(
      turn => state.agents[turn.agentId]?.runId === runId,
    );
    const turnLimit = Math.max(0, this.historyTurnLimit ?? allTurns.length);
    const turns = allTurns.slice(0, turnLimit);
    const turnsLeftOutCount = allTurns.length - turns.length;
    const from = Number(cursor);
    const messages = all.slice(from, from + pageMessages);
    const next = from + messages.length;
    const first = cursor === 0n;
    return create(GetRunHistoryResponseSchema, {
      sequence: state.sequence,
      agents: first ? Object.values(state.agents).filter(agent => agent.runId === runId) : [],
      turns: first ? turns : [],
      turnsLeftOut: first && turnsLeftOutCount > 0,
      turnsLeftOutCount: first ? turnsLeftOutCount : 0,
      checkIns: first ? Object.values(state.checkIns).filter(each => each.runId === runId) : [],
      messages,
      nextCursor: next < all.length ? BigInt(next) : 0n,
    });
  }

  changesAfter(sequence: bigint): RunChange[] {
    return this.#log.slice(Number(sequence), this.#visible);
  }

  #items(agentId: string): Item[] {
    return Object.values(this.#state(this.#visible).items)
      .filter(item => item.agentId === agentId)
      .sort((a, b) => b.ordinal - a.ordinal);
  }

  /** The newest `pageSize` items below `beforeOrdinal` (all of them when it is 0), newest first. */
  page(agentId: string, pageSize: number, beforeOrdinal: number): ItemsPage {
    const all = this.#items(agentId).filter(
      item => beforeOrdinal === 0 || item.ordinal < beforeOrdinal,
    );
    const whole = all.slice(0, pageSize);
    const shown = whole.map(elided);
    const items = shown.map(one => one.item);
    const oldest = items[items.length - 1];
    return create(ItemsPageSchema, {
      items,
      elisions: shown.flatMap(one =>
        one.bytes === null
          ? []
          : [create(ElisionSchema, {ordinal: one.item.ordinal, bodyBytes: BigInt(one.bytes)})],
      ),
      sequence: this.sequence,
      nextBeforeOrdinal: oldest !== undefined && oldest.ordinal > 1 ? oldest.ordinal : 0,
    });
  }

  /** One item of an agent, whole, or undefined when there is none at that ordinal. */
  item(agentId: string, ordinal: number): Item | undefined {
    return this.#items(agentId).find(item => item.ordinal === ordinal);
  }

  /**
   * What the host says to a call outright, before it decides the command: set for a catch-up from a
   * client that names no viewer, which the real host refuses with `InvalidArgument`.
   */
  callRefusal(command: Command): string | undefined {
    return oneofIs(command.body, 'requestCatchUp', COMMAND_KINDS) && this.viewer === null
      ? NO_VIEWER_REFUSAL
      : undefined;
  }

  /**
   * Carry out a command: the same id answers with the same receipt. `at` is the host's clock for
   * the command, which a fixture sets to place a check-in's window; the log's own clock by default.
   */
  dispatch(command: Command, at: number = this.#now()): Receipt {
    const seen = this.#receipts.get(command.id);
    if (seen !== undefined) return seen;
    const receipt = this.#receiptFor(command, at);
    this.#receipts.set(command.id, receipt);
    return receipt;
  }

  /** What the host answers a command it has not answered before. */
  #receiptFor(command: Command, at: number): Receipt {
    const {body} = command;
    if (oneofIs(body, 'sendMessage', COMMAND_KINDS))
      return this.#sendMessage(command.id, body.value.agentId, body.value.text, at);
    if (oneofIs(body, 'stopAgent', COMMAND_KINDS))
      return this.#stopAgent(command.id, body.value.agentId, at);
    if (oneofIs(body, 'requestCatchUp', COMMAND_KINDS))
      return this.#catchUp(command.id, body.value.runId, at);
    if (oneofIs(body, 'startRun', COMMAND_KINDS)) return this.#startRun(command.id, body.value);
    if (oneofIs(body, 'updateRun', COMMAND_KINDS))
      return this.#updateRun(command.id, body.value, at);
    if (this.setup !== undefined && oneofIs(body, 'beginSetup', COMMAND_KINDS))
      return this.setup.begin(command.id, body.value, at);
    if (this.setup !== undefined && oneofIs(body, 'completeSetup', COMMAND_KINDS))
      return this.setup.complete(command.id, body.value, at);
    return this.#refuse(command.id, Refusal.NOT_PERMITTED, '', at);
  }

  /** The fake clock: one second for each fact the log holds, from a fixed start. */
  #now(): number {
    return Date.parse('2026-11-01T18:00:00Z') + this.#log.length * 1000;
  }

  /** `viewer`'s check-ins in `runId`, the newest first (by the end of the window, then the start). */
  #checkInsOf(runId: string, viewerId: string): CheckIn[] {
    return Object.values(this.#state(this.#visible).checkIns)
      .filter(checkIn => checkIn.runId === runId && checkIn.viewer?.id === viewerId)
      .sort(
        (a, b) =>
          (ms(b.at) ?? 0) - (ms(a.at) ?? 0) ||
          (ms(b.since) ?? 0) - (ms(a.since) ?? 0) ||
          b.id.localeCompare(a.id),
      );
  }

  /**
   * A press of Catch me up as the host decides it. A press while the viewer's newest check-in is
   * still being worked out answers with that one. Otherwise the check-in is pending, its window
   * starts where the viewer's last one that did not fail ended (the run's start for a first one)
   * and ends at the press or, for a run that is over, where the run ended; a live run whose lead
   * can still be asked is sent a request, which is not yet delivered. The facts are not worked out
   * here: a test settles the check-in (`settleCheckIn`) with the facts it wants served.
   */
  #catchUp(commandId: string, runId: string, atMs: number): Receipt {
    const state = this.#state(this.#visible);
    const run = state.runs[runId];
    if (run === undefined) return this.#refuse(commandId, Refusal.RUN_NOT_FOUND, '', atMs);
    if (this.viewer === null) return this.#refuse(commandId, Refusal.NO_VIEWER, '', atMs);
    const mine = this.#checkInsOf(runId, this.viewer.id);
    const answer = (checkIn: CheckIn): Receipt => {
      const receipt = create(ReceiptSchema, {
        commandId,
        runId,
        checkInId: checkIn.id,
        messageId: checkIn.askMessageId,
        sequence: BigInt(this.#log.length + 1),
      });
      this.#append(atMs, {case: 'receipt', value: receipt});
      return receipt;
    };
    const newest = mine[0];
    if (newest?.state === CheckInState.PENDING) return answer(newest);
    const since =
      ms(mine.find(checkIn => checkIn.state !== CheckInState.FAILED)?.at) ??
      ms(run.createdAt) ??
      atMs;
    const over = run.status === RunStatus.ENDED || run.status === RunStatus.FAILED;
    const at = Math.max(since, over ? (ms(run.endedAt) ?? atMs) : atMs);
    const id = `check-in-${this.#log.length + 1}`;
    const asked = run.status === RunStatus.RUNNING && !run.finalReported;
    const ask = asked ? `ask-${id}` : '';
    if (asked) {
      this.#append(atMs, {
        case: 'message',
        value: create(MessageSchema, {
          id: ask,
          runId,
          toAgentId: run.leadAgentId,
          delivery: Delivery.QUEUED,
          at: timestampFromMs(atMs),
          body: {
            case: 'catchUpRequest',
            value: create(CatchUpRequestSchema, {
              checkInId: id,
              since: timestampFromMs(since),
              until: timestampFromMs(at),
            }),
          },
        }),
      });
    }
    const checkIn = create(CheckInSchema, {
      id,
      runId,
      viewer: create(ViewerSchema, this.viewer),
      since: timestampFromMs(since),
      at: timestampFromMs(at),
      state: CheckInState.PENDING,
      askMessageId: ask,
    });
    this.#append(atMs, {case: 'checkIn', value: checkIn});
    return answer(checkIn);
  }

  #checkIn(id: string): CheckIn {
    const held = this.#state(this.#visible).checkIns[id];
    if (held === undefined) throw new Error(`the fake host has no check-in ${id}`);
    return held;
  }

  /** The host has worked out the facts of a check-in: it is ready with them, for good. */
  settleCheckIn(id: string, facts: WindowFacts): void {
    const held = this.#checkIn(id);
    if (held.state !== CheckInState.PENDING) return;
    this.#append(this.#now(), {
      case: 'checkIn',
      value: create(CheckInSchema, {...held, state: CheckInState.READY, facts}),
    });
  }

  /** The host could not work out the facts of a check-in: it failed with the reason. */
  failCheckIn(id: string, failure: string): void {
    const held = this.#checkIn(id);
    if (held.state !== CheckInState.PENDING) return;
    this.#append(this.#now(), {
      case: 'checkIn',
      value: create(CheckInSchema, {...held, state: CheckInState.FAILED, failure}),
    });
  }

  /** The lead answers the request that came with a check-in: its report is posted and linked. */
  postReport(id: string, headline: string, changed = ''): void {
    const held = this.#checkIn(id);
    const ask = this.#state(this.#visible).messages[held.askMessageId];
    if (ask === undefined) throw new Error(`check-in ${id} did not ask the lead`);
    const at = this.#now();
    const report = `report-${id}`;
    this.#append(at, {
      case: 'message',
      value: create(MessageSchema, {...ask, delivery: Delivery.CONSUMED}),
    });
    this.#append(at, {
      case: 'message',
      value: create(MessageSchema, {
        id: report,
        runId: held.runId,
        fromAgentId: ask.toAgentId,
        delivery: Delivery.CONSUMED,
        at: timestampFromMs(at),
        body: {
          case: 'report',
          value: create(ReportSchema, {
            headline,
            changed,
            kind: ReportKind.PROGRESS,
            scope: ReportScope.WINDOW,
            sequence: 1,
          }),
        },
      }),
    });
    this.#append(at, {
      case: 'checkIn',
      value: create(CheckInSchema, {...held, reportMessageId: report}),
    });
  }

  /**
   * The host gives up delivering a message it had accepted: the stored message becomes rejected
   * and says why, which is how a person finds out a receipt's first answer did not hold.
   */
  reject(messageId: string, rejection: Rejection = Rejection.AGENT_GONE): void {
    const message = this.#state(this.#visible).messages[messageId];
    if (message === undefined) return;
    const at = this.#now();
    this.#append(at, {
      case: 'message',
      value: create(MessageSchema, {...message, delivery: Delivery.REJECTED, rejection}),
    });
  }

  /** An update as the host decides it (`decideUpdate`): its facts committed, then its receipt. */
  #updateRun(commandId: string, update: UpdateRun, atMs: number): Receipt {
    const decision = decideUpdate(
      this.#state(this.#visible),
      update,
      this.#models?.allowedModels ?? [],
    );
    for (const body of decision.bodies) this.#append(atMs, body);
    const receipt = create(ReceiptSchema, {
      commandId,
      refusal: decision.refusal,
      runId: update.runId,
      model: decision.model,
      allowedModels: [...decision.allowedModels],
      sequence: BigInt(this.#log.length + 1),
    });
    this.#append(atMs, {case: 'receipt', value: receipt});
    return receipt;
  }

  /**
   * The lead's turn ended while a lead change waited: the change applies, as the host does in
   * `turnEnded` for the lead. Commits nothing for a run with no change waiting.
   */
  finishLeadTurn(runId: string): void {
    const at = this.#now();
    for (const body of applyPendingLead(this.#state(this.#visible), runId)) this.#append(at, body);
  }

  /** A start: refused when its lead's model is outside the allowed list, taken otherwise. */
  #startRun(commandId: string, start: StartRun): Receipt {
    const allowed = this.#models?.allowedModels ?? [];
    const model = start.lead?.model ?? '';
    if (allowed.length > 0 && !allowed.includes(model)) {
      return create(ReceiptSchema, {
        commandId,
        refusal: Refusal.MODEL_NOT_ALLOWED,
        model,
        allowedModels: [...allowed],
      });
    }
    this.#started.push(start);
    return create(ReceiptSchema, {commandId, runId: `run-started-${this.#started.length}`});
  }

  /**
   * The session of an agent being stopped closes, as the host hears once the stop reached it
   * (`sessionOver`, which ends a running turn with `endTurn`, then `rejectMail`):
   * - a turn still running ends interrupted: first every item still running in it fails, then the
   *   message that started it is settled -- out of attempts and rejected for good when the host has
   *   started as many turns for it as it ever will, else put back in line with the rest of its mail;
   * - the agent ends and its stop stage is cleared;
   * - everything still owed to the agent -- the turn's message, put back above, along with the rest
   *   of its mail -- is rejected together, in message-id order as the host sends them, as gone while
   *   its run is live, else as the run stopping.
   *
   * Only a stop ends a session here, so an agent that is not being stopped is a mistake in the test.
   */
  finishStop(agentId: string): void {
    const state = this.#state(this.#visible);
    const agent = state.agents[agentId];
    if (agent === undefined || FINAL.has(agent.status) || !STOPPING.has(agent.stopStage))
      throw new Error(`the fake host is not stopping an agent ${agentId}`);
    const at = this.#now();
    const running = Object.values(state.turns).filter(
      turn => turn.agentId === agentId && turn.status === TurnStatus.RUNNING,
    );
    for (const turn of running) this.#endInterruptedTurn(state, turn, at);
    this.#append(at, {
      case: 'agent',
      value: create(AgentSchema, {
        ...agent,
        status: AgentStatus.ENDED,
        stopStage: AgentStopStage.NONE,
        endedAt: timestampFromMs(at),
      }),
    });
    const run = state.runs[agent.runId];
    const gone =
      run?.status === RunStatus.STARTING || run?.status === RunStatus.RUNNING
        ? Rejection.AGENT_GONE
        : Rejection.RUN_STOPPING;
    const owed = Object.values(this.#state(this.#visible).messages)
      .filter(message => message.toAgentId === agentId && IN_TRANSIT.has(message.delivery))
      .sort((a, b) => (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
    for (const message of owed) this.reject(message.id, gone);
  }

  /**
   * A running turn ends interrupted (`endTurn`): first every item still running in it fails. The
   * message that started it is then out of attempts and rejected for good, once the host has
   * started as many turns for it as it ever will; otherwise it goes back in line, to be swept up
   * with the rest of the agent's mail once the session closes.
   */
  #endInterruptedTurn(state: OrchestratorState, turn: Turn, at: number): void {
    const endedAt = timestampFromMs(at);
    for (const item of Object.values(state.items))
      if (item.turnId === turn.id && item.status === ItemStatus.RUNNING)
        this.#append(at, {
          case: 'item',
          value: create(ItemSchema, {...item, status: ItemStatus.FAILED, at: endedAt}),
        });
    this.#append(at, {
      case: 'turn',
      value: create(TurnSchema, {...turn, status: TurnStatus.INTERRUPTED, endedAt}),
    });
    const trigger = state.messages[turn.triggerMessageId];
    if (trigger === undefined) return;
    if (trigger.attempts >= MAX_DELIVERY_ATTEMPTS)
      this.reject(trigger.id, Rejection.ATTEMPTS_EXHAUSTED);
    else
      this.#append(at, {
        case: 'message',
        value: create(MessageSchema, {...trigger, delivery: Delivery.QUEUED}),
      });
  }

  /**
   * A stop of one agent as the host decides it (`stopSubagent`, then `stopAgent`): the lead stops
   * only with its run, an agent that is over or whose session is already closing is not live, and
   * a stop already interrupting a turn is accepted and changes nothing. Otherwise an agent in a
   * turn is interrupted and an agent between turns has its session closed. The run's status is
   * not asked, as the host does not ask it.
   */
  #stopAgent(commandId: string, agentId: string, atMs: number): Receipt {
    const state = this.#state(this.#visible);
    const agent = state.agents[agentId];
    if (agent === undefined) return this.#refuse(commandId, Refusal.AGENT_NOT_FOUND, agentId, atMs);
    if (agent.role === AgentRole.LEAD)
      return this.#refuse(commandId, Refusal.AGENT_IS_LEAD, agentId, atMs);
    if (FINAL.has(agent.status) || agent.stopStage === AgentStopStage.CLOSING)
      return this.#refuse(commandId, Refusal.AGENT_NOT_LIVE, agentId, atMs);
    if (agent.stopStage !== AgentStopStage.INTERRUPTING) {
      const turning = Object.values(state.turns).some(
        turn => turn.agentId === agentId && turn.status === TurnStatus.RUNNING,
      );
      this.#append(atMs, {
        case: 'agent',
        value: create(AgentSchema, {
          ...agent,
          stopStage: turning ? AgentStopStage.INTERRUPTING : AgentStopStage.CLOSING,
        }),
      });
    }
    const receipt = create(ReceiptSchema, {
      commandId,
      runId: agent.runId,
      agentId: agent.id,
      sequence: BigInt(this.#log.length + 1),
    });
    this.#append(atMs, {case: 'receipt', value: receipt});
    return receipt;
  }

  #refuse(commandId: string, refusal: Refusal, agentId: string, atMs: number): Receipt {
    const runId = Object.keys(this.#state(this.#visible).runs)[0] ?? '';
    const receipt = create(ReceiptSchema, {
      commandId,
      refusal,
      runId,
      agentId,
    });
    this.#append(atMs, {case: 'receipt', value: receipt});
    return receipt;
  }

  #sendMessage(commandId: string, agentId: string, text: string, atMs: number): Receipt {
    const agent = this.#state(this.#visible).agents[agentId];
    if (agent === undefined) return this.#refuse(commandId, Refusal.AGENT_NOT_FOUND, agentId, atMs);
    if (FINAL.has(agent.status))
      return this.#refuse(commandId, Refusal.AGENT_NOT_LIVE, agentId, atMs);
    const steers = agent.capabilities?.steers === true;
    const delivery =
      agent.status !== AgentStatus.ACTIVE
        ? Delivery.NEW_TURN
        : steers
          ? Delivery.STEERED
          : Delivery.QUEUED;
    const message: Message = create(MessageSchema, {
      id: `msg-operator-${commandId}`,
      runId: agent.runId,
      toAgentId: agent.id,
      delivery,
      at: timestampFromMs(atMs),
      body: {case: 'text', value: text},
    });
    this.#append(atMs, {case: 'message', value: message});
    const receipt = create(ReceiptSchema, {
      commandId,
      runId: agent.runId,
      agentId: agent.id,
      messageId: message.id,
      delivery,
      sequence: BigInt(this.#log.length + 1),
    });
    this.#append(atMs, {case: 'receipt', value: receipt});
    this.setup?.afterMessage(agent, text, atMs);
    return receipt;
  }

  #append(atMs: number, body: RunChange['body']): void {
    // Appended facts come after everything the scenario holds back, so they commit last.
    const change = create(RunChangeSchema, {
      sequence: BigInt(this.#log.length + 1),
      runId: this.scenario.runId,
      at: timestampFromMs(atMs),
      body,
    });
    this.#log.push(change);
    this.#visible = this.#log.length;
    for (const watcher of this.#watchers) invokeCallback(watcher);
  }
}
