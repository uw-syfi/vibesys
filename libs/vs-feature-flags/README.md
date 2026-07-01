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
    NEW_DASHBOARD = "new_dashboard"
    STRICT_VALIDATION = "strict_validation"


FEATURES = FeatureRegistry(
    FeatureFlag,
    {
        FeatureFlag.NEW_DASHBOARD: FeatureDefinition(
            description="Use the redesigned dashboard.",
            default=False,
        ),
        FeatureFlag.STRICT_VALIDATION: FeatureDefinition(
            description="Reject invalid input earlier.",
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
if FEATURES.is_enabled(FeatureFlag.NEW_DASHBOARD):
    show_new_dashboard()
else:
    show_current_dashboard()
```

Pass typed overrides when user config or tests need to change behavior:

```python
overrides = {FeatureFlag.NEW_DASHBOARD: True}

if FEATURES.is_enabled(FeatureFlag.NEW_DASHBOARD, overrides):
    show_new_dashboard()
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
new_dashboard = true
strict_validation = false
```

The parser rejects unknown flag names and non-boolean values. It does not coerce
strings like `"true"` into booleans.

## Testing

Package tests live next to the package. From this directory:

```bash
pytest tests
```
