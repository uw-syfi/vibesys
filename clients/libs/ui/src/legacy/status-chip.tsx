import type {JSX, ReactNode} from 'react';

/** The closed set of tones a status reads in. */
export type Tone = 'ok' | 'warn' | 'error' | 'idle';

const DOTS: Readonly<Record<Tone, string>> = {
  ok: 'dot ok',
  warn: 'dot warn',
  error: 'dot error',
  idle: 'dot',
};

/** A rounded chip: a colored dot for `tone`, then `label`. A button when it has `press`. */
export function StatusChip(props: {
  readonly tone: Tone;
  readonly label: ReactNode;
  readonly title?: string;
  readonly press?: () => void;
}): JSX.Element {
  const {tone, label, title, press} = props;
  const content = (
    <>
      <span className={DOTS[tone]} />
      {label}
    </>
  );
  return press === undefined ? (
    <span className="chip" title={title}>
      {content}
    </span>
  ) : (
    <button className="chip" type="button" title={title} onClick={press}>
      {content}
    </button>
  );
}
