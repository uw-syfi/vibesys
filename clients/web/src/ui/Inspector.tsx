import {
  ArrowDown,
  ArrowUp,
  ChevronRight,
  FileIcon,
  FileMinus,
  FilePen,
  FilePlus,
  FileSymlink,
  type LucideIcon,
  X,
} from 'lucide-react';
import {useEffect, useRef} from 'react';
import {prose} from '../derive.js';
import type {InspectorModel, JudgeAttempt, Verdict} from '../model.js';
import {Prose} from './Prose.js';
import './Inspector.css';

export interface InspectorProps {
  model: InspectorModel | null;
  /** >= 1024 px: an aside. 768-1023 px: a right drawer. < 768 px: a bottom sheet. */
  mode: 'aside' | 'drawer' | 'sheet';
  /** Drawer and sheet only. */
  open: boolean;
  /**
   * True only while the selected round's backfill is in flight: its verdicts are not loaded yet,
   * so the Judge section renders skeleton rows with `aria-busy="true"`. False after a failed
   * backfill; the Judge section then follows its normal empty rule, and the log carries the
   * error and Retry.
   */
  judgePending: boolean;
  designError: string | null;
  onClose: () => void;
  onRetryDesign: () => void;
}

const icon = {size: 16, strokeWidth: 1.75, 'aria-hidden': true} as const;
const VERDICT_CLASS: Record<Verdict, string> = {
  'Gate failed': 'st-failed',
  Rejected: 'st-rejected',
  Kept: 'st-kept',
  Passed: 'st-rejected',
};
// A kind this build does not know gets a neutral icon and word instead of crashing the render.
const CHANGE = new Map<string, readonly [LucideIcon, string]>([
  ['added', [FilePlus, 'Added']],
  ['modified', [FilePen, 'Modified']],
  ['deleted', [FileMinus, 'Deleted']],
  ['renamed', [FileSymlink, 'Renamed']],
]);
const UNKNOWN_CHANGE = [FileIcon, 'Changed'] as const;

export function Inspector({
  model,
  mode,
  open,
  judgePending,
  designError,
  onClose,
  onRetryDesign,
}: InspectorProps) {
  const dialog = useRef<HTMLDialogElement>(null);
  // Whether the pointer went down on the backdrop: a selection dragged out of the panel ends in a
  // click on the dialog too, and must not close it.
  const pressed = useRef(false);
  // Crossing 1024 px swaps the dialog for the aside and back; the new dialog node must reopen, or
  // `open` stays true with nothing shown and no row click can open it again.
  // biome-ignore lint/correctness/useExhaustiveDependencies: `mode` re-runs this for the new node.
  useEffect(() => {
    const node = dialog.current;
    if (node === null) return;
    if (open && !node.open) node.showModal();
    else if (!open && node.open) node.close();
  }, [open, mode]);

  const label = model === null ? 'Round details' : `Round ${model.round} details`;
  const body =
    model === null ? null : (
      // Keyed by round: a new round starts from its own attempts' open state, not the DOM's.
      <Body
        key={model.round}
        model={model}
        judgePending={judgePending}
        designError={designError}
        onRetryDesign={onRetryDesign}
      />
    );
  if (mode === 'aside') {
    return (
      <aside className="insp" aria-label={label}>
        {body}
      </aside>
    );
  }
  return (
    // biome-ignore lint/a11y/useKeyWithClickEvents: the click is on the backdrop; Esc closes natively.
    <dialog
      ref={dialog}
      className={`insp insp-${mode}`}
      aria-label={label}
      onClose={onClose}
      onPointerDown={event => {
        pressed.current = event.target === event.currentTarget;
      }}
      onClick={event => {
        if (pressed.current && event.target === event.currentTarget) event.currentTarget.close();
      }}
    >
      <div className="insp-inner">
        <div className="sheet-bar">
          <button
            type="button"
            className="btn btn-ghost btn-icon"
            aria-label="Close"
            onClick={() => dialog.current?.close()}
          >
            <X {...icon} />
          </button>
        </div>
        {body}
      </div>
    </dialog>
  );
}

function Body({
  model,
  judgePending,
  designError,
  onRetryDesign,
}: {
  model: InspectorModel;
  judgePending: boolean;
  designError: string | null;
  onRetryDesign: () => void;
}) {
  const {hypothesis, metric, delta, judge, changes} = model;
  return (
    <div className="insp-body">
      {hypothesis === null ? null : (
        // Not <header>: inside the dialog that would be a second banner landmark.
        <div>
          {hypothesis.id === null ? null : <p className="hid mono">{hypothesis.id}</p>}
          {hypothesis.title === null ? null : <h2>{hypothesis.title}</h2>}
          {hypothesis.claim === null ? null : (
            <div className="claim">
              <Prose paragraphs={prose(hypothesis.claim)} />
            </div>
          )}
        </div>
      )}
      {metric === null && delta === null ? null : (
        <section className="sec" aria-label="Measurement">
          {metric === null ? null : (
            <h3>
              <span className="mono">{metric.name}</span>
              {metric.direction === null ? null : (
                <span
                  className="dir"
                  data-tip={metric.direction === 'max' ? 'Higher is better' : 'Lower is better'}
                >
                  {metric.direction === 'max' ? (
                    <ArrowUp {...icon} size={14} />
                  ) : (
                    <ArrowDown {...icon} size={14} />
                  )}
                  <span className="sr-only">
                    {metric.direction === 'max' ? ', higher is better' : ', lower is better'}
                  </span>
                </span>
              )}
            </h3>
          )}
          {delta === null ? null : (
            <p className="meas">
              {delta.value === null ? (
                <span className="big pending">Pending</span>
              ) : (
                <span className="big mono" data-tip={delta.tip ?? undefined}>
                  {delta.value}
                </span>
              )}
              {delta.vs === null ? null : <span className="vs">vs R{delta.vs}</span>}
            </p>
          )}
        </section>
      )}
      {judgePending ? (
        // Verdicts still loading: skeleton rows, no text.
        <section className="sec" aria-label="Judge" aria-busy="true">
          <div className="insp-skel" aria-hidden="true">
            <span />
            <span />
          </div>
        </section>
      ) : judge.length === 0 ? null : (
        <section className="sec" aria-label="Judge">
          <h3>Judge</h3>
          {judge.map(attempt => (
            <Attempt key={attempt.attempt} attempt={attempt} />
          ))}
        </section>
      )}
      {changes === null ? null : (
        <section className="sec" aria-label="Changes">
          <h3>
            Changes
            {changes.commit === null ? null : (
              <span className="mono aside" data-tip="Commit">
                {changes.commit.slice(0, 7)}
              </span>
            )}
          </h3>
          {changes.files.length === 0 ? (
            <p className="insp-note">No files changed</p>
          ) : (
            <ul>
              {changes.files.map(file => {
                const [Icon, word] = CHANGE.get(file.change) ?? UNKNOWN_CHANGE;
                const tip =
                  file.change === 'renamed' && file.renamed_from
                    ? `Renamed from ${file.renamed_from}`
                    : word;
                return (
                  <li key={file.path} className="file">
                    <span className="ic" data-tip={tip}>
                      <Icon {...icon} />
                      <span className="sr-only">{tip}</span>
                    </span>
                    <code className="trunc">{file.path}</code>
                  </li>
                );
              })}
            </ul>
          )}
        </section>
      )}
      {designError === null ? null : (
        <p className="insp-note st-failed" role="alert">
          Could not load changes: {designError}{' '}
          <button type="button" className="btn btn-sm" onClick={onRetryDesign}>
            Retry
          </button>
        </p>
      )}
    </div>
  );
}

function Attempt({attempt}: {attempt: JudgeAttempt}) {
  const head = (
    <>
      <span>Attempt {attempt.attempt}</span>
      <span className={`verdict ${VERDICT_CLASS[attempt.verdict]}`}>{attempt.verdict}</span>
    </>
  );
  if (attempt.feedback === '') return <p className="att att-flat">{head}</p>;
  return (
    <details className="att" open={attempt.open}>
      <summary>
        {head}
        <ChevronRight {...icon} className="chev" />
      </summary>
      <div className="att-body">
        <Prose paragraphs={prose(attempt.feedback)} />
      </div>
    </details>
  );
}
