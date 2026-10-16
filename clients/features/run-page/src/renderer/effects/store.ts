/**
 * The Run screen's shell: its store over the ports the app provides. It performs the reducer's
 * requests (commands through the app's run-control paths, the force-stop bound on the injected
 * clock, the stream listing) and keeps one log follower per stream the screen needs
 * (`followedStreams`), feeding each follower's view back as an action. `dispose` stops every
 * follower and cancels the bound; a reply that arrives later is dropped by its token.
 */
import {type LogListOutcome, type LogTransport, startLogFollower} from '@vibesys/logs';
import type {ScheduleTimeout} from '@vibesys/machine';
import {createStore, type Store} from '@vibesys/store';
import {
  followedStreams,
  type RunPageAction,
  type RunPageRequest,
  type RunPageSeed,
  type RunPageState,
  runPageInitial,
  runPageStep,
} from '../../state/index.js';

/** The run's logs as the app reads them. */
export interface RunPageLogs {
  list(): Promise<LogListOutcome>;
  readonly transport: LogTransport;
}

/** What the Run screen asks of the app. */
export interface RunPagePorts {
  /** Stop the run (`force`: without waiting for a safe point); resolves false when it could not. */
  stop(force: boolean): Promise<boolean>;
  pause(): void;
  resume(): void;
  /** Resume an ended run that its end record says can resume. */
  resumeEnded(fromRound: number | null): void;
  openWindow(): void;
  archive(): void;
  runAgain(): void;
  /** The run's log streams, when the app can read them (`unavailable.logs` says when not). */
  readonly logs: RunPageLogs | null;
}

export interface RunPageStore extends Store<RunPageState, RunPageAction> {
  dispose(): void;
}

type Dispatch = (action: RunPageAction) => void;

/** One follower per stream `followedStreams` names, started and stopped as the state changes. */
class Followers {
  readonly #followers = new Map<string, {stop(): void}>();

  constructor(
    private readonly logs: RunPageLogs,
    private readonly schedule: ScheduleTimeout,
    private readonly dispatch: Dispatch,
  ) {}

  reconcile(state: RunPageState): void {
    const instanceId = state.record.kind === 'unlisted' ? null : state.record.run.id;
    const wanted = new Set(instanceId === null ? [] : followedStreams(state));
    for (const [streamId, follower] of this.#followers) {
      if (wanted.has(streamId)) continue;
      follower.stop();
      this.#followers.delete(streamId);
    }
    if (instanceId === null) return;
    for (const streamId of wanted) {
      if (!this.#followers.has(streamId)) this.#start(instanceId, streamId);
    }
  }

  stop(): void {
    for (const follower of this.#followers.values()) follower.stop();
    this.#followers.clear();
  }

  #start(instanceId: string, streamId: string): void {
    const actor = startLogFollower(this.logs.transport, this.schedule, {instanceId, streamId});
    let last = actor.getSnapshot().context.view;
    actor.subscribe(snapshot => {
      const {view} = snapshot.context;
      if (view === last) return;
      last = view;
      this.dispatch({type: 'log-view', streamId, view});
    });
    this.#followers.set(streamId, actor);
  }
}

function performCommand(ports: RunPagePorts, request: RunPageRequest): void {
  switch (request.kind) {
    case 'pause':
      ports.pause();
      return;
    case 'resume':
      ports.resume();
      return;
    case 'resume-ended':
      ports.resumeEnded(request.fromRound);
      return;
    case 'open-window':
      ports.openWindow();
      return;
    case 'archive':
      ports.archive();
      return;
    case 'run-again':
      ports.runAgain();
      return;
    default:
      return;
  }
}

export function createRunPageStore(
  seed: RunPageSeed,
  ports: RunPagePorts,
  schedule: ScheduleTimeout,
): RunPageStore {
  const cancels = new Set<() => void>();
  let disposed = false;
  const live =
    (dispatch: Dispatch): Dispatch =>
    action => {
      if (!disposed) dispatch(action);
    };
  const perform = (request: RunPageRequest, raw: Dispatch): void => {
    const dispatch = live(raw);
    if (disposed) return;
    switch (request.kind) {
      case 'stop':
        void ports.stop(request.force).then(
          stopped => (stopped ? undefined : dispatch({type: 'stop-failed'})),
          () => dispatch({type: 'stop-failed'}),
        );
        return;
      case 'arm-bound': {
        const cancel = schedule(() => {
          cancels.delete(cancel);
          dispatch({type: 'bound-elapsed', token: request.token});
        }, request.ms);
        cancels.add(cancel);
        return;
      }
      case 'list-logs': {
        const settle = (outcome: LogListOutcome): void =>
          dispatch({type: 'streams', token: request.token, outcome});
        void ports.logs
          ?.list()
          .then(settle, (error: unknown) =>
            settle({ok: false, failure: {code: 'transport', message: String(error)}}),
          );
        return;
      }
      default:
        performCommand(ports, request);
    }
  };
  const store = createStore<RunPageState, RunPageAction, RunPageRequest>(
    runPageStep,
    runPageInitial(seed),
    perform,
  );
  const followers =
    ports.logs === null ? null : new Followers(ports.logs, schedule, live(store.dispatch));
  const unsubscribe = store.subscribe(() => followers?.reconcile(store.getState()));
  return {
    ...store,
    dispose: () => {
      disposed = true;
      unsubscribe();
      followers?.stop();
      for (const cancel of cancels) cancel();
      cancels.clear();
    },
  };
}
