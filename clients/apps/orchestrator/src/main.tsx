import '@vibesys/ui/tokens.css';
import '@vibesys/ui/fonts.css';
import '@vibesys/ui/components.css';
import {OrchestratorApp} from '@vibesys/feature-orchestrator/renderer';
import type {GatewayPortBridge} from '@vibesys/ipc';
import {BackoffSchedule} from '@vibesys/transport';
import {applyTheme} from '@vibesys/ui';
import {DEFAULT_THEMES} from '@vibesys/ui/tokens';
import {createRoot} from 'react-dom/client';
import {FIXTURE_NAMES, type FixtureName, fixtureHosts} from './fixtures.js';
import {addressedHosts, liveHost} from './live.js';

declare global {
  interface Window {
    vibesysGateway?: GatewayPortBridge;
  }
}

/**
 * `?host=local` or `?host=ssh:<alias>` (repeatable) shows those hosts, read live through the
 * desktop app. Otherwise `?fixture=long-run` (the default), `reconnects`, `short-run`,
 * `disconnected` or `rejected-message` picks the fixture hosts shown; `?reveal=N` starts the live
 * fixture run with only its first N events and adds one every 400 ms.
 */
const params = new URLSearchParams(window.location.search);
const addressed = addressedHosts(params);
const asked = params.get('fixture');
const name: FixtureName = FIXTURE_NAMES.find(known => known === asked) ?? 'long-run';
const reveal = params.get('reveal');
const fixtures =
  addressed.length > 0 ? [] : fixtureHosts(name, reveal === null ? null : Number(reveal));
const connections =
  addressed.length > 0
    ? addressed.map(host => liveHost(host, window.vibesysGateway))
    : fixtures.map(({host, ports}) => ({host, ports}));
// A fixture has no wall clock: the time of its last event, a minute on, is "now".
const fixtureNow = Math.max(0, ...fixtures.map(host => host.endMs)) + 60_000;

applyTheme(DEFAULT_THEMES.light);
if (reveal !== null) {
  const timer = window.setInterval(() => {
    if (!fixtures.some(host => host.advance())) window.clearInterval(timer);
  }, 400);
}
const root = document.getElementById('root');
if (root === null) throw new Error('index.html has no #root');
createRoot(root).render(
  <OrchestratorApp
    connections={connections}
    now={addressed.length > 0 ? () => Date.now() : () => fixtureNow}
    newId={() => crypto.randomUUID()}
    backoff={() => new BackoffSchedule()}
  />,
);
