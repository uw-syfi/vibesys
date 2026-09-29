import {Check, ChevronDown, Plus} from 'lucide-react';
import {useRef} from 'react';
import type {AskMessage, AskView, ModelGroup, ThreadRow} from '../ask.js';
import type {ProsePart} from '../model.js';
import type {Menu} from '../ui-state.js';
import {Composer} from './Composer.js';
import {PaneHead} from './Pane.js';
import {Prose} from './Prose.js';
import {Popover, useReturnFocus} from './TitleRow.js';

type Selection = {provider: string; model: string};

export interface AskTabProps {
  view: AskView;
  menu: Menu;
  draft: string;
  /** Why asking is off (the connection is down), or null. */
  reason: string | null;
  /** A thread that could not be started, or null. */
  error: string | null;
  onMenu: (menu: Menu) => void;
  onThread: (id: string) => void;
  onNewThread: (selection: Selection | null) => void;
  onDraft: (text: string) => void;
  onSend: (text: string) => Promise<boolean>;
  /** Asks the run again for its chat models after a failed check. */
  onRetry: () => void;
}

const NO_HARNESS = 'This run offers no chat harness.';

const runtime = (row: ThreadRow): string | null =>
  [row.provider, row.model].filter(part => part !== null).join(' · ') || null;

export function AskTab(props: AskTabProps) {
  const {view} = props;
  if (view.harness !== 'available' && view.threads.every(thread => thread.count === 0)) {
    return (
      <>
        <PaneHead scope="Run" />
        <HarnessLine harness={view.harness} onRetry={props.onRetry} />
      </>
    );
  }
  return (
    <>
      <ThreadHead {...props} />
      <Thread view={view} />
      {view.harness === 'available' ? (
        <AskDock {...props} />
      ) : (
        <HarnessLine harness={view.harness} onRetry={props.onRetry} />
      )}
    </>
  );
}

/** Why there is no composer: the harness is being checked, the check failed, or there is none. */
function HarnessLine({harness, onRetry}: {harness: AskView['harness']; onRetry: () => void}) {
  if (harness === 'failed')
    return (
      <p className="empty1">
        Couldn’t check the chat harness.{' '}
        <button
          type="button"
          className="linkish"
          title="Check the chat harness again"
          onClick={onRetry}
        >
          Retry
        </button>
      </p>
    );
  return (
    <p className="empty1">{harness === 'checking' ? 'Checking the chat harness…' : NO_HARNESS}</p>
  );
}

function ThreadHead({view, menu, onMenu, onThread, onNewThread}: AskTabProps) {
  const {current, threads} = view;
  const available = view.harness === 'available';
  const answeredBy = runtime(current);
  const trigger = useRef<HTMLButtonElement>(null);
  useReturnFocus(menu === 'thread', trigger);
  return (
    <div className="phead askhead">
      <span className="scope">Run</span>
      <button
        ref={trigger}
        type="button"
        className={menu === 'thread' ? 'disc on' : 'disc'}
        aria-label={`Thread: ${current.title}`}
        aria-haspopup="menu"
        aria-expanded={menu === 'thread'}
        title={answeredBy === null ? 'Switch thread' : `Switch thread (answered by ${answeredBy})`}
        onClick={() => onMenu(menu === 'thread' ? null : 'thread')}
      >
        <span className="ttl">{current.title}</span>
        <ChevronDown size={14} strokeWidth={1.5} aria-hidden />
      </button>
      {available ? (
        <span className="r">
          <button
            type="button"
            className="iconbtn"
            title="New thread"
            aria-label="New thread"
            onClick={() => onNewThread(null)}
          >
            <Plus size={16} strokeWidth={1.5} aria-hidden />
          </button>
        </span>
      ) : null}
      {menu === 'thread' ? (
        <Popover role="menu" label={{'aria-label': 'Threads'}} onClose={() => onMenu(null)}>
          <div className="gh">
            {threads.length === 1 ? '1 thread' : `${threads.length} threads`}
          </div>
          {threads.map(thread => (
            <button
              key={thread.id}
              type="button"
              role="menuitemradio"
              aria-checked={thread.id === current.id}
              aria-label={`${thread.title}, ${thread.count} ${thread.count === 1 ? 'question' : 'questions'}`}
              className={thread.id === current.id ? 'it on' : 'it'}
              title={runtime(thread) ?? undefined}
              onClick={() => onThread(thread.id)}
            >
              <span className="ttl">{thread.title}</span>
              <span className="d">{thread.count}</span>
            </button>
          ))}
        </Popover>
      ) : null}
    </div>
  );
}

function Thread({view}: Pick<AskTabProps, 'view'>) {
  const who = view.current.model ?? 'Answer';
  return (
    <div className="thread">
      {view.messages.length === 0 ? (
        <p className="t2">Ask about progress, a failure, or what a hypothesis changed.</p>
      ) : null}
      {view.messages.map(message => (
        <Exchange key={message.id} message={message} who={who} streaming={view.streaming} />
      ))}
    </div>
  );
}

function Exchange({
  message,
  who,
  streaming,
}: {
  message: AskMessage;
  who: string;
  streaming: ProsePart[][] | null;
}) {
  return (
    <>
      <div className="human">{message.question}</div>
      {message.error !== null ? (
        <p className="hint bad" role="alert">{`Not answered: ${message.error}`}</p>
      ) : (
        <div className="answer" aria-live="polite">
          <div className="who2">{who}</div>
          {message.answer !== null ? (
            <Prose paragraphs={message.answer} />
          ) : streaming !== null ? (
            <Prose paragraphs={streaming} />
          ) : (
            <p className="t2">
              <span className="spin" /> Answering…
            </p>
          )}
        </div>
      )}
    </>
  );
}

function AskDock({
  view,
  menu,
  draft,
  reason,
  error,
  onMenu,
  onNewThread,
  onDraft,
  onSend,
}: AskTabProps) {
  const {current} = view;
  const chip = useRef<HTMLButtonElement>(null);
  useReturnFocus(menu === 'model', chip);
  const pick = (provider: string, model: string) =>
    provider === current.provider && model === current.model
      ? onMenu(null)
      : onNewThread({provider, model});
  return (
    <div className="pdock">
      <Composer
        id="ask"
        label="Ask about this run"
        placeholder={reason ?? 'Ask about this run…'}
        draft={draft}
        onDraft={onDraft}
        disabled={reason !== null}
        held={view.pending}
        error={error}
        onSend={onSend}
      >
        <button
          ref={chip}
          type="button"
          className="mchip"
          aria-label={`Chat model: ${current.model ?? 'run default'}`}
          aria-haspopup="menu"
          aria-expanded={menu === 'model'}
          title="Choosing a model starts a new thread"
          onClick={() => onMenu(menu === 'model' ? null : 'model')}
        >
          {current.model ?? 'Model'}
          <ChevronDown size={14} strokeWidth={1.5} aria-hidden />
        </button>
      </Composer>
      {menu === 'model' ? (
        <ModelMenu
          groups={view.groups}
          current={current}
          onPick={pick}
          onClose={() => onMenu(null)}
        />
      ) : null}
    </div>
  );
}

function ModelMenu(props: {
  groups: ModelGroup[];
  current: ThreadRow;
  onPick: (provider: string, model: string) => void;
  onClose: () => void;
}) {
  const {groups, current, onPick, onClose} = props;
  return (
    <Popover role="menu" label={{'aria-label': 'Chat model'}} onClose={onClose}>
      {groups.map(group => (
        <div key={group.provider}>
          <div className="gh">{group.label}</div>
          {group.models.map(model => {
            const on = group.provider === current.provider && model === current.model;
            return (
              <button
                key={model}
                type="button"
                role="menuitemradio"
                aria-checked={on}
                className={on ? 'it on' : 'it'}
                onClick={() => onPick(group.provider, model)}
              >
                {model}
                {on ? <Check size={14} strokeWidth={1.5} className="d" aria-hidden /> : null}
              </button>
            );
          })}
        </div>
      ))}
    </Popover>
  );
}
