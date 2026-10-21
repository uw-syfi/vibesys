import {Card, Stack, Switch, Table, Text} from '@vibesys/ui';
import type {JSX} from 'react';
import type {RecordRow} from '../../state/index.js';

export function RecordsPanel(props: {
  readonly enabled: boolean;
  readonly busy: boolean;
  readonly error: string | null;
  readonly rows: readonly RecordRow[];
  readonly change: (enabled: boolean) => void;
}): JSX.Element {
  return (
    <Card>
      <Stack gap="space-2">
        <Switch
          label="Run transport v2"
          checked={props.enabled}
          change={props.change}
          {...(props.busy ? {disabledReason: 'Connecting'} : {})}
        />
        {props.error === null ? null : (
          <Text tone="err" title={props.error}>
            Unavailable
          </Text>
        )}
        {props.enabled ? (
          <Table
            label="Raw records"
            rows={props.rows}
            rowKey={row => row.seq}
            columns={[
              {key: 'seq', header: 'Seq', mono: true, cell: row => row.seq},
              {key: 'epoch', header: 'Epoch', mono: true, cell: row => row.epoch},
              {key: 'body', header: 'Body', mono: true, cell: row => row.body},
              {key: 'event', header: 'Event', mono: true, cell: row => row.event},
            ]}
          />
        ) : null}
      </Stack>
    </Card>
  );
}
