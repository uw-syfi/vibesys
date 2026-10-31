import type {JSX} from 'react';
import {Button} from './button.js';
import {Card} from './card.js';
import {Text} from './text.js';

export type StepState = 'done' | 'current' | 'pending' | 'failed';

/** One step: what it is, how it stands, and where it is among the steps (`1/3`). */
export interface StepItem {
  readonly id: string;
  readonly label: string;
  readonly state: StepState;
  readonly position: string;
  /** Beside the step: how many tries it has taken, or why it failed. */
  readonly note?: string;
}

/** Each state's mark and the word that names it, so a state never reads by color alone. */
const MARKS: Readonly<Record<StepState, {readonly mark: string; readonly word: string}>> = {
  done: {mark: '✓', word: 'Done'},
  current: {mark: '●', word: 'Current'},
  pending: {mark: '○', word: 'Waiting'},
  failed: {mark: '✕', word: 'Failed'},
};

/**
 * Ordered steps under a title: the step under way or the one that failed shows its position and a
 * note, and a failed list offers `retry`.
 */
export function StepList(props: {
  readonly title: string;
  readonly steps: readonly StepItem[];
  readonly retry?: () => void;
}): JSX.Element {
  return (
    <Card>
      <div className="vs-step-list">
        <Text variant="body-strong">{props.title}</Text>
        <ol className="vs-steps">
          {props.steps.map(step => {
            const shown = step.state === 'current' || step.state === 'failed';
            return (
              <li key={step.id} className="vs-step" data-state={step.state}>
                <span className="vs-step-mark" role="img" aria-label={MARKS[step.state].word}>
                  {MARKS[step.state].mark}
                </span>
                <Text>{step.label}</Text>
                {shown ? (
                  <Text variant="small" tone="muted">
                    {step.position}
                  </Text>
                ) : null}
                {shown && step.note !== undefined ? (
                  <Text variant="small" tone="muted">
                    {step.note}
                  </Text>
                ) : null}
              </li>
            );
          })}
        </ol>
        {props.retry === undefined ? null : (
          <span>
            <Button variant="secondary" size="sm" text="Retry" press={props.retry} />
          </span>
        )}
      </div>
    </Card>
  );
}
