import {Check, Pause, Plus, Search, Square, Undo2} from 'lucide-react';
import type {ReactNode} from 'react';
import {type HomeRun, type ProjectSection, relativeTime} from '../home.js';
import {type RoundRow, type RunSummary, signed} from '../rounds.js';

/** Bold enough that kept and reverted read apart at a glance (a 12px stroke thinner than this greys out). */
const GLYPH = {size: 12, strokeWidth: 2.5} as const;

function RoundGlyph({state}: {state: RoundRow['state']}) {
  if (state === 'running') {
    return (
      <span className="gl">
        <span className="dot live" role="img" aria-label="running" />
      </span>
    );
  }
  if (state === 'kept') {
    return (
      <span className="gl">
        <Check {...GLYPH} className="ok" role="img" aria-label="kept" />
      </span>
    );
  }
  const label = state === 'failed' ? 'failed' : 'reverted';
  return (
    <span className="gl">
      <Undo2 {...GLYPH} className="bad" role="img" aria-label={label} />
    </span>
  );
}

function RunGlyph({outcome}: {outcome: HomeRun['outcome']}) {
  switch (outcome) {
    case 'running':
      return (
        <span className="gl">
          <span className="dot live" role="img" aria-label="running" />
        </span>
      );
    case 'paused':
      return (
        <span className="gl">
          <Pause {...GLYPH} fill="currentColor" className="warn" role="img" aria-label="paused" />
        </span>
      );
    case 'failed':
      return (
        <span className="gl">
          <span className="dot err" role="img" aria-label="failed" />
        </span>
      );
    case 'completed':
      return (
        <span className="gl">
          <Check {...GLYPH} className="t2" role="img" aria-label="finished" />
        </span>
      );
    case 'stopped':
      return (
        <span className="gl">
          <Square {...GLYPH} className="t2" role="img" aria-label="stopped" />
        </span>
      );
    default:
      return <span className="gl" />;
  }
}

export interface SidebarProps {
  width: number;
  sections: ProjectSection[];
  /** The page's own run: it lists its rounds. */
  current: string | null;
  summary: RunSummary;
  /** A line under the open run's rounds, in place of the planned count (the project has not attached). */
  note?: string | null;
  selected: number | null;
  now: Date;
  onRound: (round: number) => void;
  /** The top row's controls (the hide button). */
  head?: ReactNode;
  /** Navigation rows under the top row (Search and commands). */
  nav?: ReactNode;
  resizer?: ReactNode;
}

export function Sidebar({
  width,
  sections,
  current,
  summary,
  note = null,
  selected,
  now,
  onRound,
  head,
  nav,
  resizer,
}: SidebarProps) {
  return (
    <nav className="side" style={{width}} aria-label="Runs">
      <div className="sidehead">{head}</div>
      {nav}
      <div className="sidescroll">
        {sections.map(section => (
          <section key={section.id} aria-label={section.name}>
            <div className="sect">{section.name}</div>
            {section.runs.map(run =>
              run.id === current ? (
                <CurrentRun
                  key={run.id}
                  run={run}
                  summary={summary}
                  note={note}
                  selected={selected}
                  now={now}
                  onRound={onRound}
                />
              ) : (
                <OtherRun key={run.id} run={run} now={now} />
              ),
            )}
          </section>
        ))}
      </div>
      {resizer}
    </nav>
  );
}

function CurrentRun({
  run,
  summary,
  note,
  selected,
  now,
  onRound,
}: {
  run: HomeRun;
  summary: RunSummary;
  note: string | null;
  selected: number | null;
  now: Date;
  onRound: (round: number) => void;
}) {
  return (
    <>
      <div className="run cur" title={runHint(run)}>
        <RunGlyph outcome={run.outcome} />
        <span className="ttl">{run.title}</span>
        <span className="meta">
          {run.outcome === 'running' ? 'now' : relativeTime(run.updatedAt, now)}
        </span>
      </div>
      <div className="rounds">
        {summary.rows.map(row => (
          <button
            key={row.round}
            type="button"
            className={row.round === selected ? 'rnd sel' : 'rnd'}
            aria-current={row.round === selected ? 'true' : undefined}
            aria-label={`Round ${row.round}, ${row.state}: ${row.title ?? 'no hypothesis yet'}${row.delta === null ? '' : `, ${signed(row.delta)}`}`}
            title={`Round ${row.round}: ${row.hypothesis ?? row.title ?? 'no hypothesis yet'}`}
            onClick={() => onRound(row.round)}
          >
            <RoundGlyph state={row.state} />
            <span className="rn">r{row.round}</span>
            <span className="ttl">{row.title ?? 'No hypothesis yet'}</span>
            <span className="d">{row.delta === null ? '' : signed(row.delta)}</span>
          </button>
        ))}
        {note !== null ? (
          <div className="rnd more">{note}</div>
        ) : summary.planned ? (
          <div className="rnd more">{summary.planned} more planned</div>
        ) : null}
      </div>
    </>
  );
}

/** The run row's hint: its title, and its gateway's state unless that is simply live. */
function runHint(run: HomeRun): string {
  return run.gateway === 'live'
    ? run.title
    : `${run.title}\nGateway: ${run.gateway.replaceAll('_', ' ')}`;
}

function OtherRun({run, now}: {run: HomeRun; now: Date}) {
  const body = (
    <>
      <RunGlyph outcome={run.outcome} />
      <span className="ttl">{run.title}</span>
      <span className="meta">{relativeTime(run.updatedAt, now)}</span>
    </>
  );
  if (run.url === null) {
    return (
      <div className="run" title={`${runHint(run)}\nNot reachable from this page`}>
        {body}
      </div>
    );
  }
  return (
    <a className="run" href={run.url} title={runHint(run)}>
      {body}
    </a>
  );
}

/** Opens the ⌘K palette. */
export function SearchRow({onOpen}: {onOpen: () => void}) {
  return (
    <button type="button" className="nav" onClick={onOpen}>
      <Search size={16} strokeWidth={1.5} aria-hidden />
      Search and commands
      <span className="kbd">⌘K</span>
    </button>
  );
}

/** The sidebar's first row: start another run (mockup `#home`). */
export function NewRunRow({href, on}: {href: string; on: boolean}) {
  return (
    <a className={on ? 'nav on' : 'nav'} href={href} aria-current={on ? 'page' : undefined}>
      <Plus size={16} strokeWidth={1.5} aria-hidden />
      New run
      <span className="kbd">⌘N</span>
    </a>
  );
}
