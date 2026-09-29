import {X} from 'lucide-react';
import type {ReactNode} from 'react';
import {PANE, type PaneTab} from '../ui-state.js';
import {Resizer} from './Resizer.js';

const TABS: ReadonlyArray<readonly [PaneTab, string]> = [
  ['ask', 'Ask'],
  ['changes', 'Changes'],
  ['agents', 'Agents'],
  ['experiments', 'Experiments'],
  ['notes', 'Notes'],
];

export interface PaneProps {
  tab: PaneTab;
  width: number;
  onTab: (tab: PaneTab) => void;
  onClose: () => void;
  onResize: (width: number) => void;
  children: ReactNode;
}

export function Pane({tab, width, onTab, onClose, onResize, children}: PaneProps) {
  return (
    <aside className="pane" style={{width}} aria-label="Run details">
      <Resizer
        label="Resize the side pane"
        edge="left"
        value={width}
        min={PANE.min}
        max={PANE.max}
        grow={-1}
        widthAt={clientX => window.innerWidth - clientX}
        onChange={onResize}
      />
      <div className="tabs" role="tablist" aria-label="Run details">
        {TABS.map(([key, label]) => (
          <button
            key={key}
            type="button"
            role="tab"
            id={`tab-${key}`}
            aria-selected={tab === key}
            aria-controls="pane-body"
            className={tab === key ? 'tab on' : 'tab'}
            onClick={() => onTab(key)}
          >
            {label}
          </button>
        ))}
        <button
          type="button"
          className="iconbtn"
          title="Close pane"
          aria-label="Close pane"
          onClick={onClose}
        >
          <X size={16} strokeWidth={1.5} aria-hidden />
        </button>
      </div>
      <div className="pbody" id="pane-body" role="tabpanel" aria-labelledby={`tab-${tab}`}>
        {children}
      </div>
    </aside>
  );
}

/** A tab's first row: its scope ("Round N" or "Run"), then what it shows. */
function PaneHead({scope, children}: {scope: string; children?: ReactNode}) {
  return (
    <div className="phead">
      <span className="scope">{scope}</span>
      {children}
    </div>
  );
}

export function Placeholder({scope, text}: {scope: string; text: string}) {
  return (
    <>
      <PaneHead scope={scope} />
      <p className="empty1">{text}</p>
    </>
  );
}
