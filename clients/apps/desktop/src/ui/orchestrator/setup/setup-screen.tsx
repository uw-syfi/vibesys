import {type Agent, type Run, RunStatus} from '@vibesys/orchestrator-proto';
import {Button, Chip, type Crumb, Page, Pane, Panes, Text} from '@vibesys/ui';
import type {JSX, ReactNode} from 'react';
import {
  agentNames,
  type CommandProgress,
  inSetup,
  messageIs,
  messagesOf,
  type OrchestratorState,
  type SetupFacts,
  startOutcome,
} from '../../../state/index.js';
import {AgentTranscript, type HistoryAccess} from '../../agents/agent-transcript.js';
import {agentLabel} from '../../agents/labels.js';
import {MessageBox} from '../../agents/message-box.js';
import {type CompleteSetupRequest, completeRequestOf} from './complete-request.js';
import {DraftPanel} from './draft-panel.js';
import {runStateWord, STARTING, setupTitle} from './setup-words.js';

const STOPPABLE = new Set([RunStatus.STARTING, RunStatus.RUNNING]);

/**
 * A run in its setup phase: the chat with the lead that makes the task, beside the draft of the task
 * as the host reads it. The page does not scroll; only the transcript does. When Start is taken and
 * the lead is still finishing its turn the run's state says Starting until the host swaps the run
 * into optimizing, when this page is replaced by the run's own.
 */
export function SetupScreen(props: {
  readonly state: OrchestratorState;
  readonly run: Run;
  readonly facts: SetupFacts;
  /** The lead the chat is with; undefined until the run's agents have been read. */
  readonly lead: Agent | undefined;
  readonly host: string;
  readonly nowMs: number;
  readonly trail: readonly Crumb[];
  /** Where the reads of the run stand, among the state words under the title. */
  readonly reading: ReactNode;
  readonly history: HistoryAccess;
  readonly open: (agentId: string) => void;
  readonly sendProgress: CommandProgress | undefined;
  readonly send: (text: string) => void;
  readonly retrySend: () => void;
  readonly completeProgress: CommandProgress | undefined;
  readonly complete: (request: CompleteSetupRequest) => void;
  readonly retryComplete: () => void;
  readonly stopProgress: CommandProgress | undefined;
  readonly stop: () => void;
}): JSX.Element {
  const {state, run, facts, lead} = props;
  const outcome = startOutcome(props.completeProgress);
  // Taken, yet still in setup: the host waits for the lead to rest before it swaps the run.
  const starting = outcome.kind === 'taken' && inSetup(run);
  const word = runStateWord(true, run.status);
  const name = lead === undefined ? undefined : agentNames(state, run.id).get(lead.id);
  return (
    <Page
      key="setup"
      width="wide"
      fill
      state="ready"
      trail={props.trail}
      title={setupTitle(run.repository)}
      meta={
        <>
          <Chip label={word.label} tone={word.tone} dot />
          {starting ? <Chip label={STARTING.label} tone={STARTING.tone} dot /> : null}
          <Chip label={props.host} mono clip />
          {props.reading}
        </>
      }
      actions={
        STOPPABLE.has(run.status) ? (
          <Button
            text="Stop run"
            label="Stop run"
            variant="danger"
            press={props.stop}
            {...(props.stopProgress?._tag === 'Sending'
              ? {disabledReason: 'Stopping.', quietReason: true}
              : {})}
          />
        ) : null
      }
    >
      <Panes>
        <Pane
          kicker="Setup chat"
          title={name === undefined ? 'Lead' : agentLabel(name)}
          width="fill"
          fixed
        >
          {lead === undefined ? (
            <Text tone="muted">Reading.</Text>
          ) : (
            <>
              <AgentTranscript
                state={state}
                agentId={lead.id}
                host={props.host}
                nowMs={props.nowMs}
                history={props.history}
                open={props.open}
              />
              <MessageBox
                nowMs={props.nowMs}
                agent={lead}
                progress={props.sendProgress}
                // Only the last message sent: the box keeps one height as the chat goes on.
                sent={messagesOf(state, lead.id)
                  .filter(message => message.fromAgentId === '' && messageIs(message, 'text'))
                  .slice(-1)}
                send={props.send}
                retry={props.retrySend}
              />
            </>
          )}
        </Pane>
        <DraftPanel
          facts={facts}
          outcome={outcome}
          starting={starting}
          start={() => lead !== undefined && props.complete(completeRequestOf(facts, lead))}
          retry={props.retryComplete}
        />
      </Panes>
    </Page>
  );
}
