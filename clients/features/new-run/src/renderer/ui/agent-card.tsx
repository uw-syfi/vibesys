/** The Agent card: the harness as tiles with their marks, and the model from the vetted list. */

import type {HarnessOffer} from '@vibesys/ipc';
import {BrandMark, Card, ChoiceTiles, Field, Select, Stack, Text} from '@vibesys/ui';
import type {JSX} from 'react';
import {vettedModels} from '../../state/index.js';
import type {CardProps} from './card-props.js';
import {harnessMark} from './view.js';

/** The harness and the model every role runs; each role's model and effort are under More options. */
export function AgentCard(props: CardProps): JSX.Element {
  const {context, dispatch} = props;
  const offers: readonly HarnessOffer[] | null =
    context.harnesses?.for === context.form.machine ? context.harnesses.list : null;
  const agent = context.form.agent;
  if (offers === null) {
    return (
      <Card section="Agent">
        <Text tone="muted">Asking the machine which agents it runs.</Text>
      </Card>
    );
  }
  if (agent === null) {
    return (
      <Card section="Agent">
        <Text tone="warn">This machine offers no vetted agent harness.</Text>
      </Card>
    );
  }
  const providers = [...new Set(offers.map(offer => offer.provider))];
  return (
    <Card section="Agent">
      <Stack gap="space-3">
        <Text variant="body-strong">Harness</Text>
        <ChoiceTiles
          label="Harness"
          value={agent.provider}
          change={provider => dispatch({type: 'provider', provider})}
          options={providers.map(provider => ({
            value: provider,
            label: provider,
            leading: <BrandMark mark={harnessMark(provider)} />,
          }))}
        />
      </Stack>
      <Field label="Model" help="Only models VibeSys has tested with this harness are listed.">
        <Select
          value={agent.model}
          options={vettedModels(offers, agent.provider).map(model => ({
            value: model,
            label: model,
          }))}
          change={model => dispatch({type: 'model', model})}
        />
      </Field>
    </Card>
  );
}
