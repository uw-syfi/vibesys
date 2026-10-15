import type {JSX} from 'react';
import {Button} from './button.js';
import {Icon, type IconName} from './icon.js';
import {Text} from './text.js';

export type CheckState = 'ok' | 'warn' | 'fail' | 'pending';

/** One check: its state, what it checks, a detail, and an optional action that fixes it. */
export interface CheckItem {
  readonly id: string;
  readonly state: CheckState;
  readonly label: string;
  readonly detail?: string;
  readonly fix?: {readonly text: string; readonly press: () => void};
}

/** Each state's icon and the word that names it, so a state never reads by color alone. */
const STATES: Readonly<Record<CheckState, {readonly icon: IconName; readonly word: string}>> = {
  ok: {icon: 'check', word: 'Passed'},
  warn: {icon: 'clock', word: 'Needs attention'},
  fail: {icon: 'cross', word: 'Failed'},
  pending: {icon: 'circle', word: 'Not checked yet'},
};

/** A list of checks, each led by an icon of its state. */
export function CheckList(props: {readonly items: readonly CheckItem[]}): JSX.Element {
  return (
    <ul className="vs-check-list">
      {props.items.map(item => (
        <li key={item.id} className="vs-check" data-state={item.state}>
          <span className="vs-check-icon">
            <Icon name={STATES[item.state].icon} label={STATES[item.state].word} />
          </span>
          <span className="vs-check-text">
            <Text variant="body-strong">{item.label}</Text>
            {item.detail === undefined ? null : (
              <Text variant="small" tone="muted">
                {item.detail}
              </Text>
            )}
            {item.fix === undefined ? null : (
              <span className="vs-check-fix">
                <Button size="sm" text={item.fix.text} press={item.fix.press} />
              </span>
            )}
          </span>
        </li>
      ))}
    </ul>
  );
}
