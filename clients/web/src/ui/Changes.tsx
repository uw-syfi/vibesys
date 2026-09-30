import type {DesignRound} from '@vibesys/backend-client';
import {ChevronRight} from 'lucide-react';
import {useEffect, useMemo, useRef, useState} from 'react';
import {
  type ChangesModel,
  changesModel,
  type FileChanges,
  type LoadPatch,
  loadPatchSlot,
  type PatchRequest,
  type PatchSlot,
  patchRequests,
} from '../changes.js';
import {copyText} from '../clipboard.js';
import type {RoundRow} from '../rounds.js';
import type {LineStat} from '../transcript.js';
import {Diff} from './Diff.js';
import {PaneHead} from './Pane.js';

/** The last command a Copy button sent to the clipboard, and whether it landed. */
export interface Copied {
  command: string;
  ok: boolean;
}

interface FileProps {
  file: FileChanges;
  copied: Copied | null;
  onCopy: (command: string) => void;
}

/**
 * The note, then the copy button on its own line. The button only copies: the full command is
 * secondary detail and lives in its `title` hint, never inline.
 */
function Reproduce({text, file, copied, onCopy}: FileProps & {text: string}) {
  return (
    <div className="note stack">
      <span>{text}</span>
      <button
        type="button"
        className="chip"
        title={file.command}
        onClick={() => onCopy(file.command)}
      >
        {copied?.command === file.command
          ? copied.ok
            ? 'Copied'
            : "Couldn't copy"
          : 'Copy command'}
      </button>
    </div>
  );
}

/** A truncated patch: how much it leaves out, and on opening, how to get the rest. */
function Truncated(props: FileProps) {
  const [open, setOpen] = useState(false);
  const {hidden} = props.file;
  return (
    <>
      <button type="button" className="morerow" aria-expanded={open} onClick={() => setOpen(!open)}>
        <ChevronRight
          size={12}
          strokeWidth={1.75}
          className={open ? 'chev open' : 'chev'}
          aria-hidden
        />
        {hidden === null ? 'More changed lines' : `${hidden} more changed lines`}
      </button>
      {open ? <Reproduce {...props} text="Patch truncated at the server's size bound." /> : null}
    </>
  );
}

function FileBodyView(props: FileProps) {
  const {body} = props.file;
  switch (body.kind) {
    case 'loading':
      return <div className="note">Loading patch…</div>;
    case 'error':
      return <Reproduce {...props} text={`The patch query failed: ${body.message}`} />;
    case 'unavailable':
      return <Reproduce {...props} text="The workspace repository could not produce this patch." />;
    case 'patch':
      return (
        <>
          {body.lines.length === 0 ? (
            <div className="note">No content change (mode or rename only).</div>
          ) : (
            <Diff lines={body.lines} />
          )}
          {body.truncated ? <Truncated {...props} /> : null}
        </>
      );
  }
}

function FileSection(props: FileProps) {
  const {file} = props;
  return (
    <section aria-label={file.path}>
      <div className="fhead">
        <span className="mono">
          {file.renamedFrom === null ? file.path : `${file.renamedFrom} → ${file.path}`}
        </span>
        {file.body.kind === 'patch' ? (
          <span className="stat num">
            <span className="ok">+{file.added}</span> <span className="bad">−{file.removed}</span>
          </span>
        ) : null}
      </div>
      <FileBodyView {...props} />
    </section>
  );
}

export function Changes({
  model,
  copied,
  onCopy,
}: {
  model: ChangesModel;
  copied: Copied | null;
  onCopy: (command: string) => void;
}) {
  switch (model.kind) {
    case 'none':
      return (
        <>
          <PaneHead scope="Run" />
          <p className="empty1">No round results yet.</p>
        </>
      );
    case 'running':
      return (
        <>
          <PaneHead scope={`Round ${model.round}`} />
          <p className="empty1">Changes appear when round {model.round} finishes.</p>
        </>
      );
    case 'unresolved':
      return (
        <>
          <PaneHead scope={`Round ${model.round}`} />
          <p className="empty1">No change range was recorded for round {model.round}.</p>
        </>
      );
    case 'files':
      return (
        <>
          <PaneHead scope={`Round ${model.round}`}>
            <span>against {model.against}</span>
            {model.commit === null ? null : (
              <span className="r mono" title="The round's commit">
                {model.commit}
              </span>
            )}
          </PaneHead>
          {model.files.length === 0 ? (
            <p className="empty1">
              Round {model.round} changed no files outside framework bookkeeping.
            </p>
          ) : null}
          {model.files.map(file => (
            <FileSection key={file.path} file={file} copied={copied} onCopy={onCopy} />
          ))}
        </>
      );
  }
}

/** Fetches each requested patch once per page (commit ranges never change); a failure may retry. */
function usePatches(
  requests: readonly PatchRequest[],
  load: LoadPatch,
): Readonly<Record<string, PatchSlot>> {
  const [slots, setSlots] = useState<Record<string, PatchSlot>>({});
  const asked = useRef(new Set<string>());
  useEffect(() => {
    for (const request of requests) {
      if (asked.current.has(request.key)) continue;
      asked.current.add(request.key);
      setSlots(previous => ({...previous, [request.key]: {kind: 'loading'}}));
      void loadPatchSlot(load, request).then(slot => {
        if (slot.kind === 'error') asked.current.delete(request.key);
        setSlots(previous => ({...previous, [request.key]: slot}));
      });
    }
  }, [requests, load]);
  return slots;
}

export interface ChangesTabProps {
  row: RoundRow | undefined;
  design: DesignRound | undefined;
  /** The design query has not answered yet. */
  loading: boolean;
  error: string | null;
  onRetry: () => void;
  loadPatch: LoadPatch;
  /** The round's edits per file, which count what a truncated patch leaves out. */
  edits: ReadonlyMap<string, LineStat>;
}

export function ChangesTab({
  row,
  design,
  loading,
  error,
  onRetry,
  loadPatch,
  edits,
}: ChangesTabProps) {
  const requests = useMemo(() => patchRequests(design), [design]);
  const patches = usePatches(requests, loadPatch);
  const [copied, setCopied] = useState<Copied | null>(null);
  const scope = row === undefined ? 'Run' : `Round ${row.round}`;
  if (design === undefined && error !== null) {
    return (
      <>
        <PaneHead scope={scope} />
        <div className="note">
          <span>Changes did not load: {error}</span>
          <button type="button" className="chip" onClick={onRetry}>
            Retry
          </button>
        </div>
      </>
    );
  }
  if (design === undefined && loading) {
    return (
      <>
        <PaneHead scope={scope} />
        <p className="empty1">Loading changes…</p>
      </>
    );
  }
  const copy = (command: string) => {
    void copyText(command).then(ok => setCopied({command, ok}));
  };
  return (
    <Changes model={changesModel(row, design, patches, edits)} copied={copied} onCopy={copy} />
  );
}
