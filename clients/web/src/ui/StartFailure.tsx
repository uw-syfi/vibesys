/** Why a run did not start (mockup #startfail): the message, the run server's stderr, Retry. */
import {useState} from 'react';
import {type FileLocation, type LaunchFailure, locationText, tailParts} from '../launch.js';

export interface StartFailureProps {
  failure: LaunchFailure;
  /** The project root, to make relative file locations absolute. */
  root: string | null;
  backLabel: string;
  onRetry: () => void;
  onBack: () => void;
}

type Copy = (text: string) => void;

function TailPiece({
  part,
  root,
  onCopy,
}: {
  part: string | FileLocation;
  root: string | null;
  onCopy: Copy;
}) {
  if (typeof part === 'string') return part;
  const text = locationText(root, part);
  return (
    <button type="button" className="linkish" title={`Copy ${text}`} onClick={() => onCopy(text)}>
      {part.text}
    </button>
  );
}

function TailLine({line, root, onCopy}: {line: string; root: string | null; onCopy: Copy}) {
  return (
    <>
      {tailParts(line).map((part, index) => (
        // biome-ignore lint/suspicious/noArrayIndexKey: a printed line's parts never reorder.
        <TailPiece key={index} part={part} root={root} onCopy={onCopy} />
      ))}
      {'\n'}
    </>
  );
}

export function StartFailure({failure, root, backLabel, onRetry, onBack}: StartFailureProps) {
  const [copied, setCopied] = useState<string | null>(null);
  const copy: Copy = text => {
    void navigator.clipboard.writeText(text).then(() => setCopied(text));
  };
  const log = failure.log;
  return (
    <div className="scroll">
      <div className="col failure">
        <p className="claim">{`${failure.message} Your settings are kept.`}</p>
        {failure.tail.length === 0 ? null : (
          <div className="out">
            <pre>
              {failure.tail.map((line, index) => (
                // biome-ignore lint/suspicious/noArrayIndexKey: the tail is fixed once shown.
                <TailLine key={index} line={line} root={root} onCopy={copy} />
              ))}
            </pre>
          </div>
        )}
        {log === null ? null : (
          <p className="logline">
            Full log{' '}
            <button
              type="button"
              className="linkish mono"
              title="Copy the path"
              onClick={() => copy(log)}
            >
              {log}
            </button>
          </p>
        )}
        <div className="failacts">
          <button type="button" className="btn primary" onClick={onRetry}>
            Retry
          </button>
          <button type="button" className="btn ghost" onClick={onBack}>
            {backLabel}
          </button>
          <span className="t2" aria-live="polite">
            {copied === null ? '' : `Copied ${copied}`}
          </span>
        </div>
      </div>
    </div>
  );
}
