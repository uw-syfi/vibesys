import '@vibesys/ui/tokens.css';
import '@vibesys/ui/fonts.css';
import '@vibesys/ui/components.css';
import {OrchestratorApp} from '@vibesys/feature-orchestrator/renderer';
import {applyTheme} from '@vibesys/ui';
import {DEFAULT_THEMES} from '@vibesys/ui/tokens';
import {createRoot} from 'react-dom/client';
import {FIXTURE_NAMES, type FixtureName, fixtureHosts} from './fixtures.js';

/**
 * `?fixture=long-run` (the default), `reconnects` or `short-run` picks the hosts shown;
 * `?reveal=N` starts the live run with only its first N events and adds one every 400 ms.
 */
const params = new URLSearchParams(window.location.search);
const asked = params.get('fixture');
const name: FixtureName = FIXTURE_NAMES.find(known => known === asked) ?? 'long-run';
const reveal = params.get('reveal');
const hosts = fixtureHosts(name, reveal === null ? null : Number(reveal));
// A fixture has no wall clock: the time of its last event, a minute on, is "now".
const now = Math.max(...hosts.map(host => host.endMs)) + 60_000;

applyTheme(DEFAULT_THEMES.light);
if (reveal !== null) {
  const timer = window.setInterval(() => {
    if (!hosts.some(host => host.advance())) window.clearInterval(timer);
  }, 400);
}
const root = document.getElementById('root');
if (root === null) throw new Error('index.html has no #root');
createRoot(root).render(
  <OrchestratorApp connections={hosts.map(({host, ports}) => ({host, ports}))} now={() => now} />,
);
