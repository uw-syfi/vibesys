import type {ScheduleTimeout} from '@vibesys/machine';
import {useEffect, useMemo, useRef, useState} from 'react';
import {getInitialSnapshot} from 'xstate';
import {
  gatewayRunMachine,
  logStreamToFollow,
  projectSkeleton,
  type RunPageSeed,
  skeletonScreenState,
} from '../../state/index.js';
import {type GatewayRunPorts, startGatewayRun} from './gateway-run.js';
import {type RunPageAddress, useZoom} from './use-zoom.js';

export function useGatewayRun(options: {
  seed: RunPageSeed;
  ports: GatewayRunPorts;
  schedule: ScheduleTimeout;
  address: RunPageAddress;
}) {
  const {seed, ports, schedule, address} = options;
  const [snapshot, setSnapshot] = useState(() => getInitialSnapshot(gatewayRunMachine, {seed}));
  const [actor, setActor] = useState<ReturnType<typeof startGatewayRun> | null>(null);
  const latestSeed = useRef(seed);
  const {host, runId} = seed.identity;
  useEffect(() => {
    latestSeed.current = seed;
  }, [seed]);
  useEffect(() => {
    const started = startGatewayRun(ports, schedule, {
      seed: {...latestSeed.current, identity: {host, runId}},
    });
    const subscription = started.subscribe(setSnapshot);
    setActor(started);
    setSnapshot(started.getSnapshot());
    return () => {
      subscription.unsubscribe();
      started.stop();
    };
  }, [ports, schedule, host, runId]);
  const {route, zoom} = useZoom(`${seed.identity.host}/${seed.identity.runId}`, address);
  const {load, ui, text, hint} = snapshot.context;
  const skeleton = load?.complete ? load.skeleton : undefined;
  const at = ui.view.at === 'live' ? (skeleton?.throughSeq ?? 0n) : BigInt(ui.view.at);
  const epoch = skeleton?.epochs.filter(item => item.seq <= at).at(-1)?.epoch;
  const objective = text[epoch?.run?.objective?.sha256 ?? ''] ?? '';
  const wholeHash = skeleton?.epochs.at(-1)?.epoch?.run?.objective?.sha256 ?? '';
  const wholeText = text[wholeHash] ?? '';
  const wholeRead = useMemo(
    () =>
      project(() =>
        skeleton ? projectSkeleton(skeleton, skeleton.throughSeq ?? 0n, wholeText) : null,
      ),
    [skeleton, wholeText],
  );
  const modelRead = useMemo(
    () =>
      at === skeleton?.throughSeq
        ? wholeRead
        : project(() => (skeleton ? projectSkeleton(skeleton, at, objective) : null)),
    [skeleton, at, objective, wholeRead],
  );
  const {value: whole} = wholeRead;
  const {value: model} = modelRead;
  const stateRead = useMemo(
    () =>
      project(() =>
        skeleton && whole ? skeletonScreenState(seed, skeleton, whole, ui, hint) : null,
      ),
    [skeleton, whole, seed, ui, hint],
  );
  const {value: state} = stateRead;
  const projectionError = wholeRead.error ?? modelRead.error ?? stateRead.error;
  const follow = state && model ? logStreamToFollow(state, model, route) : null;
  useEffect(() => {
    if (follow !== null)
      actor?.send({type: 'ui', action: {type: 'select-stream', streamId: follow}});
  }, [actor, follow]);
  return {
    snapshot,
    projectionError,
    route,
    zoom,
    model,
    whole,
    state,
    dispatch: (action: import('../../state/index.js').RunPageAction) =>
      actor?.send({type: 'ui', action}),
    toggle: (enabled: boolean) => actor?.send({type: 'toggle', enabled}),
  };
}

function project<T>(read: () => T): {value: T | null; error: string | null} {
  try {
    return {value: read(), error: null};
  } catch {
    return {value: null, error: 'Invalid records'};
  }
}
