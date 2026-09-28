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
/** The DOM id of a row. The cursor is the focused one, so this is for finding it again. */
const rowId = (id: string) => `row-${id}`;
/** Leaves of the round, folded or not: what the reader missed while away from the live edge. */
const leaves = (items: readonly LogItem[]): number =>
  items.reduce((total, item) => total + (item.kind === 'run' ? item.items.length : 1), 0);

/**
 * The rows a reader can see, which is the browser's own answer: a row inside a closed fold is
 * not rendered, and folds nest, so walking the ancestors by hand gets a fold inside a closed
 * fold wrong. The cursor takes real focus, and focusing an unrendered row silently drops it.
 */
function visibleRows(scroller: HTMLElement): HTMLElement[] {
  return [...scroller.querySelectorAll<HTMLElement>('[data-row]')].filter(row =>
    row.checkVisibility(),
  );
}
// ponytail: module state, since App renders exactly one Log at a time.
/** Set when a log unmounts with focus inside it, so the next round's log takes that focus. */
let carryFocus = false;

/** Rendered with `key={round}`, so a new selection starts with fresh follow state. */
export function Log({state, round, groups, follow, history, selected, onSelect, ref}: LogProps) {
  const scroller = useRef<HTMLDivElement>(null);
  const [away, setAway] = useState(false);
  const rows = groups.reduce((total, group) => total + leaves(group.items), 0);
  /**
   * The row the cursor was last on. `node` is the element while it still holds focus, so a
   * commit that took focus away can be told from the reader moving it; `id` outlives the node,
   * because a row can be remounted rather than hidden and the new one answers to the same id.
   */
  const held = useRef<{node: HTMLElement | null; id: string} | null>(null);
  /** Rows at the moment the reader left the live edge: the count the button reports is since. */
  const mark = useRef(0);
  const fresh = Math.max(0, rows - mark.current);

  // While following, every size change keeps the bottom in view: new rows, and also reflow on a
  // resize or a taller dock, which no render of this component sees. The pin runs in the commit
  // itself as well as from the observer, because a scroll event dispatched in the gap between
  // the rows landing and the observer catching up reads as the reader walking away.
  // biome-ignore lint/correctness/useExhaustiveDependencies: `groups` is what changed the rows.
  useLayoutEffect(() => {
    const node = scroller.current;
    if (node === null || !follow || away) return;
    node.scrollTop = node.scrollHeight;
    const observer = new ResizeObserver(() => {
      node.scrollTop = node.scrollHeight;
    });
    observer.observe(node);
    observer.observe(node.firstElementChild as Element);
    return () => observer.disconnect();
  }, [follow, away, groups]);

  // A render can hide the cursor's row behind a fold that closed over it, or replace it with a
  // new node: an adjacent call of the same verb settling folds a run, a new role speaking
  // collapses the group the cursor was in, and a steer row in a collapsing group is remounted
  // into the branch that renders outside the fold. Chromium then blurs to <body>, where the
  // log's keydown never fires and the global handler reads the arrows as the rail's, so a
  // reader pressing up to move one row loses the round instead.
  //
  // The test is on the node that held focus, not on its id: a remounted row answers to the id
  // with a node that never had focus, and <body> holds focus for plenty of reasons that are
  // none of the log's business.
  //
  // What lets the node survive to be tested is React's commit ordering, which is an internal
  // rather than a contract. Chromium does fire `focusout` on removal, synchronously, before the
  // node is detached and with `checkVisibility()` still true, so `onBlur` below would release
  // the node and kill this restore; it does not run only because React detaches the row's fiber
  // during the mutation phase and its delegated dispatch then finds no ancestor handler. If that
  // ever flips, the browser fixture's `inLog()` assertions after each trigger are what fail.
  //
  // The one mutation this cannot survive is a row being *moved* rather than removed or hidden:
  // `onBlur` does run there, with the fiber intact. It is unreachable only because rows are
  // keyed by `item.id` and ordered by a monotonic sequence (`derive.ts`, `rows.sort`), so an
  // insert never displaces a surviving sibling. Order the items by anything else and it is back.
  // biome-ignore lint/correctness/useExhaustiveDependencies: `groups` is what can hide a row.
  useLayoutEffect(() => {
    const was = held.current;
    if (was === null || was.node === null || document.activeElement !== document.body) return;
    if (was.node.isConnected && was.node.checkVisibility()) return;
    // The id stays: a remount put a new row there, and the reader can still Tab back to it.
    held.current = {node: null, id: was.id};
    scroller.current?.focus({preventScroll: true});
  }, [groups]);

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
    // `l` goes to the live edge, so the scroller keeps the focus rather than handing it back
    // to whichever row the cursor was on further up.
    held.current = null;
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
    // The cursor is the focused row, so it is announced like any other focus move, and there is
    // no second idea of "where the reader is" to keep in step with focus.
    const here = document.activeElement?.closest<HTMLElement>('[data-row]') ?? undefined;
    // Focus on the scroller rather than a row, after Shift+Tab out of one or a restore: the
    // cursor resumes where it was rather than at the edge of the log.
    const was = here ?? visible.find(row => row.id === held.current?.id);
    const at = was === undefined ? -1 : visible.indexOf(was);
    const move = (next: HTMLElement | undefined) => {
      if (next === undefined) return;
      // Focus first, without its scroll, then bring it into view within the log only: the page
      // itself never scrolls under the reader.
      next.focus({preventScroll: true});
      next.scrollIntoView({block: 'nearest'});
    };
    // Enter on a focused <summary> is the native toggle, which raises the click the fold
    // already listens for, so it is left alone.
    const enter = event.key === 'Enter' && here?.tagName !== 'SUMMARY';
    // A row that holds focus moves to its neighbour; resuming from the scroller lands on the
    // remembered row itself.
    const step = here === undefined ? 0 : 1;
    if (event.key === 'ArrowDown') move(at < 0 ? visible[0] : visible[at + step]);
    else if (event.key === 'ArrowUp') move(at < 0 ? visible.at(-1) : visible[at - step]);
    else if (here === undefined) return;
    else if (event.key === 'ArrowRight' || enter) {
      if (here.dataset['tool'] !== undefined) onSelect(here.dataset['tool']);
      else fold(here, true);
    } else if (event.key === 'ArrowLeft') {
      if (here.dataset['tool'] !== undefined) onSelect(null);
      else fold(here, false);
    } else if (event.key === 'Escape') {
      if (selected === null) return;
      onSelect(null);
    } else return;
    event.preventDefault();
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
        // The log's one tab stop, from which the arrows walk the rows. The rows themselves are
        // -1, so a round of four hundred calls is four hundred cursor steps and still one Tab.
        // biome-ignore lint/a11y/noNoninteractiveTabindex: a scrollable region must take focus to scroll by keyboard.
        tabIndex={0}
        onScroll={onScroll}
        onKeyDown={onKeyDown}
        onFocus={event => {
          const row = (event.target as HTMLElement).closest<HTMLElement>('[data-row]');
          if (row !== null) {
            held.current = {node: row, id: row.id};
            return;
          }
          // Only focus arriving from outside is a reader coming back. Focus arriving from a row
          // is Shift+Tab on its way out, and handing it back would make the log a one-way door.
          if (event.currentTarget.contains(event.relatedTarget)) return;
          // The scroller is the log's one tab stop and the cursor is focus, so a trip out and
          // back would otherwise lose it: Tab back in returns to the row it was on.
          const back = held.current === null ? null : document.getElementById(held.current.id);
          if (!back?.checkVisibility()) return;
          back.focus({preventScroll: true});
          // The log may have scrolled on since: bring the cursor back into view, clear of the
          // sticky role header, exactly as an arrow key does.
          back.scrollIntoView({block: 'nearest'});
        }}
        onBlur={event => {
          // Focus the reader moved themselves, off a row that is still there to hold it: the
          // cursor stays for a Tab back, but the restore above must not claim it.
          const was = held.current;
          if (was?.node != null && was.node === event.target && was.node.checkVisibility()) {
            held.current = {node: null, id: was.id};
          }
        }}
        // Delegated, so a row stays a row rather than becoming one tab stop each: the keyboard
        // path is the cursor this scroller carries.
        onClick={event => {
          const row = (event.target as HTMLElement).closest<HTMLElement>('[data-tool]');
          if (row?.dataset['tool'] === undefined) return;
          // The cursor follows the pointer, so arrows carry on from the row just clicked.
          row.focus({preventScroll: true});
          onSelect(row.dataset['tool']);
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
                <Group key={group.id} group={group} selected={selected} onOpen={leave} />
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
  selected,
  onOpen,
}: {
  group: LogGroup;
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
              <Item key={item.id} item={item} selected={selected} onOpen={onOpen} />
            ))}
            {rest.length === 0 ? null : (
              <Fold
                id={group.id}
                verb={group.summary || titleCase(group.role)}
                count={group.calls}
                items={rest}
                selected={selected}
                onOpen={onOpen}
              />
            )}
          </>
        ) : (
          group.items.map(item => (
            <Item key={item.id} item={item} selected={selected} onOpen={onOpen} />
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
  selected,
  onOpen,
}: {
  id: string;
  verb: string;
  count: number;
  items: LogItem[];
  selected: string | null;
  onOpen: () => void;
}) {
  return (
    <details className="fold">
      {/* biome-ignore lint/a11y/noStaticElementInteractions: <summary> is natively interactive. */}
      <summary
        id={rowId(id)}
        data-row=""
        // Natively tabbable, and the log has one tab stop: the arrows reach this row.
        tabIndex={-1}
        className="row"
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
          <Item key={item.id} item={item} selected={selected} onOpen={onOpen} />
        ))}
      </div>
    </details>
  );
}

function Item({
  item,
  selected,
  onOpen,
}: {
  item: LogItem;
  selected: string | null;
  onOpen: () => void;
}) {
  if (item.kind === 'run') {
    return (
      <Fold
        id={item.id}
        verb={item.verb}
        count={item.items.length}
        items={item.items}
        selected={selected}
        onOpen={onOpen}
      />
    );
  }
  if (item.kind === 'steer') {
    return (
      <p id={rowId(item.id)} data-row="" tabIndex={-1} className="said">
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
      <div id={rowId(item.id)} data-row="" tabIndex={-1} className="say">
        <Prose paragraphs={item.paragraphs} />
      </div>
    );
  }
  // The row shows the command's shape, sometimes none of it. What the row dropped is its
  // tooltip and, for a reader who cannot hover, its own DOM text. A row already showing its
  // whole command carries no tooltip: it would only repeat what the reader is looking at.
  const whole = item.argFull;
  const tip = whole !== null && whole.length > TIP_LIMIT ? `${whole.slice(0, TIP_LIMIT)}…` : whole;
  const marks = ['row', item.inFlight ? 'now' : '', item.id === selected ? 'sel' : ''];
  return (
    <div
      id={rowId(item.id)}
      data-row=""
      tabIndex={-1}
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
