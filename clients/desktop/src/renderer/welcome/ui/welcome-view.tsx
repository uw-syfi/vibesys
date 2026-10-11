/**
 * The welcome view: the title strip, Recent, one host's panel, "Connect to host", and the two
 * sheets. Presentational: it renders each screen store's state and reports events through `send`.
 * While a run fills the window only the title strip shows, and the sheets wait closed.
 */

import {Button} from '@vibesys/ui';
import type {JSX} from 'react';
import type {CompletionState} from '../state/completion.js';
import type {HostState} from '../state/host.js';
import type {ListsState} from '../state/lists.js';
import type {PickerState} from '../state/picker.js';
import type {StartState} from '../state/start.js';
import type {WelcomeState} from '../state/welcome.js';
import {commands, type WelcomeSenders} from './common.js';
import {HostPanel} from './host-panel.js';
import {HostPicker} from './host-picker.js';
import {RecentPanel} from './recent-panel.js';
import {StartSheet} from './start-sheet.js';
import {TitleStrip} from './title-strip.js';
import {WORDMARK} from './welcome-banner.js';

/** The view's whole input: each screen's state, and where its events go. */
export interface WelcomeViewProps {
  readonly welcome: WelcomeState;
  readonly lists: ListsState;
  readonly host: HostState;
  readonly start: StartState;
  readonly picker: PickerState;
  readonly completion: CompletionState;
  readonly send: WelcomeSenders;
}

export function WelcomeView(props: WelcomeViewProps): JSX.Element {
  const {welcome, send} = props;
  const chrome = welcome.chrome;
  const visible = chrome.mode === 'welcome';
  return (
    <div className={visible ? 'view' : 'view strip'}>
      <TitleStrip chrome={chrome} dispatch={commands(send)} />
      <main>
        <div className="column">
          <div className="hero">
            <pre role="img" aria-label="VibeSys">
              {WORDMARK}
            </pre>
            <p className="muted">
              Attach to a run, or start a new one on this Mac or another host.
            </p>
          </div>
          <p className={welcome.banner?.error === true ? 'status error' : 'status'} role="status">
            {welcome.banner?.text ?? ''}
          </p>
          <RecentPanel lists={props.lists} chrome={chrome} send={send} />
          <HostPanel
            state={props.host}
            lists={props.lists}
            completion={props.completion}
            chrome={chrome}
            send={send}
          />
          <section>
            <h2>Other hosts</h2>
            <div className="row">
              <Button onClick={() => send.picker({type: 'open'})}>Connect to host…</Button>
              <span className="muted">Hosts from ~/.ssh/config, or type user@host.</span>
            </div>
          </section>
        </div>
      </main>
      <StartSheet state={props.start} completion={props.completion} visible={visible} send={send} />
      <HostPicker state={props.picker} visible={visible} picker={send.picker} />
    </div>
  );
}
