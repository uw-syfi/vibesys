/** Operator controls for a live run. Replay has no command capability, so it renders the same
 * controls as disabled, explanatory UI rather than pretending an action can round-trip. */

import type {RunCommand, RunCommandOutcome} from '@vibesys/run-session';
import {type CoreState, hasRunEnded} from '@vibesys/run-state';
import {type FormEvent, type JSX, useState} from 'react';

export interface RunControlsProps {
  readonly run: CoreState;
  readonly command: ((input: RunCommand) => Promise<RunCommandOutcome>) | null;
}

type CommandResult = {readonly tone: 'success' | 'failure'; readonly text: string} | null;

export function RunControls({run, command}: RunControlsProps): JSX.Element {
  const [steer, setSteer] = useState('');
  const [pending, setPending] = useState<RunCommand['type'] | null>(null);
  const [result, setResult] = useState<CommandResult>(null);
  const readOnly = command === null;
  const ended = hasRunEnded(run);
  const unavailable = readOnly || ended || pending !== null;
  const pauseType = run.status === 'paused' ? 'command.resume' : 'command.pause';
  const pauseLabel = pauseType === 'command.resume' ? 'Resume' : 'Pause';
  const pausePendingLabel = pauseType === 'command.resume' ? 'Resuming…' : 'Pausing…';

  const send = async (input: RunCommand): Promise<void> => {
    if (command === null || hasRunEnded(run)) return;
    setPending(input.type);
    setResult(null);
    try {
      const response = await command(input);
      if (response.kind === 'rejected') {
        setResult({tone: 'failure', text: response.message ?? 'The run rejected this command.'});
      } else {
        setResult({tone: 'success', text: `${response.action} ${response.status}`});
        if (input.type === 'command.steer') setSteer('');
      }
    } catch (error) {
      setResult({
        tone: 'failure',
        text: error instanceof Error ? error.message : 'The command could not reach the run.',
      });
    } finally {
      setPending(null);
    }
  };

  const submitSteer = (event: FormEvent<HTMLFormElement>): void => {
    event.preventDefault();
    const text = steer.trim();
    if (text.length > 0) void send({type: 'command.steer', text});
  };

  const disabledReason = readOnly
    ? 'Controls are unavailable while replaying a recorded run.'
    : ended
      ? 'This run has ended.'
      : null;

  return (
    <section className="run-controls" aria-label="Run controls">
      <div className="panel-heading">
        <h2>Run controls</h2>
        <span>{readOnly ? 'Read-only replay' : 'Live command channel'}</span>
      </div>
      {disabledReason !== null && <p className="controls-copy">{disabledReason}</p>}
      <div className="control-actions">
        <button type="button" disabled={unavailable} onClick={() => void send({type: pauseType})}>
          {pending === pauseType ? pausePendingLabel : pauseLabel}
        </button>
        <button
          type="button"
          className="danger-button"
          disabled={unavailable}
          onClick={() => void send({type: 'command.stop'})}
        >
          {pending === 'command.stop' ? 'Stopping…' : 'Stop'}
        </button>
      </div>
      <form className="steer-form" onSubmit={submitSteer}>
        <label htmlFor="run-steer">Steer the next safe point</label>
        <div>
          <input
            id="run-steer"
            value={steer}
            disabled={unavailable}
            onChange={event => setSteer(event.target.value)}
            placeholder="Describe what the run should consider"
          />
          <button type="submit" disabled={unavailable || steer.trim().length === 0}>
            {pending === 'command.steer' ? 'Sending…' : 'Send'}
          </button>
        </div>
      </form>
      {result !== null && (
        <p className={`control-result control-result-${result.tone}`} role="status">
          {result.text}
        </p>
      )}
    </section>
  );
}
