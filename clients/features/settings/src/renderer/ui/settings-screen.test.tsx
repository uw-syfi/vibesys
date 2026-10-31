import {expect, test} from 'bun:test';
import {renderToStaticMarkup} from 'react-dom/server';
import {INITIAL_MACHINE_SETTINGS, type SettingsState} from '../../state/index.js';
import {SettingsScreen} from './settings-screen.js';

function screen(state: SettingsState): string {
  return renderToStaticMarkup(
    <SettingsScreen
      state={state}
      dispatch={() => undefined}
      back={() => undefined}
      machinesContent={null}
      machineSettings={{state: INITIAL_MACHINE_SETTINGS, dispatch: () => undefined, machines: []}}
      machines={{count: 0, attention: 0, listed: true}}
    />,
  );
}

test('settings that could not be read stay on the page as a failure with Retry', () => {
  const html = screen({kind: 'failed', os: 'dark', token: 1, error: 'the file is unreadable'});
  expect(html).toContain('Could not read settings');
  expect(html).toContain('data-tone="failed"');
  expect(html).toContain('Retry');
});
