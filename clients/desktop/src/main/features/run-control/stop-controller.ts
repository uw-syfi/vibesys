/**
 * The stop flows of every run the app has been asked to stop: one `StopState` per run, driven by
 * `stopStep`. It asks the user (`confirm`), runs the stop through the run's host, and learns that a
 * run has ended from the registry's pushes (`observeLive`) or from the connection (`runEnded`). No timers:
 * the caller reports what it sees and is told, through `changed`, when a view must be redrawn.
 */
import type {InstanceStopResult} from '@vibesys/contracts/host-helper';
import {
  confirmText,
  type HostKey,
  STOP_IDLE,
  type StopEvent,
  type StopState,
  type StopStep,
  type StopView,
  stopKey,
  stopStep,
  stopView,
} from '@vibesys/entities';

export interface StopDeps {
  /** Ask the host to stop the instance, distinguishing withdrawal from answer and failure. */
  stop(
    host: HostKey,
    instanceId: string,
    force: boolean,
    current: () => boolean,
  ): Promise<StopCommandResult>;
  /** Ask the user; resolves true on "yes". */
  confirm(text: string): Promise<boolean>;
  /** A view changed: send the views again. */
  changed(): void;
}

type StopCommandResult =
  | {readonly kind: 'not-written'}
  | {readonly kind: 'answered'; readonly result: InstanceStopResult}
  | {readonly kind: 'failed'; readonly written: boolean; readonly message: string};

type StopResult =
  | {readonly kind: 'complete'}
  | {readonly kind: 'withdrawn'}
  | {readonly kind: 'failed'; readonly message: string};

/** The run a flow belongs to, with the words the dialogs use for it. */
export interface StopTarget {
  readonly host: HostKey;
  readonly hostLabel: string;
  readonly instanceId: string;
  /** What the dialogs call the run: its task, run id, or instance id. */
  readonly label: string;
}

export class StopController {
  readonly #deps: StopDeps;
  readonly #states = new Map<string, StopState>();

  constructor(deps: StopDeps) {
    this.#deps = deps;
  }

  /** The flows that are past idle, by `stopKey`. */
  views(): Record<string, StopView> {
    const views: Record<string, StopView> = {};
    for (const [key, state] of this.#states) {
      if (state.phase !== 'idle') views[key] = stopView(state);
    }
    return views;
  }

  /**
   * Stop `target` after asking the user; `force` is the second, stronger stop offered after a
   * failed one. Resolves once the host has answered (an accepted stop is still ending then).
   */
  async request(target: StopTarget, force: boolean): Promise<void> {
    await this.#stop(
      target,
      force,
      () => this.#deps.confirm(confirmText({label: target.label, host: target.hostLabel, force})),
      () => true,
    );
  }

  /**
   * Stop `target` without asking because the user already cancelled or superseded its launch.
   * Distinguishes a stop withdrawn before its write from completion and a recorded failure.
   */
  async stopConfirmed(
    target: StopTarget,
    current: () => boolean = () => true,
  ): Promise<StopResult> {
    return this.#stop(target, false, async () => true, current);
  }

  async #stop(
    target: StopTarget,
    force: boolean,
    confirm: () => Promise<boolean>,
    current: () => boolean,
  ): Promise<StopResult> {
    if (!current()) return {kind: 'withdrawn'};
    const key = stopKey(target.host, target.instanceId);
    if (this.#step(key, {type: 'request', force}).state.phase !== 'confirming')
      return {kind: 'complete'};
    const yes = await confirm();
    if (!current()) {
      this.#step(key, {type: 'abandon'});
      return {kind: 'withdrawn'};
    }
    const {effect} = this.#step(key, yes ? {type: 'confirm'} : {type: 'cancel'});
    if (effect === null) return {kind: 'complete'};
    try {
      const command = await this.#deps.stop(target.host, target.instanceId, effect.force, current);
      switch (command.kind) {
        case 'not-written':
          this.#step(key, {type: 'abandon'});
          return {kind: 'withdrawn'};
        case 'failed':
          this.#step(key, {type: 'failed', message: command.message});
          return {kind: 'failed', message: command.message};
        case 'answered': {
          const {state} = this.#step(key, {type: 'result', outcome: command.result.outcome});
          return state.phase === 'error'
            ? {kind: 'failed', message: state.message}
            : {kind: 'complete'};
        }
      }
    } catch (error) {
      const message = (error as Error).message;
      this.#step(key, {type: 'failed', message});
      return {kind: 'failed', message};
    }
  }

  /** Every accepted stop of `host` whose server `live` (the registry's live ids) no longer lists is over. */
  observeLive(host: HostKey, live: ReadonlySet<string>): void {
    for (const [key, state] of [...this.#states]) {
      const [stopHost, instanceId = ''] = key.split('\n');
      if (stopHost !== host || live.has(instanceId)) continue;
      if (state.phase === 'stopping' && state.accepted) this.#step(key, {type: 'ended'});
    }
  }

  /** The run's connection ended with "run ended". */
  runEnded(host: HostKey, instanceId: string): void {
    this.#step(stopKey(host, instanceId), {type: 'ended'});
  }

  #step(key: string, event: StopEvent): StopStep {
    const step = stopStep(this.#states.get(key) ?? STOP_IDLE, event);
    if (step.state.phase === 'idle') this.#states.delete(key);
    else this.#states.set(key, step.state);
    this.#deps.changed();
    return step;
  }
}
