/** The capacity-stop banner: what the provider said, what the run does about it, and, while
 * paused, the operator's resume choices. Presentational: it renders the fact it is given and
 * reports clicks through the command port, the same one `RunControls` sends through. */

import type {RunCommand, RunCommandOutcome} from '@vibesys/run-session';
import {type JSX, useState} from 'react';
import type {QuotaBanner as QuotaBannerFact} from '../../state/quota-banner.js';

export interface QuotaBannerProps {
  readonly banner: QuotaBannerFact | null;
  /** Null on replay, which has no command channel by contract; drops the resume affordances. */
  readonly command: ((input: RunCommand) => Promise<RunCommandOutcome>) | null;
}

type Pending = 'resume' | 'fallback' | null;

export function QuotaBanner({banner, command}: QuotaBannerProps): JSX.Element | null {
  const [pending, setPending] = useState<Pending>(null);
  const [result, setResult] = useState<string | null>(null);
  if (banner === null) return null;

  const resume = async (fallback: boolean): Promise<void> => {
    if (command === null) return;
    setPending(fallback ? 'fallback' : 'resume');
    setResult(null);
    try {
      const response = await command(
        fallback ? {type: 'command.resume', fallback: true} : {type: 'command.resume'},
      );
      setResult(
        response.kind === 'rejected'
          ? (response.message ?? 'The run rejected this command.')
          : `${response.action} ${response.status}`,
      );
    } catch (error) {
      setResult(error instanceof Error ? error.message : 'The command could not reach the run.');
    } finally {
      setPending(null);
    }
  };

  return (
    <div
      className="stale-banner quota-banner"
      role="alert"
      aria-label="Quota status"
      data-testid="quota-banner"
    >
      <div className="quota-banner-copy">
        <span>{banner.message}</span>
        <p className="controls-copy">{banner.detail}</p>
        {banner.kind === 'pause' && <p className="controls-copy">{banner.wait}</p>}
        {result !== null && (
          <p className="control-result" role="status">
            {result}
          </p>
        )}
      </div>
      {banner.kind === 'pause' && command !== null && (
        <div className="control-actions">
          <button type="button" disabled={pending !== null} onClick={() => void resume(false)}>
            {pending === 'resume' ? 'Resuming…' : 'Resume'}
          </button>
          {banner.fallback !== null && (
            <button type="button" disabled={pending !== null} onClick={() => void resume(true)}>
              {pending === 'fallback'
                ? 'Resuming…'
                : `Resume on ${banner.fallback.provider}${
                    banner.fallback.model === null ? '' : ` (${banner.fallback.model})`
                  }`}
            </button>
          )}
        </div>
      )}
    </div>
  );
}
