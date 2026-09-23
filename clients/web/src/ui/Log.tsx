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
import {type Ref, useImperativeHandle, useLayoutEffect, useRef, useState} from 'react';
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
  /** The tool row whose output the inspector shows, by `LogItem.id`. */
  selected: string | null;
  onSelect: (id: string | null) => void;
  ref?: Ref<LogHandle>;
}

/** What `L` needs from the log, which owns the scroller the key acts on. */
export interface LogHandle {
  jump: () => void;
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
/** The DOM id of a row, which is what `aria-activedescendant` names. */
const rowId = (id: string) => `row-${id}`;
/** Leaves of the round, folded or not: what the reader missed while away from the live edge. */
const leaves = (items: readonly LogItem[]): number =>
  items.reduce((total, item) => total + (item.kind === 'run' ? item.items.length : 1), 0);

/**
 * The rows a reader can see. A closed fold's own summary is still a row; everything under it
 * is not rendered to them, so the cursor steps over it.
 */
function visibleRows(scroller: HTMLElement): HTMLElement[] {
  return [...scroller.querySelectorAll<HTMLElement>('[data-row]')].filter(row => {
    const shut = row.closest('details:not([open])');
    return shut === null || row.parentElement === shut;
  });
}
// ponytail: module state, since App renders exactly one Log at a time.
/** Set when a log unmounts with focus inside it, so the next round's log takes that focus. */
let carryFocus = false;

/** Rendered with `key={round}`, so a new selection starts with fresh follow state. */
export function Log({state, round, groups, follow, history, selected, onSelect, ref}: LogProps) {
  const scroller = useRef<HTMLDivElement>(null);
  const [away, setAway] = useState(false);
  // The roving cursor, by DOM id. It is not focus: the scroller keeps that and names the row
  // through `aria-activedescendant`, so moving the cursor is not announced as new content.
  const [cursor, setCursor] = useState<string | null>(null);
  const rows = groups.reduce((total, group) => total + leaves(group.items), 0);
  /** Rows at the moment the reader left the live edge: the count the button reports is since. */
  const mark = useRef(0);
  const fresh = Math.max(0, rows - mark.current);

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

  function leave() {
    if (!away) mark.current = rows;
    setAway(true);
  }

  function onScroll() {
    const node = scroller.current;
    if (node === null) return;
    const gone = node.scrollHeight - node.scrollTop - node.clientHeight > 32;
    if (gone && !away) mark.current = rows;
    setAway(gone);
  }

  function jump() {
    const node = scroller.current;
    if (node === null) return;
    node.scrollTop = node.scrollHeight;
    node.focus();
    setAway(false);
  }
  // `L` reaches the scroller from anywhere, so the key binding lives with the other shortcuts.
  // biome-ignore lint/correctness/useExhaustiveDependencies: `jump` only reads stable refs.
  useImperativeHandle(ref, () => ({jump}), []);

  /** Open or close the fold a summary row heads; anything else has none. */
  function fold(row: HTMLElement, open: boolean) {
    const details = row.parentElement;
    if (!(details instanceof HTMLDetailsElement) || details.open === open) return;
    details.open = open;
    // Opening history leaves the live edge, as a click on the same summary does.
    if (open) leave();
  }

  function onKeyDown(event: React.KeyboardEvent<HTMLDivElement>) {
    const node = scroller.current;
    if (node === null || event.metaKey || event.ctrlKey || event.altKey || event.shiftKey) return;
    const visible = visibleRows(node);
    const at = visible.findIndex(row => row.id === cursor);
    const here = visible[at];
    const move = (next: HTMLElement | undefined) => {
      if (next === undefined) return;
      setCursor(next.id);
      // Within the log only: the page itself never scrolls under the reader.
      next.scrollIntoView({block: 'nearest'});
    };
    // Enter and Space on a focused <summary> are the native toggle; only the scroller's own
    // Enter acts on the cursor.
    const enter = event.key === 'Enter' && event.target === event.currentTarget;
    if (event.key === 'ArrowDown') move(at < 0 ? visible[0] : visible[at + 1]);
    else if (event.key === 'ArrowUp') move(at < 0 ? visible.at(-1) : visible[at - 1]);
    else if (here === undefined) return;
    else if (event.key === 'ArrowRight' || enter) {
      if (here.dataset.tool !== undefined) onSelect(here.dataset.tool);
      else fold(here, true);
    } else if (event.key === 'ArrowLeft') {
      if (here.dataset.tool !== undefined) onSelect(null);
      else fold(here, false);
    } else if (event.key === 'Escape') {
      if (selected === null) return;
      onSelect(null);
    } else return;
    event.preventDefault();
  }

  return (
    <div className="logwrap">
      {/* The region stays a `log`, which does not take `aria-activedescendant`: the cursor
          rides along for anything reading the DOM and is deliberately not announced. */}
      {/* biome-ignore lint/a11y/useAriaPropsSupportedByRole: the region is a live log first. */}
      <div
        ref={scroller}
        id="log"
        className="log"
        role="log"
        aria-live="off"
        aria-label={round === null ? 'Run log' : `Round ${round} log`}
        aria-busy={state === 'loading' || history.loading || undefined}
        aria-activedescendant={cursor ?? undefined}
        // biome-ignore lint/a11y/noNoninteractiveTabindex: a scrollable region must take focus to scroll by keyboard.
        tabIndex={0}
        onScroll={onScroll}
        onKeyDown={onKeyDown}
        // Delegated, so a row stays a row rather than becoming one tab stop each: the keyboard
        // path is the cursor this scroller carries.
        onClick={event => {
          const row = (event.target as HTMLElement).closest<HTMLElement>('[data-tool]');
          if (row?.dataset.tool === undefined) return;
          // The cursor follows the pointer, so the two never disagree about which row is live.
          setCursor(row.id);
          onSelect(row.dataset.tool);
        }}
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
                <Group
                  key={group.id}
                  group={group}
                  cursor={cursor}
                  selected={selected}
                  onOpen={leave}
                />
              ))}
            </ol>
          )}
        </div>
      </div>
      {follow && away ? (
        // The count is the one thing the button does not otherwise say: how much was missed.
        <button
          type="button"
          className="jump"
          aria-keyshortcuts="L"
          data-tip=""
          data-key="L"
          onClick={jump}
        >
          <ArrowDown size={14} strokeWidth={1.75} aria-hidden />
          {fresh === 0 ? 'Jump to latest' : `${fresh} new · Jump to latest`}
        </button>
      ) : null}
    </div>
  );
}

function Group({
  group,
  cursor,
  selected,
  onOpen,
}: {
  group: LogGroup;
  cursor: string | null;
  selected: string | null;
  onOpen: () => void;
}) {
  const Icon = ROLE_ICONS[group.role] ?? Bot;
  const steers = group.items.filter(item => item.kind === 'steer');
  const rest = group.items.filter(item => item.kind !== 'steer');
  return (
    <li className={group.active ? 'grp is-active' : 'grp'}>
      <div className="who">
        <Icon {...icon} />
        {titleCase(group.role)}
        {group.attempt > 1 ? <span className="att">Attempt {group.attempt}</span> : null}
      </div>
      <div className="what">
        {group.collapsed ? (
          <>
            {steers.map(item => (
              <Item key={item.id} item={item} cursor={cursor} selected={selected} onOpen={onOpen} />
            ))}
            {rest.length === 0 ? null : (
              <Fold
                id={group.id}
                verb={group.summary || titleCase(group.role)}
                count={group.calls}
                items={rest}
                cursor={cursor}
                selected={selected}
                onOpen={onOpen}
              />
            )}
          </>
        ) : (
          group.items.map(item => (
            <Item key={item.id} item={item} cursor={cursor} selected={selected} onOpen={onOpen} />
          ))
        )}
      </div>
    </li>
  );
}

/** A counted row that opens in place: a whole role's turn, or a run of calls of one verb. */
function Fold({
  id,
  verb,
  count,
  items,
  cursor,
  selected,
  onOpen,
}: {
  id: string;
  verb: string;
  count: number;
  items: LogItem[];
  cursor: string | null;
  selected: string | null;
  onOpen: () => void;
}) {
  return (
    <details className="fold">
      {/* biome-ignore lint/a11y/noStaticElementInteractions: <summary> is natively interactive. */}
      <summary
        id={rowId(id)}
        data-row=""
        className={rowId(id) === cursor ? 'row cur' : 'row'}
        // Opening history leaves the live edge, so following stops before the fold grows. Enter
        // and Space also fire click; `toggle` would come too late.
        onClick={event => {
          if (!(event.currentTarget.parentElement as HTMLDetailsElement).open) onOpen();
        }}
      >
        <span className="lab">
          <span className="verb">{verb}</span>
          {count === 0 ? null : <span className="n mono">{calls(count)}</span>}
        </span>
        <ChevronRight {...icon} className="chev" />
      </summary>
      <div className="what">
        {items.map(item => (
          <Item key={item.id} item={item} cursor={cursor} selected={selected} onOpen={onOpen} />
        ))}
      </div>
    </details>
  );
}

function Item({
  item,
  cursor,
  selected,
  onOpen,
}: {
  item: LogItem;
  cursor: string | null;
  selected: string | null;
  onOpen: () => void;
}) {
  const at = rowId(item.id) === cursor;
  if (item.kind === 'run') {
    return (
      <Fold
        id={item.id}
        verb={item.verb}
        count={item.items.length}
        items={item.items}
        cursor={cursor}
        selected={selected}
        onOpen={onOpen}
      />
    );
  }
  if (item.kind === 'steer') {
    return (
      <p id={rowId(item.id)} data-row="" className={at ? 'said cur' : 'said'}>
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
      <div id={rowId(item.id)} data-row="" className={at ? 'say cur' : 'say'}>
        <Prose paragraphs={item.paragraphs} />
      </div>
    );
  }
  // The row shows the command's shape, sometimes none of it. What the row dropped is its
  // tooltip and, for a reader who cannot hover, its own DOM text. A row already showing its
  // whole command carries no tooltip: it would only repeat what the reader is looking at.
  const whole = item.argFull;
  const tip = whole !== null && whole.length > TIP_LIMIT ? `${whole.slice(0, TIP_LIMIT)}…` : whole;
  const marks = [
    'row',
    item.inFlight ? 'now' : '',
    item.id === selected ? 'sel' : '',
    at ? 'cur' : '',
  ];
  return (
    <div
      id={rowId(item.id)}
      data-row=""
      className={marks.filter(Boolean).join(' ')}
      aria-current={item.inFlight ? 'step' : undefined}
      data-tool={item.id}
      data-tip={tip ?? undefined}
    >
      <span className="lab">
        <span className="verb">{item.verb}</span>
        {item.arg === null ? null : (
          <code className="arg" aria-hidden={item.argFull === null ? undefined : true}>
            {item.arg}
          </code>
        )}
        {item.argFull === null ? null : <span className="sr-only">{item.argFull}</span>}
        {item.result === null ? null : (
          <span className={item.result.failed ? 'res err' : 'res'}>{item.result.text}</span>
        )}
      </span>
      {item.duration === null ? null : <span className="dur mono">{item.duration}</span>}
    </div>
  );
}
