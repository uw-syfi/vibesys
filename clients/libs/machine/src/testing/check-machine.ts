/**
 * Model-based checking of a control-state machine, the counterpart of `@vibesys/store/testing`'s
 * `checkModel` for reducers. Seeded random event sequences are folded through the pure
 * `transition(machine, snapshot, event)`, which returns the next snapshot and the actions the live
 * actor would execute. A ledger replays those actions the way the actor system would: an invoked
 * operation starts (`xstate.spawnChild`) and stops (`xstate.stopChild`), an `after` delay is armed
 * (a delayed `xstate.raise`) and cancelled (`xstate.cancel`). After every step:
 *
 * - every live operation and armed delay belongs to a state node that is active, so leaving a
 *   state is what cancels its work (no orphaned operation, no stray timer);
 * - a delay is never armed twice, and only a pending one is cancelled;
 * - a machine that reached a final state holds nothing;
 * - the model's own `invariants` hold, and its `also` properties pass.
 *
 * The model's `event` picks among plain events and the outcomes of what is live now: `done` and
 * `error` of a live operation, `fire` of an armed delay. A stopped operation never reports, so
 * there is no stale reply to generate: that is a property of the live actor, tested there.
 *
 * Every invoke must have an explicit `id`, which is how the ledger finds the state that owns it.
 * A failing seed is reported in the error message, to pin as an example.
 */
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {
  type AnyStateMachine,
  type EventFromLogic,
  type InputFrom,
  initialTransition,
  type SnapshotFrom,
  transition,
} from 'xstate';

/** An action as `transition` reports it. */
export interface MachineAction {
  readonly type: string;
  readonly params?: unknown;
}

interface ActionParams {
  readonly id?: string;
  readonly delay?: number;
  readonly event?: {readonly type: string};
  readonly sendId?: string;
  readonly actorRef?: {readonly id: string};
}

interface StateNodeLike {
  readonly id: string;
  readonly invoke: readonly {readonly id: string}[];
  readonly states: Readonly<Record<string, StateNodeLike>>;
}

/** Each invoke id of `machine`, mapped to the id of the state node that invokes it. */
function invokeOwners(machine: AnyStateMachine): ReadonlyMap<string, string> {
  const owners = new Map<string, string>();
  const visit = (node: StateNodeLike): void => {
    for (const invoke of node.invoke) owners.set(invoke.id, node.id);
    for (const child of Object.values(node.states)) visit(child);
  };
  visit(machine.root as unknown as StateNodeLike);
  return owners;
}

/** What the live actor system would hold after the actions so far. */
class Ledger {
  readonly children = new Set<string>();
  readonly timers = new Map<string, {readonly type: string}>();
  readonly problems: string[] = [];

  apply(event: {readonly type: string}, actions: readonly MachineAction[]): void {
    const settled = /^xstate\.(?:done|error)\.actor\.(.+)$/.exec(event.type)?.[1];
    if (settled !== undefined) this.children.delete(settled);
    const fired = this.timers.delete(event.type) ? event.type : null;
    for (const action of actions) this.#apply(action, fired);
  }

  #apply(action: MachineAction, fired: string | null): void {
    const params = (action.params ?? {}) as ActionParams;
    if (action.type === 'xstate.spawnChild') this.#start(params.id);
    else if (action.type === 'xstate.stopChild') {
      this.children.delete(params.actorRef?.id ?? params.id ?? '');
    } else if (action.type === 'xstate.raise') this.#arm(params);
    else if (action.type === 'xstate.cancel') this.#cancel(params.sendId ?? '', fired);
  }

  #start(id: string | undefined): void {
    if (id === undefined) return;
    if (this.children.has(id)) this.problems.push(`started live ${id} again`);
    this.children.add(id);
  }

  #arm({delay, id, event}: ActionParams): void {
    if (delay === undefined || id === undefined) return;
    if (this.timers.has(id)) this.problems.push(`armed live delay ${id} again`);
    this.timers.set(id, event ?? {type: id});
  }

  #cancel(id: string, fired: string | null): void {
    // Leaving the state whose delay just fired cancels it too: a no-op, not a problem.
    if (!this.timers.delete(id) && id !== fired) this.problems.push(`cancelled ${id}, not armed`);
  }
}

/** What is live now, and the events that settle it. */
export interface Live {
  /** The ids of the operations running now. */
  readonly operations: readonly string[];
  /** The ids of the delays armed now. */
  readonly delays: readonly string[];
  done(operation: string, output?: unknown): {readonly type: string};
  error(operation: string, error: unknown): {readonly type: string};
  fire(delay: string): {readonly type: string};
}

export interface MachineModel<M extends AnyStateMachine> {
  readonly machine: M;
  readonly input: InputFrom<M>;
  /** One random event; null skips the step (for example, nothing live to settle). */
  readonly event: (
    random: Seeded,
    live: Live,
    snapshot: SnapshotFrom<M>,
  ) => {readonly type: string} | null;
  /**
   * What else must hold after every step, of the snapshot and of the work live beside it; returns
   * the violations (empty when none).
   */
  readonly invariants?: (snapshot: SnapshotFrom<M>, live: Live) => string[];
  /** More properties of one step; throws when one fails. */
  readonly also?: (
    before: SnapshotFrom<M>,
    event: {readonly type: string},
    after: SnapshotFrom<M>,
    actions: readonly MachineAction[],
  ) => void;
}

function activeIds(snapshot: unknown): Set<string> {
  return new Set((snapshot as {_nodes: {id: string}[]})._nodes.map(node => node.id));
}

function holdProblems(
  snapshot: unknown,
  ledger: Ledger,
  owners: ReadonlyMap<string, string>,
): string[] {
  const active = activeIds(snapshot);
  const problems: string[] = [];
  for (const id of ledger.children) {
    const owner = owners.get(id);
    if (owner === undefined) problems.push(`operation ${id} has no explicit invoke id`);
    else if (!active.has(owner)) problems.push(`operation ${id} outlived its state ${owner}`);
  }
  for (const id of ledger.timers.keys()) {
    const owner = /^xstate\.after\.[^.]+\.(.+)$/.exec(id)?.[1];
    if (owner === undefined || !active.has(owner)) problems.push(`delay ${id} outlived its state`);
  }
  const {status} = snapshot as {status: string};
  if (status === 'done' && (ledger.children.size > 0 || ledger.timers.size > 0)) {
    problems.push('reached a final state still holding work');
  }
  return problems;
}

function liveOf(ledger: Ledger): Live {
  return {
    operations: [...ledger.children],
    delays: [...ledger.timers.keys()],
    done: (operation, output) => ({type: `xstate.done.actor.${operation}`, output}),
    error: (operation, error) => ({type: `xstate.error.actor.${operation}`, error}),
    fire: delay => ledger.timers.get(delay) ?? {type: delay},
  };
}

function checkSeed<M extends AnyStateMachine>(
  model: MachineModel<M>,
  owners: ReadonlyMap<string, string>,
  seed: number,
  steps: number,
): void {
  const random = new Seeded(seed);
  const ledger = new Ledger();
  const [initial, initialActions] = initialTransition(model.machine, model.input);
  ledger.apply({type: 'xstate.init'}, initialActions as readonly MachineAction[]);
  let snapshot = initial as SnapshotFrom<M>;
  for (let step = 0; step < steps; step += 1) {
    const event = model.event(random, liveOf(ledger), snapshot);
    if (event === null) continue;
    const before = snapshot;
    const [next, actions] = transition(model.machine, snapshot, event as EventFromLogic<M>);
    snapshot = next as SnapshotFrom<M>;
    ledger.apply(event, actions as readonly MachineAction[]);
    const problems = [
      ...ledger.problems.splice(0),
      ...holdProblems(snapshot, ledger, owners),
      ...(model.invariants?.(snapshot, liveOf(ledger)) ?? []),
    ];
    try {
      model.also?.(before, event, snapshot, actions as readonly MachineAction[]);
    } catch (error) {
      problems.push(error instanceof Error ? error.message : String(error));
    }
    if (problems.length > 0) {
      throw new Error(
        `checkMachine seed ${seed}, step ${step} (${event.type}): ${problems.join('; ')}`,
      );
    }
  }
}

export function checkMachine<M extends AnyStateMachine>(model: MachineModel<M>, steps = 40): void {
  const owners = invokeOwners(model.machine);
  for (const seed of SEEDS) checkSeed(model, owners, seed, steps);
}
