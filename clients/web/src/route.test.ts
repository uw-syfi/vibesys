import {strict as assert} from 'node:assert';
import {test} from 'node:test';
import {homeHref, homeView, pageParams, runHref, runLinks} from './route.js';
import {webSocketUrlFromLocation} from './session.js';

test('home views round-trip through the hash; anything else is the empty view', () => {
  const views = [
    {kind: 'empty'},
    {kind: 'new'},
    {kind: 'open', projectId: 'p1', runId: 'llm-serve-20260925-140100'},
    {kind: 'resume', projectId: 'p/1', runId: 'a b'},
  ] as const;
  for (const view of views) {
    const href = homeHref('h0me', view);
    assert.ok(href.startsWith('/?token=h0me'), href);
    assert.deepEqual(homeView(new URL(href, 'http://127.0.0.1:8764').hash), view);
  }
  assert.deepEqual(homeView(''), {kind: 'empty'});
  assert.deepEqual(homeView('#open=no-slash'), {kind: 'empty'});
  assert.deepEqual(homeView('#resume=%E0%A4%A/x'), {kind: 'empty'});
  assert.deepEqual(homeView('#settings'), {kind: 'empty'});
});

test("a run page keeps the home token and connects to the gateway with the gateway's own token", () => {
  const href = runHref('h0me', 'p1', 'ws://127.0.0.1:53211/ws?token=gw');
  const location = {href: new URL(href, 'http://127.0.0.1:8764').toString()};
  assert.deepEqual(pageParams(location.href), {
    token: 'h0me',
    gateway: 'ws://127.0.0.1:53211/ws?token=gw',
    project: 'p1',
  });
  assert.equal(webSocketUrlFromLocation(location), 'ws://127.0.0.1:53211/ws?token=gw');
});

test('run links lead back to the home page', () => {
  const links = runLinks('h0me', 'p1');
  assert.equal(links.newRun, '/?token=h0me#new');
  assert.equal(links.resume('r1'), '/?token=h0me#resume=p1/r1');
});
