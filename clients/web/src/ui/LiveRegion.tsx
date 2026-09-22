import {useEffect, useRef, useState} from 'react';
import {announce} from '../derive.js';
import type {RunPulse} from '../model.js';

/** The one polite live region: run status changes, new rounds, and drops, never log output. */
export function LiveRegion({status, round, ended, connection}: RunPulse) {
  const previous = useRef<RunPulse | null>(null);
  const [message, setMessage] = useState('');
  useEffect(() => {
    const next = {status, round, ended, connection};
    const said = announce(previous.current, next);
    previous.current = next;
    if (said !== null) setMessage(said);
  }, [status, round, ended, connection]);
  return (
    <p className="sr-only" role="status">
      {message}
    </p>
  );
}
