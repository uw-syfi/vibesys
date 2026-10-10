import {expect, test} from 'bun:test';
import {event} from '@vibesys/backend-client/testing';
import {renderToStaticMarkup} from 'react-dom/server';
import {App} from './App.js';
import {createCoreStateStore} from './store.js';

test('surfaces a server-truncated event beside its retained payload', () => {
  const store = createCoreStateStore();
  store.append([
    {
      ...event(1, 'agent_output_chunk', 'retained prefix'),
      truncated: true,
    },
  ]);

  const html = renderToStaticMarkup(<App store={store} />);

  expect(html).toContain('retained prefix');
  expect(html).toContain('class="event-truncated"');
  expect(html).toContain('Event payload truncated at the server');
});
