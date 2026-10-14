/** The machine owns every load, subscription, and playback delay; the shell injects their I/O. */
import {actorStore, type MachineClock} from '@vibesys/machine';
import type {Store} from '@vibesys/store';
import {createActor, fromCallback, fromPromise} from 'xstate';
import {type CampaignAction, type CampaignInput, campaignMachine} from '../../state/campaign.js';
import {projectCampaign} from '../../state/campaign-projection.js';
import type {CampaignRecord} from '../../state/campaign-record.js';
import type {CampaignStream} from '../../state/campaign-stream.js';
import type {CampaignViewModel} from '../../state/campaign-view.js';

export interface CampaignState {
  readonly scenario: CampaignRecord | null;
  readonly campaign: CampaignViewModel | null;
  readonly error: string | null;
  readonly loading: boolean;
}

export interface CampaignStore extends Store<CampaignState, CampaignAction> {
  close(): void;
}

export interface CampaignStoreOptions {
  /** Injected XState clock; tests advance playback without waiting for real time. */
  readonly clock?: MachineClock;
}

/** The app validates the document at its backend boundary and honors cancellation. */
export type LoadCampaign = (signal: AbortSignal) => Promise<CampaignRecord>;

function createCampaignStore(
  machine: typeof campaignMachine,
  input: CampaignInput,
  options: CampaignStoreOptions,
): CampaignStore {
  const actor = createActor(machine, {input, ...options});
  const send = (action: CampaignAction): void => actor.send(action);
  const commands = {
    setMetric: (id: string) => send({type: 'metric', id}),
    setPlaying: (playing: boolean) => send({type: 'play', playing}),
    setPointIndex: (index: number) => send({type: 'point', index}),
    setSelectedAgentId: (id: string | null) => send({type: 'agent', id}),
    setSelectedArchitectureComponentId: (id: string | null) =>
      send({type: 'architecture-component', id}),
    setSelectedWorkstreamId: (id: string | null) => send({type: 'workstream', id}),
    setView: (view: CampaignViewModel['view']) => send({type: 'view', view}),
    setTimelineMode: (mode: CampaignViewModel['timelineMode']) => send({type: 'timeline', mode}),
    setWorkstreamLayout: (layout: CampaignViewModel['workstreamLayout']) =>
      send({type: 'layout', layout}),
    setWorkstreamSort: (sort: CampaignViewModel['workstreamSort']) => send({type: 'sort', sort}),
  };
  const store = actorStore(actor, snapshot => {
    const projected = projectCampaign(snapshot.context);
    return {
      ...projected,
      campaign: projected.campaign === null ? null : {...projected.campaign, ...commands},
      error: snapshot.context.error,
      loading: snapshot.context.loading,
    };
  });
  actor.start();
  return {
    ...store,
    close: () => {
      send({type: 'close'});
      actor.stop();
    },
  };
}

export function createCampaignHistoryStore(
  record: CampaignRecord,
  options: CampaignStoreOptions = {},
): CampaignStore {
  return createCampaignStore(campaignMachine, {mode: 'history', record}, options);
}

/** Start or retry with `dispatch({type: 'load'})`; close aborts any unfinished load. */
export function createCampaignReplayStore(
  load: LoadCampaign,
  options: CampaignStoreOptions = {},
): CampaignStore {
  const machine = campaignMachine.provide({
    actors: {load: fromPromise<CampaignRecord, void>(({signal}) => load(signal))},
  });
  return createCampaignStore(machine, {mode: 'replay'}, options);
}

/** Starts receiving frames immediately; close releases the subscription once. */
export function createLiveCampaignStore(
  stream: CampaignStream,
  options: CampaignStoreOptions = {},
): CampaignStore {
  const machine = campaignMachine.provide({
    actors: {
      stream: fromCallback<CampaignAction>(({sendBack}) =>
        stream.subscribe(
          frame => sendBack({type: 'frame', frame}),
          error => sendBack({type: 'stream-error', error: error.message}),
        ),
      ),
    },
  });
  return createCampaignStore(machine, {mode: 'live'}, options);
}
