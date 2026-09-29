import type {DesignRound} from '@vibesys/backend-client';
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
import type {RoundRow} from '../rounds.js';
import {Diff} from './Diff.js';
import {PaneHead} from './Pane.js';

interface FileProps {
  file: FileChanges;
  copied: string | null;
  onCopy: (command: string) => void;
}

function Reproduce({text, file, copied, onCopy}: FileProps & {text: string}) {
  return (
    <div className="note">
      <span>{text}</span>
      <button
        type="button"
        className="chip"
        title={file.command}
        onClick={() => onCopy(file.command)}
      >
        {copied === file.command ? (
          'Copied'
        ) : (
          <>
            Copy <span className="mono">{file.command}</span>
          </>
        )}
      </button>
    </div>
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
          {body.truncated ? (
            <Reproduce {...props} text="Patch truncated at the server's size bound." />
          ) : null}
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
  copied: string | null;
  onCopy: (command: string) => void;
}) {
  switch (model.kind) {
    case 'none':
      return <p className="empty1">No round has started yet.</p>;
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
}

export function ChangesTab({row, design, loading, error, onRetry, loadPatch}: ChangesTabProps) {
  const requests = useMemo(() => patchRequests(design), [design]);
  const patches = usePatches(requests, loadPatch);
  const [copied, setCopied] = useState<string | null>(null);
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
    void navigator.clipboard.writeText(command).then(() => setCopied(command));
  };
  return <Changes model={changesModel(row, design, patches)} copied={copied} onCopy={copy} />;
}
