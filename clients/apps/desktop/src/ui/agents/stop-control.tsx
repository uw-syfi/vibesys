import {type Agent, AgentRole, AgentStatus, AgentStopStage} from '@vibesys/orchestrator-proto';
import {Button, Dialog, Stack, Text} from '@vibesys/ui';
import {type JSX, useState} from 'react';
import type {CommandProgress} from '../../state/index.js';
import {CommandLine} from '../orchestrator/command-line.js';

/** An agent whose session is over, by any end; the host refuses a stop of one of these. */
const FINAL: ReadonlySet<AgentStatus> = new Set([
  AgentStatus.ENDED,
  AgentStatus.FAILED,
  AgentStatus.LOST,
]);

/**
 * Whether a press of Stop could reach the host: a live agent that is not the run's lead and whose
 * session is not already closing. The host refuses a stop of the lead, of an agent that is over,
 * and of one already closing; the control is hidden for those so a person is never offered a press
 * that only comes back refused. The mirror of this decision lives in the host (`stopSubagent`); the
 * host is the authority, this is only what to show.
 */
export function canStopAgent(agent: Agent): boolean {
  return (
    agent.role !== AgentRole.LEAD &&
    !FINAL.has(agent.status) &&
    agent.stopStage !== AgentStopStage.CLOSING
  );
}

/**
 * The Stop control for one agent: a danger button that opens a modal to confirm, since a stop
 * cannot be taken back. The modal says what a stop does, carries the press, and shows how it went
 * (sending, accepted, refused in the host's words, or a transport fault with a retry). The button
 * is drawn only while a stop could reach the host; once one is in flight or settled the modal still
 * reads it, so the result stays in view.
 */
export function StopControl(props: {
  readonly agent: Agent;
  /** Where the last Stop of this agent stands, or undefined before any. */
  readonly progress: CommandProgress | undefined;
  readonly stop: () => void;
  readonly retry: () => void;
}): JSX.Element | null {
  const [open, setOpen] = useState(false);
  const stoppable = canStopAgent(props.agent);
  if (!stoppable && props.progress === undefined) return null;
  const sending = props.progress?._tag === 'Sending';
  const settled = props.progress?._tag === 'Accepted' || props.progress?._tag === 'Refused';
  const actions =
    settled || !stoppable ? (
      <Button text="Close" variant="secondary" press={() => setOpen(false)} />
    ) : (
      <>
        <Button text="Cancel" variant="secondary" press={() => setOpen(false)} />
        <Button
          text={sending ? 'Stopping agent' : 'Stop agent'}
          label="Stop agent"
          variant="danger"
          press={props.stop}
          {...(sending ? {disabledReason: 'Stopping', quietReason: true} : {})}
        />
      </>
    );
  return (
    <>
      {stoppable ? (
        <Button
          text="Stop agent"
          label="Stop agent"
          variant="danger"
          press={() => setOpen(true)}
          {...(sending ? {disabledReason: 'Stopping.', quietReason: true} : {})}
        />
      ) : null}
      <Dialog open={open} title="Stop this agent?" dismiss={() => setOpen(false)} actions={actions}>
        <Stack gap="space-3">
          <Text>
            Its turn ends and its session closes. Anything it has not handed back is lost. The run
            keeps going.
          </Text>
          <CommandLine progress={props.progress} done="Stop agent" retry={props.retry} />
        </Stack>
      </Dialog>
    </>
  );
}
