import {useEffect, useRef, useState} from 'react';
import {announce} from '../derive.js';
import type {RunPulse} from '../model.js';

/** The one polite live region: run status changes and new rounds, never log output. */
export function LiveRegion({status, round, ended}: RunPulse) {
  const previous = useRef<RunPulse | null>(null);
  const [message, setMessage] = useState('');
  useEffect(() => {
    const next = {status, round, ended};
    const said = announce(previous.current, next);
    previous.current = next;
    if (said !== null) setMessage(said);
  }, [status, round, ended]);
  return (
    <p className="sr-only" role="status">
      {message}
    </p>
  );
}
