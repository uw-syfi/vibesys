import {describe, expect, it} from '@effect/vitest';
import fc from 'fast-check';
import type {ProvisionExample} from '../../../core/index.js';
import {initialNavigation, readOf} from '../../../state/index.js';
import {appCommandsOf, hostSnapshotOf} from '../../../testing/dom/app-builders.js';
import {fieldOf, typeInto} from '../../../testing/dom/fields.js';
import {installDom, mount} from '../../../testing/dom/harness.js';
import {
  FIXTURE_REPOSITORIES_DIRECTORY,
  fixtureCatalogueResponse,
  fixturePlacesResponse,
} from '../../../testing/orchestrator/new-run-fixtures.js';
import {OrchestratorApp} from '../app.js';

installDom();

/** New run opened on an example, with the machine's kept folder; the examples the Start sent. */
function open(repositoriesDirectory: string) {
  const sent: ProvisionExample[] = [];
  const screen = mount(
    <OrchestratorApp
      hosts={[
        hostSnapshotOf({
          catalogue: readOf(fixtureCatalogueResponse()),
          evalPlaces: readOf(fixturePlacesResponse()),
          repositoriesDirectory,
        }),
      ]}
      nowMs={0}
      commands={appCommandsOf({startFromExample: (_host, example) => sent.push(example)})}
      retryHost={() => undefined}
      retryCost={() => undefined}
      route={initialNavigation.runs}
      fleet={initialNavigation.fleet}
      go={() => undefined}
    />,
  );
  screen.press(screen.control('New run'));
  const row = [...screen.container.querySelectorAll<HTMLElement>('[role="option"]')].find(each =>
    each.textContent?.includes('swmr-map'),
  );
  if (row === undefined) throw new Error('no swmr-map row');
  screen.press(row);
  return {screen, sent};
}

const folder = fc.stringMatching(/^(~|~\/[a-z0-9/_-]{0,20}|\/[a-z0-9/_-]{0,20})$/);

describe('where Copy to starts', () => {
  it('shows the host default when the machine keeps no folder, and sends none', () => {
    const {screen, sent} = open('');
    expect(fieldOf<HTMLInputElement>(screen, 'Copy to').placeholder).toBe(
      FIXTURE_REPOSITORIES_DIRECTORY,
    );
    screen.press(screen.control('Start run'));
    expect(sent[0]?.parent).toBe('');
    screen.unmount();
  });

  it('starts at the folder the machine keeps, whatever it is, unless the person types one', () => {
    fc.assert(
      fc.property(folder, kept => {
        fc.pre(kept !== '');
        const {screen, sent} = open(kept);
        expect(fieldOf<HTMLInputElement>(screen, 'Copy to').placeholder).toBe(kept);
        screen.press(screen.control('Start run'));
        expect(sent[0]?.parent).toBe(kept);
        screen.unmount();
      }),
      {numRuns: 25},
    );
  });

  it('sends what the person typed over the kept folder', () => {
    const {screen, sent} = open('~/kept');
    typeInto(fieldOf<HTMLInputElement>(screen, 'Copy to'), '~/typed');
    screen.press(screen.control('Start run'));
    expect(sent[0]?.parent).toBe('~/typed');
    screen.unmount();
  });
});
