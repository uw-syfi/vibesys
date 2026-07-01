# vs-feature-flags

Typed feature flag utilities for VibeServe and other local packages.

This package is intentionally generic. It does not know about VibeServe's
specific flags, TOML config shape, CLI, or runtime behavior. VibeServe declares
its own flags in `src/vibe_serve/features.py` and uses this package as a small
library.

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

In the consuming package, define a `StrEnum` and a registry:

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

For TOML, the consuming app can expose config like:

```toml
[feature_flags]
new_agent_loop = true
strict_judge_prompts = false
```

The parser rejects unknown flag names and non-boolean values. It does not coerce
strings like `"true"` into booleans.

## VibeServe Integration

VibeServe uses this package from:

- `src/vibe_serve/features.py` for the app-specific manifest.
- `src/vibe_serve/config.py` for `[feature_flags]` parsing.

There are currently no declared VibeServe feature flags. When adding one:

1. Add a member to `FeatureFlag` in `src/vibe_serve/features.py`.
2. Add a matching `FeatureDefinition` to `FEATURES`.
3. Use `FEATURES.is_enabled(FeatureFlag.YOUR_FLAG, config["feature_flags"])`
   at the call site.
4. Add or update tests for both the default and overridden behavior.

## Testing

Library tests live next to the package:

```bash
uv run pytest libs/vs-feature-flags/tests
```

The root project also includes these tests in its pytest configuration, so
`uv run pytest` runs both VibeServe tests and `vs-feature-flags` tests.
