/**
 * The Agent page: which maker's models (a card each), which family and generation of them when the
 * host names them (the latest generation shown, older ones behind "Older (n)"), which model of it
 * (a slider from faster and cheaper to stronger), which harness runs it on the machine, a different
 * agent for a role (closed until opened), and how many ideas to try.
 */
import type {HarnessOffer} from '@vibesys/ipc';
import {
  BrandMark,
  Button,
  ChoiceTiles,
  Field,
  Row,
  SegmentedControl,
  Select,
  Slider,
  Stack,
  Text,
} from '@vibesys/ui';
import {type JSX, useState} from 'react';
import {
  type AgentChoice,
  type AgentGroup,
  agentGroups,
  familiesIn,
  generationsIn,
  groupOf,
  harnessLabel,
  installedHarnesses,
  modelAtTier,
  modelLabel,
  offersIn,
  pickGroup,
  pickHarness,
  type Scope,
  scopeOf,
  tierOf,
  tiersIn,
} from '../../state/index.js';
import {IdeasField} from './budget-fields.js';
import type {CardProps} from './card-props.js';
import {RoleAgents} from './role-agents.js';
import {agentMakerMark, harnessMark, machineLabel} from './view.js';

type Pick = (provider: string, model: string) => void;

/** The generations of a model line, the newest first and the older ones behind "Older (n)". */
function Generations(props: {
  readonly generations: readonly {readonly id: string; readonly name: string}[];
  readonly current: string | null;
  readonly choose: (id: string) => void;
}): JSX.Element {
  const [older, setOlder] = useState(false);
  const {generations} = props;
  const shown = older || generations.slice(1).some(each => each.id === props.current);
  const visible = shown ? generations : generations.slice(0, 1);
  return (
    <Stack gap="space-2">
      <Text variant="body-strong">Generation</Text>
      <Row wrap>
        <ChoiceTiles
          label="Generation"
          options={visible.map((each, index) => ({
            value: each.id,
            label: index === 0 ? `${each.name} (latest)` : each.name,
          }))}
          value={props.current}
          change={props.choose}
        />
        {shown ? null : (
          <Button
            variant="quiet"
            size="sm"
            text={`Older (${generations.length - 1})`}
            press={() => setOlder(true)}
          />
        )}
      </Row>
    </Stack>
  );
}

/** The model within the card: its family and generation, then the slider over its tiers. */
function ModelChoice(props: {
  readonly offers: readonly HarnessOffer[];
  readonly group: AgentGroup;
  readonly agent: AgentChoice;
  readonly pick: Pick;
}): JSX.Element {
  const {offers, group, agent, pick} = props;
  const current = offers.find(
    offer => offer.provider === agent.provider && offer.model === agent.model,
  );
  const scope: Scope | null = current === undefined ? null : scopeOf(current);
  const families = familiesIn(offers, group, agent.provider);
  const generations = generationsIn(offers, group, agent.provider, scope?.family ?? null);
  const tiers = tiersIn(offers, group, agent.provider, scope);
  const within = (next: Scope): void => {
    const chosen = pickHarness(offers, group, agent.provider, agent, next);
    if (chosen !== null) pick(chosen.provider, chosen.model);
  };
  return (
    <>
      {families.length === 0 ? null : (
        <SegmentedControl
          label="Model family"
          value={scope?.family ?? families[0] ?? ''}
          options={families.map(family => ({value: family, label: family}))}
          change={family =>
            within({
              family,
              generation: generationsIn(offers, group, agent.provider, family)[0]?.id ?? null,
            })
          }
        />
      )}
      {generations.length === 0 ? null : (
        <Generations
          generations={generations}
          current={scope?.generation ?? null}
          choose={generation => within({family: scope?.family ?? null, generation})}
        />
      )}
      <Field label="Model">
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
            options={offersIn(offers, group, agent.provider, scope).map(offer => ({
              value: offer.model,
              label: modelLabel(offers, offer.provider, offer.model),
            }))}
            change={model => pick(agent.provider, model)}
          />
        )}
      </Field>
      <Text tone="muted">{modelLabel(offers, agent.provider, agent.model)}</Text>
    </>
  );
}

export function AgentPage(props: CardProps): JSX.Element {
  const {context, dispatch} = props;
  const {agent, machine} = context.form;
  const offers = context.harnesses?.for === machine ? context.harnesses.list : null;
  if (offers === null) return <Text tone="muted">Listing</Text>;
  const groups = agentGroups(offers);
  const group =
    agent === null ? undefined : (groupOf(offers, agent.provider, agent.model) ?? groups[0]);
  if (agent === null || group === undefined) {
    return <Text tone="warn">No agents</Text>;
  }
  const pick: Pick = (provider, model) => dispatch({type: 'agent', provider, model});
  const installed = installedHarnesses(context.facts, machine);
  return (
    <Stack gap="space-5">
      <Stack gap="space-3">
        <Text variant="section">Model</Text>
        <ChoiceTiles
          label="Model line"
          layout="cards"
          options={groups.map(each => {
            const mark = agentMakerMark(each.maker);
            return {
              value: each.id,
              label: each.name,
              ...(mark === null ? {} : {leading: <BrandMark mark={mark} />}),
            };
          })}
          value={group.id}
          change={id => {
            const next = groups.find(each => each.id === id);
            const chosen = next === undefined ? null : pickGroup(offers, next, agent);
            if (chosen !== null) pick(chosen.provider, chosen.model);
          }}
        />
        <ModelChoice offers={offers} group={group} agent={agent} pick={pick} />
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
      <RoleAgents {...props} offers={offers} agent={agent} />
      <IdeasField {...props} />
    </Stack>
  );
}
