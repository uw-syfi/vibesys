# Publishing VibeSys

VibeSys publishes one platform-independent Python wheel and no source distribution.

## One-time external setup

Create a protected GitHub environment named `pypi`. Restrict deployment to
selected tags matching `v*`, and require the repository's chosen reviewers. Do
not restrict the environment to `main`: release jobs run on tag refs. The
workflow separately proves that the tagged commit is an ancestor of `main`
before any build or deployment starts. Do not add a PyPI API token.

Configure a pending Trusted Publisher on PyPI with this exact tuple:

- PyPI project: `vibesys`
- GitHub owner: `uw-syfi`
- GitHub repository: `vibesys`
- Workflow filename: `publish.yml`
- Environment: `pypi`

The publish job receives `id-token: write` only after the unprivileged
build and verifier succeed. It downloads the verified `release-dist` artifact.

## Release flow

1. Update `project.version` in `pyproject.toml` using canonical PEP 440. For a
   release candidate, use `0.2.0rc1` and tag `v0.2.0rc1`.
2. Merge the version change to `main`. Never publish a tag whose commit is not
   contained in `main`.
3. Create the `v<version>` tag on that commit and publish a GitHub release for
   the tag. Do not invoke production publishing manually. A published release
   is the only production trigger.
4. Wait for wheel verification, approval of the
   `pypi` environment, and Trusted Publishing to finish. Publishing is
   serialized, and an in-progress publication is never canceled by a newer
   workflow run.

For a final release after an RC, choose a new final version, for example
`0.2.0`, merge it to `main`, then publish the matching `v0.2.0` GitHub release.

## Fresh-machine verification

Use a machine without a VibeSys checkout or existing VibeSys environment:

```bash
uv tool install vibesys==0.2.0
vibesys --help
vibesys validate /path/to/input
```

Exercise the desktop app separately when promoting a release candidate.
Git must be installed separately. Coding-agent CLIs and
provider credentials remain external user prerequisites and must be configured
separately.

## Recovery

PyPI files and released versions are immutable. Never delete and recreate a tag
or attempt to overwrite an uploaded version. If a release is bad, yank that
version in the PyPI project UI with a precise reason, mark the GitHub release as
affected, fix forward, increment the version, and publish a new release. Yanking
keeps exact pins available while preventing ordinary dependency resolution from
selecting the bad version.
