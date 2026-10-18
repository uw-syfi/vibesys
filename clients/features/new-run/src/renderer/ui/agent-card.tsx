/** The Agent card: the harness as tiles with their marks, and the model from the vetted list. */

import type {HarnessOffer} from '@vibesys/ipc';
import {
  BrandMark,
  Button,
  Card,
  ChoiceTiles,
  Field,
  SegmentedControl,
  Select,
  Slider,
  Stack,
  Text,
} from '@vibesys/ui';
import {type JSX, useState} from 'react';
import {harnessLabel, modelLabel, vettedModels} from '../../state/index.js';
import type {CardProps} from './card-props.js';
import {modelAtTier, modelFamilies} from './model-picker.js';
import {harnessMark} from './view.js';

/** The harness and the model every role runs; each role's model and effort are under More options. */
export function AgentCard(props: CardProps): JSX.Element {
  const {context, dispatch} = props;
  const [showAllModels, setShowAllModels] = useState(false);
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
  const families = modelFamilies(offers, agent.provider);
  const selectedFamily =
    families.find(family =>
      family.tiers.some(tier => tier.offers.some(offer => offer.model === agent.model)),
    ) ?? families[0];
  const onLadder = families.some(family =>
    family.tiers.some(tier => tier.offers.some(offer => offer.model === agent.model)),
  );
  const hasLadder = families.some(family => family.tiers.length > 1);
  const fullList = showAllModels || !onLadder || !hasLadder;
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
            label: harnessLabel(offers, provider),
            leading: <BrandMark mark={harnessMark(provider)} />,
          }))}
        />
      </Stack>
      {fullList || selectedFamily === undefined ? (
        <Field label="Model">
          <Select
            value={agent.model}
            options={vettedModels(offers, agent.provider).map(model => ({
              value: model,
              label: modelLabel(offers, agent.provider, model),
            }))}
            change={model => dispatch({type: 'model', model})}
          />
        </Field>
      ) : (
        <Stack gap="space-3">
          {families.length < 2 ? null : (
            <SegmentedControl
              label="Model family"
              value={selectedFamily.name}
              options={families.map(family => ({value: family.name, label: family.name}))}
              change={name => {
                const family = families.find(each => each.name === name);
                const tier = family?.tiers.at(-1);
                if (tier !== undefined) dispatch({type: 'model', model: modelAtTier(tier)});
              }}
            />
          )}
          <Field label="Model">
            <Slider
              options={selectedFamily.tiers.map(tier => tier.name)}
              value={Math.max(
                0,
                selectedFamily.tiers.findIndex(tier =>
                  tier.offers.some(offer => offer.model === agent.model),
                ),
              )}
              change={index => {
                const tier = selectedFamily.tiers[index];
                if (tier !== undefined) dispatch({type: 'model', model: modelAtTier(tier)});
              }}
            />
          </Field>
          <Text tone="muted">{modelLabel(offers, agent.provider, agent.model)}</Text>
        </Stack>
      )}
      {hasLadder && onLadder ? (
        <Button
          variant="quiet"
          size="sm"
          text={fullList ? 'Show model tiers' : 'Show all models'}
          press={() => setShowAllModels(!fullList)}
        />
      ) : null}
    </Card>
  );
}
