import type {ScheduleTimeout} from '@vibesys/machine';
import {observeRun, transcriptStreamFor} from '@vibesys/run-skeleton';
import {RunLiveness, type RunWriterFacts} from '@vibesys/runs/intents';
import {useEffect, useMemo, useRef, useState} from 'react';
import {getInitialSnapshot} from 'xstate';
import {
  type GatewayRunContext,
  gatewayRunMachine,
  loadView,
  logStreamToFollow,
  paintedSkeleton,
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
  const {load, progress, ui, text, replies, transcripts} = snapshot.context;
  const skeleton = paintedSkeleton(load);
  const {hint, writer} = useWriterFacts(snapshot.context);
  const at = ui.view.at === 'live' ? (skeleton?.throughSeq ?? 0n) : BigInt(ui.view.at);
  const epoch = skeleton?.epochs.filter(item => item.seq <= at).at(-1)?.epoch;
  const objective = text[epoch?.run?.objective?.sha256 ?? ''] ?? '';
  const wholeHash = skeleton?.epochs.at(-1)?.epoch?.run?.objective?.sha256 ?? '';
  const wholeText = text[wholeHash] ?? '';
  const wholeRead = useMemo(
    () =>
      project(() =>
        skeleton
          ? projectSkeleton(skeleton, skeleton.throughSeq ?? 0n, wholeText, {
              head: hint?.head,
              progress,
              content: {text, replies, transcripts},
            })
          : null,
      ),
    [skeleton, wholeText, hint?.head, progress, text, replies, transcripts],
  );
  const modelRead = useMemo(
    () =>
      at === skeleton?.throughSeq
        ? wholeRead
        : project(() =>
            skeleton
              ? projectSkeleton(skeleton, at, objective, {
                  content: {text, replies, transcripts},
                  progress,
                })
              : null,
          ),
    [skeleton, at, objective, wholeRead, text, replies, transcripts, progress],
  );
  const {value: whole} = wholeRead;
  const {value: model} = modelRead;
  const stateRead = useMemo(
    () =>
      project(() =>
        skeleton && whole
          ? skeletonScreenState(seed, skeleton, whole, ui, hint, {text, replies, transcripts})
          : null,
      ),
    [skeleton, whole, seed, ui, hint, text, replies, transcripts],
  );
  const {value: state} = stateRead;
  const projectionError = wholeRead.error ?? modelRead.error ?? stateRead.error;
  const follow = state && model ? logStreamToFollow(state, model, route) : null;
  useFollowedContent(actor, follow, skeleton, route.step);
  return {
    snapshot,
    writer,
    load: loadView(snapshot.context),
    retry: () => actor?.send({type: 'retry'}),
    refreshWriter: () => actor?.send({type: 'refresh-writer'}),
    projectionError,
    route,
    zoom,
    model,
    whole,
    state,
    dispatch: (action: import('../../state/index.js').RunPageAction) =>
      actor?.send({type: 'ui', action}),
  };
}

/** The one reading of the writer: the header and the controls both project these facts. */
function useWriterFacts({load, head, hint: liveHint, fresh}: GatewayRunContext) {
  return useMemo(() => {
    const hint = head
      ? {
          epoch: head.epoch,
          value: liveHint?.epoch === head.epoch ? liveHint.value : RunLiveness.UNSPECIFIED,
          head,
        }
      : liveHint;
    const observed = load ? observeRun(load.skeleton, hint) : undefined;
    const observation = observed?.epoch
      ? {
          epoch: BigInt(observed.epoch),
          ...(observed.lifecycle ? {lifecycle: observed.lifecycle} : {}),
        }
      : undefined;
    const writer: RunWriterFacts = {
      observation,
      liveness: hint
        ? {epoch: BigInt(hint.epoch), value: hint.value}
        : {epoch: 0n, value: RunLiveness.UNSPECIFIED},
      fresh,
    };
    return {hint, writer};
  }, [load, head, liveHint, fresh]);
}

function useFollowedContent(
  actor: ReturnType<typeof startGatewayRun> | null,
  follow: string | null,
  skeleton: import('@vibesys/run-skeleton').Skeleton | undefined,
  step: string | null,
): void {
  useEffect(() => {
    if (follow !== null)
      actor?.send({type: 'ui', action: {type: 'select-stream', streamId: follow}});
  }, [actor, follow]);
  const transcriptId = transcriptStreamFor(skeleton, step);
  const transcriptSpan = transcriptId ? step : null;
  useEffect(() => {
    actor?.send({
      type: 'follow-transcript',
      selection:
        transcriptId && transcriptSpan ? {streamId: transcriptId, spanId: transcriptSpan} : null,
    });
  }, [actor, transcriptId, transcriptSpan]);
}

function project<T>(read: () => T): {value: T | null; error: string | null} {
  try {
    return {value: read(), error: null};
  } catch {
    return {value: null, error: 'Invalid records'};
  }
}
