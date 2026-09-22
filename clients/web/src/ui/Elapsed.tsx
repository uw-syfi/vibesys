import {useEffect, useState} from 'react';
import {formatDuration} from '../derive.js';

export interface ElapsedProps {
  /** Milliseconds elapsed at `now`. */
  ms: (now: Date) => number;
  /** Ticks once a second while true; this is the only per-second render. */
  live: boolean;
  /** Tooltip, also read before the value by screen readers. */
  tip: string;
  className: string;
  /** Tooltip side: `right` (the rail) or `null` for below (the header, clear of the run control). */
  side?: 'right' | null;
  /** Hides the tip and the ticking digits from screen readers. */
  'aria-hidden'?: boolean;
}

export function Elapsed(props: ElapsedProps) {
  const {ms, live, tip, className, side = 'right', 'aria-hidden': hidden} = props;
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    if (!live) return;
    const timer = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(timer);
  }, [live]);
  return (
    <span
      className={`${className} mono`}
      data-tip={tip}
      data-side={side ?? undefined}
      aria-hidden={hidden}
    >
      <span className="sr-only">{tip} </span>
      {formatDuration(ms(now))}
    </span>
  );
}
