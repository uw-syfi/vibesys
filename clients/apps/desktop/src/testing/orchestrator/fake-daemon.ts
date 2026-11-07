/**
 * An in-memory daemon over a scenario: the part of the contract a screen reads. It commits events
 * in order (`commit` reveals more of the scenario, `dispatch` appends the facts a command causes),
 * answers a snapshot, events after a sequence, and pages of items, and notifies watchers.
 */
import {create} from '@bufbuild/protobuf';
import {timestampFromMs} from '@bufbuild/protobuf/wkt';
import {
  AgentStatus,
  type Command,
  Delivery,
  type Item,
  type ItemsPage,
  ItemsPageSchema,
  type Message,
  MessageSchema,
  oneofIs,
  type Plan,
  type Receipt,
  ReceiptSchema,
  Refusal,
  Rejection,
  type RunChange,
  RunChangeSchema,
  type RunCost,
  type RunsSnapshot,
  RunsSnapshotSchema,
} from '@vibesys/orchestrator-proto';
import {applyChange} from '../../state/orchestrator/fold.js';
import {emptyState, type OrchestratorState} from '../../state/orchestrator/state.js';
import {invokeCallback} from '../callback-errors.js';
import type {Scenario} from './scenario.js';
import {searchFixture} from './search.js';

const FINAL = new Set([AgentStatus.ENDED, AgentStatus.FAILED, AgentStatus.LOST]);

export class FakeDaemon {
  readonly #log: RunChange[];
  #visible: number;
  readonly #receipts = new Map<string, Receipt>();
  readonly #watchers = new Set<() => void>();
  readonly #costs = new Map<string, RunCost>();

  constructor(
    readonly scenario: Scenario,
    visible: number = scenario.changes.length,
  ) {
    this.#log = [...scenario.changes];
    this.#visible = visible;
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

  /** Call `listener` whenever events are committed; returns how to stop. */
  watch(listener: () => void): () => void {
    this.#watchers.add(listener);
    return () => void this.#watchers.delete(listener);
  }

  #state(upTo: number): OrchestratorState {
    return this.#log.slice(0, upTo).reduce(applyChange, emptyState);
  }

  snapshot(): RunsSnapshot {
    const state = this.#state(this.#visible);
    return create(RunsSnapshotSchema, {
      runs: Object.values(state.runs),
      agents: Object.values(state.agents),
      turns: Object.values(state.turns),
      messages: Object.values(state.messages),
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
    const items = all.slice(0, pageSize);
    const oldest = items[items.length - 1];
    return create(ItemsPageSchema, {
      items,
      sequence: this.sequence,
      nextBeforeOrdinal: oldest !== undefined && oldest.ordinal > 1 ? oldest.ordinal : 0,
    });
  }

  /** Carry out a command: the same id answers with the same receipt. */
  dispatch(command: Command): Receipt {
    const seen = this.#receipts.get(command.id);
    if (seen !== undefined) return seen;
    const at = Date.parse('2026-11-01T18:00:00Z') + this.#log.length * 1000;
    const receipt = oneofIs(command.body, 'sendMessage', {
      startRun: true,
      stopRun: true,
      spawnAgent: true,
      stopAgent: true,
      sendMessage: true,
      postHandoff: true,
      postReport: true,
    })
      ? this.#sendMessage(command.id, command.body.value.agentId, command.body.value.text, at)
      : this.#refuse(command.id, Refusal.NOT_PERMITTED, '', at);
    this.#receipts.set(command.id, receipt);
    return receipt;
  }

  /**
   * The host gives up delivering a message it had accepted: the stored message becomes rejected
   * and says why, which is how a person finds out a receipt's first answer did not hold.
   */
  reject(messageId: string, rejection: Rejection = Rejection.AGENT_GONE): void {
    const message = this.#state(this.#visible).messages[messageId];
    if (message === undefined) return;
    const at = Date.parse('2026-11-01T18:00:00Z') + this.#log.length * 1000;
    this.#append(at, {
      case: 'message',
      value: create(MessageSchema, {...message, delivery: Delivery.REJECTED, rejection}),
    });
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
