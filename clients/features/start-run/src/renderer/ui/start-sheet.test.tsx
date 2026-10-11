import {expect, test} from 'bun:test';
import {renderToStaticMarkup} from 'react-dom/server';
import {START_CLOSED, startStep} from '../../state/start.js';
import {type ProjectField, StartSheet} from './start-sheet.js';

test('the project field the app supplies gets the sheet value, its offers, and whether it edits', () => {
  const asked: Parameters<ProjectField>[0][] = [];
  const field: ProjectField = props => {
    asked.push(props);
    return <input readOnly value={props.value} />;
  };
  const open = startStep(START_CLOSED, {
    type: 'open',
    host: {key: 'ssh:gpu', label: 'gpu'},
    projects: ['/p', '/q'],
  }).state;
  const typed = startStep(open, {type: 'project', value: '/p'}).state;
  const html = renderToStaticMarkup(
    <StartSheet state={typed} visible={true} projectField={field} start={() => undefined} />,
  );
  expect(html).toContain('Start a run on gpu');
  expect(asked).toHaveLength(1);
  expect(asked[0]).toMatchObject({
    host: 'ssh:gpu',
    value: '/p',
    offers: ['/p', '/q'],
    disabled: false,
  });
});
