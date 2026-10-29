import type {HostKey, RecordedProjectRoot} from '@vibesys/hosts';
import type {GatewayBridge} from '@vibesys/ipc';
import type {ScheduleTimeout} from '@vibesys/machine';
import {newRequestId} from '@vibesys/request-identity';
import {
  type RunIntentAction,
  type RunWriterFacts,
  runIntent,
  runReference,
  writerControls,
} from '@vibesys/runs/intents';
import {type JSX, useEffect, useRef, useState} from 'react';
import type {SnapshotFrom} from 'xstate';
import {historyEpoch, intentRows, type runIntentsMachine} from '../state/index.js';
import {intentsActor, type RunRecords} from './effects/controls.js';
import {useRelaunch, useWriterRecovery} from './relaunch.js';
import {IntentControls} from './ui/controls.js';

/** Mount with a key of host/runId. enabled says whether the controls are active. */
interface RunIntentsProps {
  readonly bridge: GatewayBridge;
  readonly host: HostKey;
  readonly projectRoot: RecordedProjectRoot | undefined;
  readonly runId: string;
  readonly enabled: boolean;
  readonly schedule: ScheduleTimeout;
  /** The page's one reading of the writer, shared with its header. */
  readonly writer: RunWriterFacts;
  /** The run's one reader, whose records feed the intent history. */
  readonly records: RunRecords;
  readonly refreshWriter?: () => void;
}

function useIntentActor(props: RunIntentsProps) {
  const [snapshot, setSnapshot] = useState<SnapshotFrom<typeof runIntentsMachine> | null>(null);
  const current = useRef<ReturnType<typeof intentsActor> | null>(null);
  useEffect(() => {
    const actor = intentsActor({
      bridge: props.bridge,
      host: props.host,
      runId: props.runId,
      enabled: props.enabled,
      schedule: props.schedule,
      records: props.records,
    });
    current.current = actor;
    const subscription = actor.subscribe(setSnapshot);
    actor.start();
    return () => {
      current.current = null;
      subscription.unsubscribe();
      actor.stop();
    };
  }, [props.bridge, props.host, props.runId, props.enabled, props.schedule, props.records]);
  return {snapshot, current};
}

export function useRunIntents(props: RunIntentsProps) {
  const {snapshot, current} = useIntentActor(props);
  const [text, setText] = useState('');
  const [guidanceOpen, showGuidance] = useState(false);
  const projectRoot = props.projectRoot;
  const committed = snapshot ? historyEpoch(snapshot.context) : 0n;
  const writerFacts = props.writer;
  const machineRelaunch = useRelaunch({
    bridge: props.bridge,
    host: props.host,
    projectRoot: projectRoot ?? '',
    runId: props.runId,
    enabled: props.enabled && projectRoot !== undefined,
    writer: writerFacts,
    historyEpoch: committed,
    schedule: props.schedule,
  });
  const busy = snapshot?.matches({active: 'sending'}) ?? false;
  const sendRequest = (
    request: Parameters<NonNullable<typeof current.current>['send']>[0] & {type: 'send'},
  ) => current.current?.send(request);
  const acknowledged = machineRelaunch?.appliedEpoch ?? 0n;
  const writer = writerControls({
    fresh: writerFacts.fresh,
    reference: projectRoot === undefined ? undefined : runReference(projectRoot, props.runId),
    observation: writerFacts.observation,
    liveness: writerFacts.liveness,
    historyEpoch: committed,
    acknowledgedEpoch: acknowledged,
  });
  // Before the page has a lifecycle to observe its own loader owns the retries; re-reading from
  // here would restart that loader's bounded backoff for ever.
  useWriterRecovery(
    props.schedule,
    props.enabled && writerFacts.observation !== undefined && writer.status !== 'ready',
    props.refreshWriter,
  );

  const relaunch = machineRelaunch && {
    ...machineRelaunch,
    available: machineRelaunch.available && writer.relaunch !== null,
  };
  const confirmedEpoch = relaunch?.appliedEpoch ?? 0n;
  const epoch = writer.epoch > confirmedEpoch ? writer.epoch : confirmedEpoch;
  const actions = writer.actions;
  const send = (action: RunIntentAction) => {
    if (writer.status !== 'ready' || !writer.actions.includes(action)) return;
    const request = runIntent(
      {
        ...writer.reference,
        requestId: newRequestId(),
        expectedEpoch: writerFacts.observation?.epoch ?? 0n,
      },
      action,
      text,
    );
    current.current?.send({type: 'send', request});
    if (action === 'steer') showGuidance(false);
  };
  const controls = (
    <IntentControls
      {...(relaunch ? {relaunch} : {})}
      actions={actions}
      status={writer.status}
      epoch={epoch}
      busy={busy}
      entries={snapshot ? intentRows(snapshot.context, epoch) : []}
      error={snapshot?.context.error ?? null}
      warning={snapshot?.context.warning ?? null}
      guidanceOpen={guidanceOpen}
      showGuidance={showGuidance}
      text={text}
      edit={setText}
      send={send}
      retry={requestId => current.current?.send({type: 'retry', requestId})}
    />
  );
  const surface = intentSurface(props.enabled, controls);
  // Every surface that steers asks the same projection the Guidance control does.
  return {...surface, busy, sendRequest, steerable: offersSteer(props.enabled, writer)};
}

function offersSteer(enabled: boolean, writer: ReturnType<typeof writerControls>): boolean {
  return enabled && writer.status === 'ready' && writer.actions.includes('steer');
}

function intentSurface(
  enabled: boolean,
  controls: JSX.Element,
): {kind: 'enabled'; controls: JSX.Element} | {kind: 'disabled'; controls: null} {
  return enabled ? {kind: 'enabled', controls} : {kind: 'disabled', controls: null};
}

export function RunIntents(props: Parameters<typeof useRunIntents>[0]): JSX.Element | null {
  return useRunIntents(props).controls;
}

export {intentsActor} from './effects/controls.js';
export {relaunchActor} from './effects/relaunch.js';
