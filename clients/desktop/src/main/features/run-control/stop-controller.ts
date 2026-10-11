/**
 * The stop flows of every run the app has been asked to stop: one `StopState` per run, driven by
 * `stopStep`. It asks the user (`confirm`), runs the stop through the run's host, and learns that a
 * run has ended from the registry's pushes (`observeLive`) or from the connection (`runEnded`). No timers:
 * the caller reports what it sees and is told, through `changed`, when a view must be redrawn.
 */
import type {InstanceStopResult} from '@vibesys/backend-client/host-helper';
import type {HostKey} from '../../../common/hosts.js';
import {
  confirmText,
  IDLE,
  type StopEvent,
  type StopState,
  type StopStep,
  type StopView,
  stopKey,
  stopStep,
  stopView,
} from '../../../common/stop-run.js';

export interface StopDeps {
  /** Ask the host `host` to stop registry instance `instanceId`. */
  stop(host: HostKey, instanceId: string, force: boolean): Promise<InstanceStopResult>;
  /** Ask the user; resolves true on "yes". */
  confirm(text: string): Promise<boolean>;
  /** A view changed: send the views again. */
  changed(): void;
}

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
    await this.#stop(target, force, () =>
      this.#deps.confirm(confirmText({label: target.label, host: target.hostLabel, force})),
    );
  }

  /**
   * Stop `target` without asking: the user already decided (they cancelled the start that launched
   * it). The same flow as `request`, so the views show it and the registry ends it.
   */
  async stopConfirmed(target: StopTarget): Promise<void> {
    await this.#stop(target, false, async () => true);
  }

  async #stop(target: StopTarget, force: boolean, confirm: () => Promise<boolean>): Promise<void> {
    const key = stopKey(target.host, target.instanceId);
    if (this.#step(key, {type: 'request', force}).state.phase !== 'confirming') return;
    const yes = await confirm();
    const {effect} = this.#step(key, yes ? {type: 'confirm'} : {type: 'cancel'});
    if (effect === null) return;
    try {
      const result = await this.#deps.stop(target.host, target.instanceId, effect.force);
      this.#step(key, {type: 'result', outcome: result.outcome});
    } catch (error) {
      this.#step(key, {type: 'failed', message: (error as Error).message});
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
    const step = stopStep(this.#states.get(key) ?? IDLE, event);
    if (step.state.phase === 'idle') this.#states.delete(key);
    else this.#states.set(key, step.state);
    this.#deps.changed();
    return step;
  }
}
