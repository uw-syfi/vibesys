# vs-feature-flags

Typed feature flag utilities.

## Concepts

- `FeatureDefinition` describes one flag with a required `description` and a
  `default` boolean.
- `FeatureRegistry` owns a typed flag manifest and evaluates defaults plus
  typed overrides.
- `parse_feature_flag_overrides` converts user config such as TOML-loaded
  dictionaries into typed enum keys.

Use enum members at call sites instead of raw strings. That keeps references
easy to find and makes stale references fail when a flag is removed.

## Declare Flags

Define a `StrEnum` and a registry:

```python
from enum import StrEnum

from vs_feature_flags import FeatureDefinition, FeatureRegistry


class FeatureFlag(StrEnum):
    NEW_AGENT_LOOP = "new_agent_loop"
    STRICT_JUDGE_PROMPTS = "strict_judge_prompts"


FEATURES = FeatureRegistry(
    FeatureFlag,
    {
        FeatureFlag.NEW_AGENT_LOOP: FeatureDefinition(
            description="Use the new agent loop implementation.",
            default=False,
        ),
        FeatureFlag.STRICT_JUDGE_PROMPTS: FeatureDefinition(
            description="Use stricter judge prompts.",
            default=True,
        ),
    },
)
```

`FeatureRegistry` validates that every enum member has a definition. This keeps
the manifest documented by construction.

## Evaluate Flags

Use typed enum members at call sites:

```python
if FEATURES.is_enabled(FeatureFlag.NEW_AGENT_LOOP):
    run_new_loop()
else:
    run_current_loop()
```

Pass typed overrides when user config or tests need to change behavior:

```python
overrides = {FeatureFlag.NEW_AGENT_LOOP: True}

if FEATURES.is_enabled(FeatureFlag.NEW_AGENT_LOOP, overrides):
    run_new_loop()
```

## Parse Config

When loading config, parse the raw user-provided table into typed overrides:

```python
from vs_feature_flags import parse_feature_flag_overrides

raw_feature_flags = raw_config.get("feature_flags")
feature_flags = parse_feature_flag_overrides(raw_feature_flags, FeatureFlag)
```

For TOML, expose config like:

```toml
[feature_flags]
new_agent_loop = true
strict_judge_prompts = false
```

The parser rejects unknown flag names and non-boolean values. It does not coerce
strings like `"true"` into booleans.

## Testing

Package tests live next to the package:

```bash
uv run pytest libs/vs-feature-flags/tests
```

The root project includes these tests in its pytest configuration, so
`uv run pytest` runs them too.
