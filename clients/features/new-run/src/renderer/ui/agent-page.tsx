/**
 * The Agent page: which model line (a card each), which model of it (a slider from faster and
 * cheaper to stronger), which harness runs it on the machine, and how many ideas to try. Older
 * generations and a different agent for a role wait for the host to report them.
 */
import {BrandMark, ChoiceTiles, Field, Select, Slider, Stack, Text} from '@vibesys/ui';
import type {JSX} from 'react';
import {
  agentGroups,
  groupOf,
  harnessLabel,
  modelAtTier,
  modelLabel,
  offersIn,
  pickGroup,
  pickHarness,
  tierOf,
  tiersIn,
} from '../../state/index.js';
import {IdeasField} from './budget-fields.js';
import type {CardProps} from './card-props.js';
import {harnessMark, machineLabel} from './view.js';

export function AgentPage(props: CardProps): JSX.Element {
  const {context, dispatch} = props;
  const {agent, machine} = context.form;
  const offers = context.harnesses?.for === machine ? context.harnesses.list : null;
  if (offers === null) return <Text tone="muted">Asking the machine which agents it runs.</Text>;
  if (offers.length === 0 || agent === null) {
    return <Text tone="warn">This machine offers no vetted agent harness.</Text>;
  }
  const groups = agentGroups(offers);
  const group = groupOf(offers, agent.provider, agent.model) ?? groups[0];
  if (group === undefined)
    return <Text tone="warn">This machine offers no vetted agent harness.</Text>;
  const pick = (provider: string, model: string): void =>
    dispatch({type: 'agent', provider, model});
  const tiers = tiersIn(offers, group, agent.provider);
  const models = offersIn(offers, group, agent.provider);
  const installed =
    context.facts?.for === machine && context.facts.value.kind === 'offered'
      ? context.facts.value.value.harness_clis
      : null;
  return (
    <Stack gap="space-5">
      <Stack gap="space-3">
        <Text variant="section">Model</Text>
        <ChoiceTiles
          label="Model line"
          layout="cards"
          options={groups.map(each => ({value: each.id, label: each.name}))}
          value={group.id}
          change={id => {
            const next = groups.find(each => each.id === id);
            const chosen = next === undefined ? null : pickGroup(offers, next, agent);
            if (chosen !== null) pick(chosen.provider, chosen.model);
          }}
        />
        <Field
          label="Model"
          {...(tiers.length === 0
            ? {}
            : {help: 'Faster and cheaper on the left, stronger and slower on the right.'})}
        >
          {tiers.length > 0 ? (
            <Slider
              options={tiers.map(tier => tier.name)}
              value={Math.max(0, tierOf(tiers, agent.model) ?? 0)}
              change={index => {
                const tier = tiers[index];
                if (tier !== undefined) pick(agent.provider, modelAtTier(tier));
              }}
            />
          ) : (
            <Select
              value={agent.model}
              options={models.map(offer => ({
                value: offer.model,
                label: modelLabel(offers, offer.provider, offer.model),
              }))}
              change={model => pick(agent.provider, model)}
            />
          )}
        </Field>
        <Text tone="muted">{modelLabel(offers, agent.provider, agent.model)}</Text>
      </Stack>
      <Stack gap="space-3">
        <Text variant="section">Harness on {machineLabel(machine)}</Text>
        <ChoiceTiles
          label="Harness"
          layout="cards"
          options={group.providers.map(provider => ({
            value: provider,
            label: harnessLabel(offers, provider),
            leading: <BrandMark mark={harnessMark(provider)} />,
            ...(installed === null
              ? {}
              : {
                  meta: installed.includes(provider)
                    ? `Installed on ${machineLabel(machine)}`
                    : `Not found on ${machineLabel(machine)}`,
                }),
          }))}
          value={agent.provider}
          change={provider => {
            const chosen = pickHarness(offers, group, provider, agent);
            if (chosen !== null) pick(chosen.provider, chosen.model);
          }}
        />
      </Stack>
      <IdeasField {...props} />
    </Stack>
  );
}
