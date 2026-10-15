import type {WebSession} from '@vibesys/run-session';
import type {TranscriptEntry} from '@vibesys/run-state/transcript';
import {type JSX, useLayoutEffect, useState, useSyncExternalStore} from 'react';
import type {ConversationEvent, ConversationState} from '../state/index.js';
import {createConversationStore} from './effects/store.js';
import {ConversationView} from './ui/panel.js';

export interface ConversationPanelProps {
  readonly session?: WebSession;
  readonly runId?: string | null;
  readonly transcript?: readonly TranscriptEntry[];
  readonly connected?: boolean;
  readonly steerAvailable?: boolean;
  readonly source?: 'fixture' | 'campaign-stream';
}

/** Changing the run or session retires requests and resets drafts, messages, and open state. */
export function ConversationPanel({
  session,
  runId = null,
  transcript = [],
  connected = false,
  steerAvailable = false,
  source = 'fixture',
}: ConversationPanelProps): JSX.Element {
  const available = session !== undefined && runId !== null && connected;
  const [store, setStore] = useState<ReturnType<typeof createConversationStore> | null>(null);
  useLayoutEffect(() => {
    const owned = createConversationStore(session, {
      connected: session !== undefined && runId !== null,
      steerAvailable: false,
    });
    setStore(owned);
    return () => owned.close();
  }, [session, runId]);
  useLayoutEffect(() => {
    store?.dispatch({type: 'availability', availability: {connected: available, steerAvailable}});
  }, [store, available, steerAvailable]);
  const state = useSyncExternalStore(
    store?.subscribe ?? noSubscription,
    store?.getState ?? initialView,
    initialView,
  );
  const dispatch = (event: ConversationEvent): void => {
    store?.dispatch(
      event.type === 'submit'
        ? {
            ...event,
            transcriptStart: session?.store.getState().chatTranscript.length ?? transcript.length,
          }
        : event,
    );
  };
  const target =
    session === undefined
      ? source === 'fixture'
        ? 'Fixture replay'
        : 'Campaign stream preview'
      : runId === null
        ? 'Loading run'
        : `Run ${runId}`;
  const reason = unavailableReason(session, runId, source, available);
  return (
    <ConversationView
      state={state}
      dispatch={dispatch}
      target={target}
      unavailableReason={reason}
      transcript={transcript}
    />
  );
}

const EMPTY: ConversationState = {
  open: false,
  intent: 'ask',
  connected: false,
  steerAvailable: false,
  drafts: {ask: '', steer: ''},
  submissions: [],
};
const initialView = (): ConversationState => EMPTY;
const noSubscription = (): (() => void) => () => undefined;

function unavailableReason(
  session: WebSession | undefined,
  runId: string | null,
  source: 'fixture' | 'campaign-stream',
  available: boolean,
): string {
  if (session === undefined)
    return source === 'fixture'
      ? 'Conversation is unavailable in fixture replay. Connect to a live gateway to ask or steer.'
      : 'Conversation is unavailable in this campaign stream preview. Connect to a gateway run to ask or steer.';
  if (runId === null) return 'Waiting for the gateway to identify this run.';
  if (!available) return 'Conversation is unavailable while the gateway controls are disconnected.';
  return 'This run has ended. There is no next invocation to steer.';
}
