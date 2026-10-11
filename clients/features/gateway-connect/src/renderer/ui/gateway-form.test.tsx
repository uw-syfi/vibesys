import {describe, expect, test} from 'bun:test';
import {renderToStaticMarkup} from 'react-dom/server';
import {
  createGatewayConnectStore,
  type GatewayConnectFeedback,
  type GatewayConnectState,
  GatewayForm,
} from '../index.js';

const PAGE = 'http://127.0.0.1:5173/';
const CAPABILITY = 'http://127.0.0.1:8765/?token=secret';

function render(state: GatewayConnectState): string {
  return renderToStaticMarkup(<GatewayForm state={state} send={() => undefined} />);
}

function occurrences(text: string, pattern: string): number {
  return text.split(pattern).length - 1;
}

describe('GatewayForm connection announcements', () => {
  for (const {feedback, error} of [
    {
      feedback: {
        kind: 'failure',
        message: 'Gateway connection timed out after 10000 ms',
      } satisfies GatewayConnectFeedback,
      error: true,
    },
    {
      feedback: {
        kind: 'cancelled',
        message: 'Gateway connection cancelled. Retry when ready.',
      } satisfies GatewayConnectFeedback,
      error: false,
    },
  ]) {
    test(`announces ${feedback.kind} feedback with the matching field semantics`, () => {
      const state: GatewayConnectState = {
        kind: 'editing',
        page: PAGE,
        url: '',
        feedback,
        retryTarget: null,
        next: 1,
      };

      const html = render(state);

      expect(html).toContain(
        `class="${error ? 'gateway-connect-error' : 'gateway-connect-status'}"`,
      );
      expect(html).toContain('id="gateway-url-message"');
      expect(html).toContain('role="status"');
      expect(html).toContain('aria-live="polite"');
      expect(html).toContain('aria-atomic="true"');
      expect(html).toContain('aria-describedby="gateway-url-message"');
      if (error) expect(html).toContain('aria-invalid="true"');
      else expect(html).not.toContain('aria-invalid');
      expect(occurrences(html, 'role="status"')).toBe(1);
      expect(occurrences(html, feedback.message)).toBe(1);
    });
  }

  test('keeps one identified live region mounted before an exchange starts', () => {
    const store = createGatewayConnectStore({
      page: PAGE,
      initialError: null,
      onConnected: () => undefined,
      bootstrap: () => new Promise<string>(() => undefined),
    });

    const idle = render(store.getState());
    expect(idle).toMatch(/<p[^>]*id="gateway-url-message"[^>]*><\/p>/);
    expect(occurrences(idle, 'role="status"')).toBe(1);
    expect(idle).not.toContain('aria-describedby');
    expect(idle).not.toContain('aria-invalid');

    store.dispatch({type: 'edit', value: CAPABILITY});
    store.dispatch({type: 'submit'});
    const connecting = render(store.getState());
    expect(connecting).toContain('aria-describedby="gateway-url-message"');
    expect(connecting).toContain('Connecting…');
    expect(connecting).not.toContain('aria-invalid');
  });

  test('keeps the live region empty and unassociated after success', () => {
    const connected = render({kind: 'connected', page: PAGE, url: CAPABILITY, next: 2});
    expect(connected).toContain('>Connected</button>');
    expect(connected).toMatch(/<p[^>]*id="gateway-url-message"[^>]*><\/p>/);
    expect(occurrences(connected, 'role="status"')).toBe(1);
    expect(connected).not.toContain('aria-describedby');
    expect(connected).not.toContain('aria-invalid');
  });
});
