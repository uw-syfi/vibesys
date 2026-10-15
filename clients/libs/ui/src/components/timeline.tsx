import {type JSX, type ReactNode, useId, useState} from 'react';
import {Icon} from './icon.js';
import {Chip, StatusDot, type StatusTone} from './status.js';
import {Text} from './text.js';

export type StageState = 'done' | 'active' | 'waiting' | 'failed';

/**
 * One stage: its state, its label, how long it took or has taken (`elapsed`, formatted by the
 * caller), how long it has been quiet (`quietFor`), and a body shown when the stage is expanded.
 */
export interface Stage {
  readonly id: string;
  readonly state: StageState;
  readonly label: string;
  readonly elapsed?: string;
  readonly quietFor?: string;
  readonly body?: ReactNode;
}

const DOTS: Readonly<Record<StageState, {readonly tone: StatusTone; readonly word: string}>> = {
  done: {tone: 'ok', word: 'Done'},
  active: {tone: 'live', word: 'In progress'},
  waiting: {tone: 'neutral', word: 'Waiting'},
  failed: {tone: 'err', word: 'Failed'},
};

function StageHead(props: {readonly stage: Stage}): JSX.Element {
  const {stage} = props;
  return (
    <>
      <StatusDot tone={DOTS[stage.state].tone} label={DOTS[stage.state].word} />
      <span className="vs-stage-label">
        <Text variant="body-strong">{stage.label}</Text>
      </span>
      {stage.elapsed === undefined ? null : (
        <Text variant="code" tone="muted">
          {stage.elapsed}
        </Text>
      )}
      {stage.quietFor === undefined ? null : (
        <Chip tone="warn" label={`quiet for ${stage.quietFor}`} />
      )}
    </>
  );
}

function StageRow(props: {readonly stage: Stage}): JSX.Element {
  const {stage} = props;
  const bodyId = useId();
  const [expanded, setExpanded] = useState(stage.state === 'active' || stage.state === 'failed');
  if (stage.body === undefined) {
    return (
      <li className="vs-stage">
        <div className="vs-stage-head">
          <StageHead stage={stage} />
        </div>
      </li>
    );
  }
  return (
    <li className="vs-stage">
      <button
        type="button"
        className="vs-stage-head"
        aria-expanded={expanded}
        aria-controls={bodyId}
        onClick={() => setExpanded(!expanded)}
      >
        <StageHead stage={stage} />
        <span className="vs-tree-chevron" data-open={expanded || undefined}>
          <Icon name="chevron-right" />
        </span>
      </button>
      {expanded ? (
        <div className="vs-stage-body" id={bodyId}>
          {stage.body}
        </div>
      ) : null}
    </li>
  );
}

/**
 * Stages in order, each with a state dot, its label, elapsed time and a "quiet for" chip. A stage
 * with a body discloses it; an active or failed one starts expanded.
 */
export function Timeline(props: {readonly stages: readonly Stage[]}): JSX.Element {
  return (
    <ol className="vs-timeline">
      {props.stages.map(stage => (
        <StageRow key={stage.id} stage={stage} />
      ))}
    </ol>
  );
}
