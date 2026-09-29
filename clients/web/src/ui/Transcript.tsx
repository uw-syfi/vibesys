import {Check, ChevronRight} from 'lucide-react';
import {Fragment, type ReactNode, useLayoutEffect, useRef, useState} from 'react';
import type {ResultPart, RoundRow} from '../rounds.js';
import type {RoundTranscript, ToolDetail, ToolRow, Turn, TurnItem} from '../transcript.js';
import {Diff} from './Diff.js';
import {Prose} from './Prose.js';

/** What the rows need from the window: which row is open, and how to open it. */
export interface TranscriptControls {
  expanded: string | null;
  disclosed: Readonly<Record<string, boolean>>;
  detail: (id: string) => ToolDetail | null;
  onExpand: (id: string) => void;
  onDisclose: (key: string) => void;
}

export interface TranscriptProps {
  round: number | null;
  row: RoundRow | null;
  result: ResultPart[];
  model: RoundTranscript | null;
  /** The live round of a running run: keep the newest turn in view while the reader is there. */
  follow: boolean;
  history: {loading: boolean; error: string | null; onRetry: () => void};
  empty: string;
  controls: TranscriptControls;
  /** The execution the transcript is filtered to (Agents pane); null shows every turn. */
  only: string | null;
  onShowAll: () => void;
}

/**
 * Opens each round at its newest turn, then, on the live round, keeps the bottom in view through
 * every size change (new rows, an expanded tool call, a disclosed prompt, a resized pane) until
 * the reader scrolls away; scrolling back to the bottom resumes following.
 */
function useFollow(round: number | null, model: RoundTranscript | null, follow: boolean) {
  const ref = useRef<HTMLDivElement>(null);
  const [away, setAway] = useState(false);
  const [opened, setOpened] = useState(round);
  if (opened !== round) {
    setOpened(round);
    setAway(false);
  }
  useLayoutEffect(() => {
    const node = ref.current;
    if (node !== null && round !== null) node.scrollTop = node.scrollHeight;
  }, [round]);
  useLayoutEffect(() => {
    const node = ref.current;
    if (node === null || model === null || !follow || away) return;
    const pin = () => {
      node.scrollTop = node.scrollHeight;
    };
    pin();
    const observer = new ResizeObserver(pin);
    observer.observe(node);
    for (const child of node.children) observer.observe(child);
    return () => observer.disconnect();
  }, [model, follow, away]);
  const onScroll = () => {
    const node = ref.current;
    if (node !== null) setAway(node.scrollHeight - node.scrollTop - node.clientHeight > 40);
  };
  return {ref, onScroll};
}

export function Transcript(props: TranscriptProps) {
  const {round, row, result, model, history, empty, controls, only, onShowAll} = props;
  const follow = useFollow(round, model, props.follow);
  if (round === null || model === null) {
    return (
      <div className="scroll">
        <p className="empty1">{empty}</p>
      </div>
    );
  }
  const turns = only === null ? model.turns : model.turns.filter(turn => turn.id === only);
  const shown = turns[0];
  return (
    <div className="scroll" ref={follow.ref} onScroll={follow.onScroll}>
      <div className="sticky">
        <div className="in">
          <span className="rn">Round {round}</span>
          <span className="ttl" title={row?.title ?? undefined}>
            {row?.title ?? 'No hypothesis yet'}
          </span>
          <Result parts={result} />
        </div>
      </div>
      <div className="col">
        <History history={history} />
        <p className="claim">
          {row?.hypothesis ?? 'The hypothesis appears once the orchestrator forms one.'}
        </p>
        {only !== null && shown !== undefined ? (
          <div className="filterbar">
            Showing only {shown.role}
            {shown.phase === '' ? '' : ` (${shown.phase.toLowerCase()})`}
            <button type="button" className="linkbtn" onClick={onShowAll}>
              Show all
            </button>
          </div>
        ) : null}
        {turns.map(turn => (
          <TurnView key={turn.id} turn={turn} controls={controls} />
        ))}
        {model.turns.length === 0 ? (
          <p className="endline">No agent calls in this round yet.</p>
        ) : null}
        {model.queued.map(steer => (
          <Steer key={steer.id} text={steer.text} applied={false} />
        ))}
      </div>
    </div>
  );
}

function Result({parts}: {parts: ResultPart[]}) {
  return (
    <span className="res">
      {parts.map((part, index) => (
        // biome-ignore lint/suspicious/noArrayIndexKey: the parts of one result line never reorder, and two can share a text.
        <Fragment key={index}>
          {index > 0 && !part.joined ? <span className="sep">·</span> : null}
          <span className={part.tone ?? undefined}>{part.text}</span>
        </Fragment>
      ))}
    </span>
  );
}

function History({history}: {history: TranscriptProps['history']}) {
  if (history.error !== null) {
    return (
      <p className="endline bad" role="alert">
        Earlier events did not load: {history.error}
        <button type="button" className="linkbtn" onClick={history.onRetry}>
          Retry
        </button>
      </p>
    );
  }
  if (!history.loading) return null;
  return (
    <p className="endline">
      <span className="spin" />
      Loading earlier events…
    </p>
  );
}

function TurnView({turn, controls}: {turn: Turn; controls: TranscriptControls}) {
  return (
    <section
      className="turn"
      aria-label={turn.phase === '' ? turn.role : `${turn.role}, ${turn.phase}`}
      data-turn={turn.id}
    >
      <div className="who" title={turn.hint}>
        <span className={turn.active ? 'r act' : 'r'}>{turn.role}</span>
        <span className="ph">{turn.phase}</span>
        <Disclosures turn={turn} controls={controls} />
      </div>
      <DisclosureBodies turn={turn} controls={controls} />
      {turn.items.map(item => (
        <Item key={item.id} item={item} controls={controls} />
      ))}
      {turn.verdict === null ? null : <Verdict verdict={turn.verdict} />}
      {turn.working === null ? null : (
        <div className="working">
          <span className="spin" />
          <span>
            {turn.working.lead}
            {turn.working.detail === null ? null : (
              <>
                {' '}
                <b>{turn.working.detail}</b>
              </>
            )}
          </span>
        </div>
      )}
    </section>
  );
}

const promptKey = (turn: Turn) => `${turn.id}:prompt`;
const todosKey = (turn: Turn) => `${turn.id}:todos`;

function Disclosure({
  open,
  onClick,
  children,
}: {
  open: boolean;
  onClick: () => void;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      className={open ? 'disc on' : 'disc'}
      aria-expanded={open}
      onClick={onClick}
    >
      <ChevronRight
        size={12}
        strokeWidth={1.75}
        className={open ? 'chev open' : 'chev'}
        aria-hidden
      />
      {children}
    </button>
  );
}

/** Prompt and Todos toggles for one execution; nothing when it recorded neither. */
export function Disclosures({turn, controls}: {turn: Turn; controls: TranscriptControls}) {
  if (turn.prompt === null && turn.todos.length === 0) return null;
  return (
    <span className="acts">
      {turn.prompt === null ? null : (
        <Disclosure
          open={controls.disclosed[promptKey(turn)] === true}
          onClick={() => controls.onDisclose(promptKey(turn))}
        >
          Prompt
        </Disclosure>
      )}
      {turn.todos.length === 0 ? null : (
        <Disclosure
          open={controls.disclosed[todosKey(turn)] === true}
          onClick={() => controls.onDisclose(todosKey(turn))}
        >
          Todos ({turn.todos.length})
        </Disclosure>
      )}
    </span>
  );
}

export function DisclosureBodies({turn, controls}: {turn: Turn; controls: TranscriptControls}) {
  const prompt = controls.disclosed[promptKey(turn)] === true ? turn.prompt : null;
  const todos = controls.disclosed[todosKey(turn)] === true ? turn.todos : [];
  return (
    <>
      {prompt === null ? null : (
        <div className="block">
          <div className="bh">Prompt sent to {turn.role.toLowerCase()}</div>
          <pre>{prompt}</pre>
        </div>
      )}
      {todos.length === 0 ? null : (
        <div className="block">
          <div className="bh">Todos</div>
          {todos.map(todo => (
            <div className="todo" key={todo.content}>
              {todo.status === 'completed' ? (
                <Check size={12} strokeWidth={1.75} className="ok" role="img" aria-label="done" />
              ) : (
                <span className="gl" role="img" aria-label={todo.status}>
                  ○
                </span>
              )}
              {todo.content}
            </div>
          ))}
        </div>
      )}
    </>
  );
}

function Item({item, controls}: {item: TurnItem; controls: TranscriptControls}) {
  if (item.kind === 'prose') return <Prose paragraphs={item.paragraphs} />;
  if (item.kind === 'steer') return <Steer text={item.text} applied />;
  return (
    <div className="actions">
      {item.tools.map(tool => (
        <ToolLine key={tool.id} tool={tool} controls={controls} />
      ))}
    </div>
  );
}

function ToolLine({tool, controls}: {tool: ToolRow; controls: TranscriptControls}) {
  const open = controls.expanded === tool.id;
  const detail = open ? controls.detail(tool.id) : null;
  return (
    <div className={open ? 'tool open' : 'tool'}>
      <button
        type="button"
        className="trow"
        aria-expanded={open}
        title={tool.hint}
        onClick={() => controls.onExpand(tool.id)}
      >
        <span className="verb">{tool.verb}</span>
        {tool.object === null ? null : <span className="obj">{tool.object}</span>}
        {tool.detail === null ? null : <span className="sub">{tool.detail}</span>}
        <LineCounts tool={tool} />
        <span className="end">
          <ToolEnd tool={tool} />
        </span>
        <ChevronRight size={12} strokeWidth={1.5} className="chev" aria-hidden />
      </button>
      {detail === null ? null : <ToolOutput detail={detail} />}
    </div>
  );
}

function LineCounts({tool}: {tool: ToolRow}) {
  if (tool.added === null && tool.removed === null) return null;
  return (
    <span className="stat">
      {tool.added === null ? null : <span className="ok">+{tool.added}</span>}
      {tool.removed === null ? null : (
        <>
          {' '}
          <span className="bad">−{tool.removed}</span>
        </>
      )}
    </span>
  );
}

function ToolEnd({tool}: {tool: ToolRow}) {
  if (tool.inFlight) return <span className="spin" role="img" aria-label="running" />;
  if (tool.end === null) return null;
  return <span className={tool.end.kind === 'exit' ? 'bad' : 't2'}>{tool.end.text}</span>;
}

function ToolOutput({detail}: {detail: ToolDetail}) {
  if (detail.kind === 'diff')
    return (
      <div className="out">
        <Diff lines={detail.lines} />
      </div>
    );
  return (
    <div className="out">
      <pre>
        {detail.command === null ? null : <span className="cmd">{`$ ${detail.command}\n`}</span>}
        {detail.pending ? (
          <span className="t2">Waiting for output…</span>
        ) : (
          detail.lines.map((line, index) => (
            // biome-ignore lint/suspicious/noArrayIndexKey: lines of one output never reorder.
            <span key={index} className={line.failed ? 'fl' : undefined}>{`${line.text}\n`}</span>
          ))
        )}
        {detail.cut === null ? null : (
          <span className="t2">{`${detail.cut} more characters not shown`}</span>
        )}
      </pre>
    </div>
  );
}

function Verdict({verdict}: {verdict: NonNullable<Turn['verdict']>}) {
  return (
    <section className="verdict" aria-label="Judge verdict">
      <div className={verdict.accepted ? 'vword ok' : 'vword bad'}>
        <span className={verdict.accepted ? 'dot okd' : 'dot err'} />
        {verdict.accepted ? 'Accepted' : 'Rejected'}
      </div>
      <p>{verdict.feedback}</p>
    </section>
  );
}

function Steer({text, applied}: {text: string; applied: boolean}) {
  return (
    <div>
      <div className="human">{text}</div>
      <div className="hmeta">
        {applied ? (
          <>
            <Check size={12} strokeWidth={1.75} className="ok" aria-hidden />
            Applied to the next agent call
          </>
        ) : (
          <>
            <span className="spin" />
            Queued for the next agent call
          </>
        )}
      </div>
    </div>
  );
}
