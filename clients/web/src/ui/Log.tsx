import {
  ArrowDown,
  Bot,
  ChevronRight,
  Compass,
  Gauge,
  Hammer,
  type LucideIcon,
  Scale,
  User,
} from 'lucide-react';
import {useLayoutEffect, useRef, useState} from 'react';
import {titleCase} from '../derive.js';
import type {LogGroup, LogItem} from '../model.js';
import {Prose} from './Prose.js';
import './Log.css';

export interface LogProps {
  state: 'loading' | 'ready';
  round: number | null;
  groups: LogGroup[];
  /** The selected round is the latest one: pin to the bottom until the reader scrolls up. */
  follow: boolean;
  /** Backfill below the tail floor, which App runs on its own; Retry only after an error. */
  history: {loading: boolean; error: string | null; onRetry: () => void};
}

const ROLE_ICONS: Record<string, LucideIcon> = {
  orchestrator: Compass,
  implementer: Hammer,
  judge: Scale,
  profiler: Gauge,
};
const icon = {size: 16, strokeWidth: 1.75, 'aria-hidden': true} as const;
const TIP_LIMIT = 600;

const calls = (count: number) => (count === 1 ? '1 call' : `${count} calls`);
// ponytail: module state, since App renders exactly one Log at a time.
/** Set when a log unmounts with focus inside it, so the next round's log takes that focus. */
let carryFocus = false;

/** Rendered with `key={round}`, so a new selection starts with fresh follow state. */
export function Log({state, round, groups, follow, history}: LogProps) {
  const scroller = useRef<HTMLDivElement>(null);
  const [away, setAway] = useState(false);

  // While following, every size change keeps the bottom in view: new rows, and also reflow on a
  // resize or a taller dock, which no render of this component sees.
  useLayoutEffect(() => {
    const node = scroller.current;
    if (node === null || !follow || away) return;
    const observer = new ResizeObserver(() => {
      node.scrollTop = node.scrollHeight;
    });
    observer.observe(node);
    observer.observe(node.firstElementChild as Element);
    return () => observer.disconnect();
  }, [follow, away]);

  // App re-keys the log per round. Focus that was inside the old log moves to the new one;
  // focus anywhere else stays put.
  useLayoutEffect(() => {
    const node = scroller.current;
    if (carryFocus) node?.focus({preventScroll: true});
    carryFocus = false;
    return () => {
      carryFocus = node?.parentElement?.contains(document.activeElement) ?? false;
    };
  }, []);

  function onScroll() {
    const node = scroller.current;
    if (node !== null) setAway(node.scrollHeight - node.scrollTop - node.clientHeight > 32);
  }

  function jump() {
    const node = scroller.current;
    if (node === null) return;
    node.scrollTop = node.scrollHeight;
    node.focus();
    setAway(false);
  }

  return (
    <div className="logwrap">
      <div
        ref={scroller}
        id="log"
        className="log"
        role="log"
        aria-live="off"
        aria-label={round === null ? 'Run log' : `Round ${round} log`}
        aria-busy={state === 'loading' || history.loading || undefined}
        // biome-ignore lint/a11y/noNoninteractiveTabindex: a scrollable region must take focus to scroll by keyboard.
        tabIndex={0}
        onScroll={onScroll}
      >
        <div className="col">
          {history.loading ? (
            <div className="log-skel older" aria-hidden="true">
              <span />
              <span />
            </div>
          ) : null}
          {history.error === null ? null : (
            <p className="older" role="alert">
              Could not load earlier events: {history.error}{' '}
              <button type="button" className="btn btn-sm" onClick={history.onRetry}>
                Retry
              </button>
            </p>
          )}
          {state === 'loading' ? (
            <div className="log-skel" aria-hidden="true">
              <span />
              <span />
              <span />
              <span />
            </div>
          ) : groups.length === 0 ? (
            // While earlier history is loading or failed, an empty round is not known to be empty.
            history.loading || history.error !== null ? null : (
              <p className="log-note">
                {round === null
                  ? 'No round has started yet'
                  : round === 0
                    ? 'No agent calls in the baseline'
                    : 'No agent calls in this round yet'}
              </p>
            )
          ) : (
            <ol className="grps">
              {groups.map(group => (
                <Group key={group.id} group={group} onOpen={() => setAway(true)} />
              ))}
            </ol>
          )}
        </div>
      </div>
      {follow && away ? (
        <button type="button" className="jump" onClick={jump}>
          <ArrowDown size={14} strokeWidth={1.75} aria-hidden />
          Jump to latest
        </button>
      ) : null}
    </div>
  );
}

function Group({group, onOpen}: {group: LogGroup; onOpen: () => void}) {
  const Icon = ROLE_ICONS[group.role] ?? Bot;
  const steers = group.items.filter(item => item.kind === 'steer');
  const rest = group.items.filter(item => item.kind !== 'steer');
  return (
    <>
      {group.divider === null ? null : <li className="attempt">Attempt {group.divider}</li>}
      <li className={group.active ? 'grp is-active' : 'grp'}>
        <div className="who">
          <Icon {...icon} />
          {titleCase(group.role)}
        </div>
        <div className="what">
          {group.collapsed ? (
            <>
              {steers.map(item => (
                <Item key={item.id} item={item} />
              ))}
              {rest.length === 0 ? null : (
                <details className="fold">
                  {/* biome-ignore lint/a11y/noStaticElementInteractions: <summary> is natively interactive. */}
                  <summary
                    className="row"
                    // Opening history leaves the live edge, so following stops before the fold
                    // grows. Enter and Space also fire click; `toggle` would come too late.
                    onClick={event => {
                      if (!(event.currentTarget.parentElement as HTMLDetailsElement).open) onOpen();
                    }}
                  >
                    <span className="lab">
                      <span className="verb">{group.summary || titleCase(group.role)}</span>
                      {group.calls === 0 ? null : (
                        <span className="n mono">{calls(group.calls)}</span>
                      )}
                    </span>
                    <ChevronRight {...icon} className="chev" />
                  </summary>
                  <div className="what">
                    {rest.map(item => (
                      <Item key={item.id} item={item} />
                    ))}
                  </div>
                </details>
              )}
            </>
          ) : (
            group.items.map(item => <Item key={item.id} item={item} />)
          )}
        </div>
      </li>
    </>
  );
}

function Item({item}: {item: LogItem}) {
  if (item.kind === 'steer') {
    return (
      <p className="said">
        <span className="ic" data-tip="Steer, delivered at the start of this call">
          <User {...icon} />
        </span>
        <span>
          <span className="sr-only">Steer: </span>
          {item.text}
        </span>
      </p>
    );
  }
  if (item.kind === 'prose') {
    return (
      <div className="say">
        <Prose paragraphs={item.paragraphs} />
      </div>
    );
  }
  const tip =
    item.arg !== null && item.arg.length > TIP_LIMIT
      ? `${item.arg.slice(0, TIP_LIMIT)}…`
      : item.arg;
  return (
    <div
      className={item.inFlight ? 'row now' : 'row'}
      aria-current={item.inFlight ? 'step' : undefined}
    >
      <span className="lab">
        <span className="verb">{item.verb}</span>
        {item.arg === null ? null : (
          <code className="arg" data-tip={tip ?? undefined}>
            {item.arg}
          </code>
        )}
        {item.result === null ? null : (
          <span className={item.result.failed ? 'res err' : 'res'}>{item.result.text}</span>
        )}
      </span>
    </div>
  );
}
