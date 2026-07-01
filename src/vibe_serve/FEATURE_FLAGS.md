# VibeServe Feature Flags

VibeServe declares feature flags in `src/vibe_serve/features.py`.

The generic utilities come from `vs_feature_flags`, but the VibeServe-specific
manifest, defaults, config parsing, and call-site conventions live here.

## Manifest

Add flags to the `FeatureFlag` enum and define each one in `FEATURES`:

```python
from enum import StrEnum

from vs_feature_flags import FeatureDefinition, FeatureRegistry


class FeatureFlag(StrEnum):
    EXAMPLE_FEATURE = "example_feature"


FEATURES = FeatureRegistry(
    FeatureFlag,
    {
        FeatureFlag.EXAMPLE_FEATURE: FeatureDefinition(
            description="Enable the example feature.",
            default=False,
        ),
    },
)
```

`example_feature` is a non-product sample flag used to exercise feature flag
plumbing and tests. Remove it when the first real VibeServe feature flag exists.

## Config

`src/vibe_serve/config.py` parses `[feature_flags]` from `agent.toml` with
`parse_feature_flag_overrides` and stores typed overrides in
`config.feature_flags`.

Example:

```toml
[feature_flags]
example_feature = true
```

Unknown flag names and non-boolean values fail during config loading.

## Usage

Use enum members at call sites:

```python
from vibe_serve.features import FeatureFlag, is_feature_enabled


if is_feature_enabled(FeatureFlag.EXAMPLE_FEATURE, config):
    ...
```

For direct registry access:

```python
from vibe_serve.features import FEATURES, FeatureFlag


enabled = FEATURES.is_enabled(
    FeatureFlag.EXAMPLE_FEATURE,
    config.feature_flags,
)
```

## Adding A Flag

1. Add the enum member to `FeatureFlag`.
2. Add a matching `FeatureDefinition` to `FEATURES`.
3. Add `[feature_flags]` config examples only if users are expected to set it.
4. Use `FeatureFlag.YOUR_FLAG`, not raw strings, at call sites.
5. Test the default behavior and the overridden behavior.

## Removing A Flag

1. Delete the enum member and its `FeatureDefinition`.
2. Run tests to catch stale `FeatureFlag.YOUR_FLAG` references.
3. Remove any corresponding `agent.toml` examples or docs.
