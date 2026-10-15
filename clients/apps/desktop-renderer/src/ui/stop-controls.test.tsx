import {expect, test} from 'bun:test';
import {StopControls} from '@vibesys/feature-run-control/renderer';
import {renderToStaticMarkup} from 'react-dom/server';

test('a pending native confirmation cannot issue another stop', () => {
  const html = renderToStaticMarkup(
    <StopControls
      view={{phase: 'confirming', text: '', canForce: false}}
      stop={() => {
        throw new Error('a disabled stop control must not dispatch');
      }}
    />,
  );

  expect(html).toContain('disabled=""');
  expect(html).toContain('Stop run');
});
