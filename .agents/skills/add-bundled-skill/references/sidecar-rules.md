# Sidecar rules

VibeSys routing metadata for bundled skills lives in optional sidecar files
named `.vibesys.toml`, never in `SKILL.md` frontmatter. Agent harnesses and
skill loaders that do not know VibeSys ignore the sidecar, and vendored skills
stay byte-identical to upstream. The sidecar parser lives with skill
selection in the orchestration layer.

## Layout

A vendored pack keeps its sidecar in a VibeSys-owned wrapper directory, next
to the update script and beside a subtree holding the vendored skills. One rule
with `path` set to that subtree scopes every vendored skill (for example to one
backend), and the update script can delete and recreate the subtree without
touching the sidecar. An existing vendored pack is the model. First-party
top-level skills share one sidecar at the root of the bundled tree, with one
rule per skill directory.

## Rule semantics

A sidecar holds one or more `[[rule]]` tables, each with these keys:

- `path` (required): a directory relative to the sidecar's directory. It must
  exist, must not be absolute or contain `..`, and must still lie inside the
  sidecar's directory after symlinks are resolved.
- `backends` (optional): a list of `ComputeBackend` values. Absent means the
  rule does not constrain the backend.
- `domains` (optional): a list of `DomainName` values, matched against the
  input bundle's `[agent].domain`. Absent means the rule does not constrain
  the domain.

Read the allowed values from the enums,
and do not copy them into docs. Duplicate values in a list are dropped, keeping
the first.

Selection for one skill directory:

- A rule matches a skill when the skill directory lies inside the rule's
  resolved `path`. Rules come from every sidecar under the configured skill
  root, except sidecars inside a directory whose name starts with `.`.
- A skill that no rule matches loads for every backend and domain.
- When several rules match, the one whose resolved `path` has the most path
  components wins.
- Rules tied at that depth must agree on both `backends` and `domains`;
  otherwise validation fails and names every conflicting rule.
- The winning rule loads the skill only if the run's backend is in its
  `backends` (when given) and the run's domain is in its `domains` (when
  given).
- `--no-skills` loads nothing regardless of rules; `--skills-dir` and
  `--extra-skills` change only which roots are searched.

## Validation errors

Every error is a `SkillMetadataError` whose message starts with the offending
file or directory. A run fails before its first agent turn when:

- `SKILL.md` is missing its opening or closing `---` delimiter, its YAML is
  malformed, or it is not a mapping;
- the catalog finds a `name` that differs from the directory name, or an
  empty `description`;
- `.vibesys.toml` is not valid TOML;
- it has a top-level key other than `rule`;
- it has no `[[rule]]` table, or a `rule` entry that is not a table;
- a rule has a key other than `path`, `backends` and `domains`;
- `path` is missing, empty, absolute, contains `..`, escapes the directory,
  or does not exist;
- `backends` or `domains` is not a list, or holds a value outside its enum
  (the message lists the allowed values);
- two equally specific matching rules disagree.

The platform layout is checked only by whole-tree validation in the run
layer, not at run start. It fails when a platform directory inside a skill's
references is not named for a `ComputeBackend` value, or lacks a file from the
platform skeleton. Whole-tree validation also runs the sidecar and frontmatter checks; the test
suite runs it on the bundled tree, so drift in the bundled tree fails in
CI. A skill directory passed with `--skills-dir` gets only the run-start
checks above.
