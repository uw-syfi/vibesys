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

## vibesys-cli releases

The Go binary `vibesys-cli` is released separately from the wheel, by
`.github/workflows/cli-release.yml`, and its releases never reach PyPI: `publish.yml` skips a
release whose tag starts with `cli-`. Users install it with `cli/install.sh` and update it with
`vibesys-cli upgrade` ([cli/README.md](https://github.com/uw-syfi/vibesys/blob/main/cli/README.md)).

1. Merge the change to `main`. The workflow refuses a tag whose commit is not contained in it.
2. Push a tag `cli-v<version>`, for example `cli-v0.2.0`. The version is whatever the tag says;
   there is no version file to update. A suffix (`cli-v0.2.0-rc.1`) makes a pre-release.
3. The workflow checks the module, builds the four binaries, attests them, and publishes the release
   for that tag. For a stable version that is the newest, it then points the rolling tag
   `cli-latest` at the same commit and replaces that release's assets.

Two releases of this repository must not share GitHub's "latest" marker, which goes to the most
recently created release that is not a pre-release. The Python releases keep it. Every `vibesys-cli`
release is created with `--latest=false`, and `install.sh` and `vibesys-cli upgrade` read the
`cli-latest` tag instead, so a Python release made after a CLI release cannot redirect them.

`cli-latest` is moved, which a repository setting called immutable releases forbids. It is off
today. If it is turned on, the alias step must delete and recreate the `cli-latest` release instead.

Recovery follows the wheel's rule: do not rewrite a published version. If a binary is bad, publish
a higher version; the alias then moves to it and `vibesys-cli upgrade` follows. Between two assets
of the alias being replaced, a reader can see new binaries with old checksums for a moment. Both
`install.sh` and `upgrade` refuse that mix and can simply be run again.

## vibesys-evaluator releases

`vibesys-evaluator` is the package task writers build benchmark evaluators on. One version
covers its published parts, all built from one commit of `main`:

- the wheel `vibesys-evaluator` on PyPI (one universal wheel, no sdist, like `vibesys`);
- one image per domain, which task images build `FROM`:
  `ghcr.io/uw-syfi/vibesys-evaluator-microservices:<version>` (with loadgen, kubectl and
  buildctl) and `ghcr.io/uw-syfi/vibesys-evaluator-llm-serving:<version>` (with the AgentX
  harness pinned by `sdk/vibesys-evaluator/image/llm-serving/uv.lock`), each a target of
  `sdk/vibesys-evaluator/image/Dockerfile`;
- the Go module tag `sdk/vibesys-evaluator/go/v<semver>` for its load generator.

`.github/workflows/evaluator-release.yml` releases them. `publish.yml` skips a release whose tag
starts with `evaluator-`.

### One-time external setup

Create a GitHub environment named `pypi-evaluator`. Restrict deployment to selected tags matching
`evaluator-v*`, and add no required reviewers: nothing waits for an approval, and the publish is
gated by the check that the tagged commit is on `main` and by the verification job. Do not add a
PyPI API token.

Configure a pending Trusted Publisher on PyPI with this exact tuple:

- PyPI project: `vibesys-evaluator`
- GitHub owner: `uw-syfi`
- GitHub repository: `vibesys`
- Workflow filename: `evaluator-release.yml`
- Environment: `pypi-evaluator`

The image job pushes with the workflow's own token, one leg per domain image. GitHub creates
the container packages `vibesys-evaluator-microservices` and `vibesys-evaluator-llm-serving` on
the first push and links it to this repository through the image's
`org.opencontainers.image.source` label; a package cannot be configured before it exists. After
the first release (a release candidate is enough), open each package's settings, set its visibility
to public, and under "Manage Actions access" confirm that the `vibesys` repository has the Write
role. Task images pull them without credentials only once they are public.

### Release flow

1. Update `project.version` in `sdk/vibesys-evaluator/python/pyproject.toml`, in canonical PEP 440
   (`0.1.0`, or `0.1.0rc1` for a release candidate), and merge the change to `main`.
2. Create the tag `evaluator-v<version>` on that commit, for example `evaluator-v0.1.0rc1`, and
   publish a GitHub release for it with "Set as the latest release" off
   (`gh release create evaluator-v0.1.0rc1 --latest=false --prerelease` for a candidate). A
   published release is the only trigger.
3. The workflow refuses the release unless the tag's version equals `project.version` and the
   tagged commit is contained in `main`, and also when the release took the "latest" marker. It
   then builds the wheel and the images without any write permission and verifies them: the wheel
   installs and imports in a clean environment, `pyright --verifytypes` passes on the installed
   package, the package's tests, its public API snapshot, the loadgen Go tests and the conformance
   fixtures pass, both images import the package at the release version, the microservices
   image runs `loadgen --capabilities` with the release version, and the llm-serving image holds
   the harness version the package replays with.
4. The wheel that was verified is published by Trusted Publishing, from the only job that runs in
   `pypi-evaluator`. Then each image that was verified is pushed by digest-preserving copy, so
   the digest recorded at build time is the one published, and its build provenance is attested
   (`gh attestation verify oci://ghcr.io/uw-syfi/vibesys-evaluator-<domain>@<digest> --repo uw-syfi/vibesys`).
   Last, the Go module tag is created on the same commit and each image reference with its digest
   is added to the release notes. A release candidate's Go tag is the semantic version
   `v0.1.0-rc.1`.

Evaluator releases run one at a time, and a started one is never cancelled. Never take GitHub's
"latest" marker with an evaluator release, for the reason given under the `vibesys-cli` releases
above: it belongs to the Python releases. The workflow edits the release with `--latest=false`.

Recovery follows the wheel's rule: never delete or move a published tag, image tag or version. A
re-run after a partial failure is safe: it skips an image tag that already has the verified
digest and a Go tag that already points at the commit, and fails if either holds something else.
If a release is bad, yank the wheel on PyPI, mark the GitHub release as affected, and publish a
higher version.
