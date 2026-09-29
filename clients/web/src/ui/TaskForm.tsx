/** The task form: create a scalar-maximize task, or edit a saved one the server can round-trip. */
import type {TaskDomain} from '../home-api.js';
import {badTaskName, type Draft, draftBlockers} from '../setup.js';
import {Hint, Row, Select} from './Setup.js';

const DOMAINS: Readonly<Record<TaskDomain, string>> = {
  'llm-serving': 'LLM serving',
  generic: 'Generic',
  microservices: 'Microservices',
  database: 'Database',
};

export interface TaskFormProps {
  draft: Draft;
  creating: boolean;
  saving: boolean;
  error: string | null;
  onDraft: (patch: Partial<Draft>) => void;
  onSave: () => void;
  onDiscard: () => void;
}

interface TextRowProps {
  id: string;
  label: string;
  value: string;
  placeholder: string;
  hint: string;
  onValue: (value: string) => void;
}

function TextRow({id, label, value, placeholder, hint, onValue}: TextRowProps) {
  return (
    <Row label={label} htmlFor={id}>
      <input
        id={id}
        className="fld mono"
        value={value}
        placeholder={placeholder}
        title={hint}
        spellCheck={false}
        autoComplete="off"
        onChange={event => onValue(event.target.value)}
      />
    </Row>
  );
}

function NameRow({name, onName}: {name: string; onName: (name: string) => void}) {
  const bad = badTaskName(name);
  return (
    <Row label="Name" htmlFor="f-name">
      <input
        id="f-name"
        className={bad ? 'fld mono bad' : 'fld mono'}
        value={name}
        placeholder="decode-throughput"
        title="The task's folder under .vibesys/tasks/"
        spellCheck={false}
        autoComplete="off"
        onChange={event => onName(event.target.value)}
      />
      {bad ? (
        <Hint tone="bad">Up to 128 of a-z, 0-9, ., _ or -, starting with a letter or digit.</Hint>
      ) : null}
    </Row>
  );
}

function MetricRow({draft, onDraft}: Pick<TaskFormProps, 'draft' | 'onDraft'>) {
  return (
    <Row label="Metric" htmlFor="f-metric">
      <div className="row2">
        <input
          id="f-metric"
          className="fld mono"
          value={draft.result_metric}
          placeholder="median_tok_per_sec"
          title="The field of the benchmark's JSON output to maximize"
          spellCheck={false}
          autoComplete="off"
          onChange={event => onDraft({result_metric: event.target.value})}
        />
        <input
          className="fld mono flag"
          aria-label="JSON flag"
          value={draft.result_json_argument}
          placeholder="--json"
          title="Passed to the benchmark so it prints JSON; the metric is read from it"
          spellCheck={false}
          autoComplete="off"
          onChange={event => onDraft({result_json_argument: event.target.value})}
        />
      </div>
      <Hint tone="plain">Higher is better.</Hint>
    </Row>
  );
}

export function TaskFormRows({
  draft,
  creating,
  saving,
  error,
  onDraft,
  onSave,
  onDiscard,
}: TaskFormProps) {
  const complete = draftBlockers(draft, creating)[0]?.field === 'save';
  return (
    <>
      {creating ? <NameRow name={draft.name} onName={name => onDraft({name})} /> : null}
      <Row label="Objective" htmlFor="f-objective">
        <textarea
          id="f-objective"
          className="fld area"
          rows={3}
          value={draft.objective}
          placeholder="What should improve, and what must not change?"
          onChange={event => onDraft({objective: event.target.value})}
        />
      </Row>
      <Row label="Domain" htmlFor="f-domain">
        <Select
          id="f-domain"
          className="fld"
          value={draft.domain}
          onChange={event => {
            const keys = Object.keys(DOMAINS) as TaskDomain[];
            const domain = keys.find(key => key === event.target.value);
            if (domain !== undefined) onDraft({domain});
          }}
        >
          {(Object.entries(DOMAINS) as [TaskDomain, string][]).map(([value, label]) => (
            <option key={value} value={value}>
              {label}
            </option>
          ))}
        </Select>
      </Row>
      <TextRow
        id="f-accuracy"
        label="Accuracy"
        value={draft.accuracy_command}
        placeholder="cargo test --release"
        hint="Must pass before a round's change is measured; runs from the repository root"
        onValue={accuracy_command => onDraft({accuracy_command})}
      />
      <TextRow
        id="f-benchmark"
        label="Benchmark"
        value={draft.benchmark_command}
        placeholder="cargo bench --bench decode"
        hint="Runs from the repository root"
        onValue={benchmark_command => onDraft({benchmark_command})}
      />
      <MetricRow draft={draft} onDraft={onDraft} />
      <div className="fr">
        <span />
        <div className="acts2">
          <button
            id="f-save"
            type="button"
            className="btn"
            disabled={saving || !complete}
            onClick={onSave}
          >
            {saving ? 'Saving…' : 'Save task'}
          </button>
          <button type="button" className="btn ghost" onClick={onDiscard}>
            Discard
          </button>
          {error === null ? null : (
            <span className="hint bad" role="alert">
              {error}
            </span>
          )}
        </div>
      </div>
    </>
  );
}
