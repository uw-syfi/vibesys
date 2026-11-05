package task

import (
	"errors"
	"path"
	"slices"
	"strings"
	"time"

	"buf.build/go/protovalidate"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/tomlproto"
)

// Candidate is how a task of two images builds and starts the candidate: the image its commit's
// Dockerfile builds, and the server the task's commands reach over HTTP.
type Candidate struct {
	Build core.CandidateBuild
	// Serve is the server's command, run in the candidate container from the checkout's root, with
	// ProjectRoot and core.PortPlaceholder not yet replaced.
	Serve []string
	// Ready is the HTTP path that answers 2xx once the server is ready.
	Ready string
	// StartupTimeout bounds the wait for it.
	StartupTimeout time.Duration
}

// ServeAt is the server as an execution starts it in the checkout at root: ProjectRoot replaced,
// the port left to the environment, and the argv prefix wrap, when there is one, in front.
func (c Candidate) ServeAt(root string, wrap []string) core.CandidateServe {
	argv := append(slices.Clone(wrap), Command{Argv: c.Serve}.Resolve(root)...)
	return core.CandidateServe{Argv: argv, Ready: c.Ready, StartupTimeout: c.StartupTimeout}
}

// parseV02 reads a manifest of task schema 0.2 (TaskManifestV02 in task.proto).
func parseV02(name string, table map[string]any, o objectivesFile) (Task, error) {
	if _, ok := table["evaluator"]; ok {
		return Task{}, invalid("%s: [evaluator] is not part of task schema %s: the task's commands run in its own image, and the candidate's server in the candidate's", ManifestName, Schema02)
	}
	var m cliv1.TaskManifestV02
	declared := Schema02
	if err := tomlproto.DecodeTable(table, &m, tomlproto.Options{Declared: &declared}); err != nil {
		return Task{}, invalid("%s: %s", ManifestName, core.AsFailure(err).Message)
	}
	if err := protovalidate.Validate(&m); err != nil {
		return Task{}, invalid("%s: %s", ManifestName, strings.TrimPrefix(strings.ReplaceAll(err.Error(), "\n", " "), "validation error: "))
	}
	t := Task{Name: name, Samples: DefaultSamples, Schema: Schema02}
	var err error
	if t.Backend, err = backendOf(m.GetBackends()); err != nil {
		return Task{}, err
	}
	if t.Needs, err = needsOf("environment.needs", m.GetEnvironment().GetNeeds()); err != nil {
		return Task{}, err
	}
	if t.Check, err = makeCommand("accuracy", m.GetAccuracy().GetCommand(), int(m.GetAccuracy().GetTimeoutSeconds())); err != nil {
		return Task{}, err
	}
	b := m.GetBenchmark()
	if t.Benchmark, err = makeCommand("benchmark", b.GetCommand(), int(b.GetTimeoutSeconds())); err != nil {
		return Task{}, err
	}
	if p := m.GetProfile(); p != nil {
		c, err := makeCommand("profile", p.GetCommand(), int(p.GetTimeoutSeconds()))
		if err != nil {
			return Task{}, err
		}
		t.Profile = &c
	}
	for _, c := range []Command{t.Check, t.Benchmark} {
		if err := checkPlaceholders(c); err != nil {
			return Task{}, err
		}
	}
	if t.Profile != nil {
		if err := checkPlaceholders(*t.Profile); err != nil {
			return Task{}, err
		}
	}
	c := m.GetCandidate()
	dockerfile, err := repositoryPath("candidate.dockerfile", c.GetDockerfile(), false)
	if err != nil {
		return Task{}, err
	}
	context, err := repositoryPath("candidate.context", c.GetContext(), true)
	if err != nil {
		return Task{}, err
	}
	if err := checkPlaceholders(Command{Argv: c.GetServe()}, core.PortPlaceholder); err != nil {
		return Task{}, err
	}
	t.Candidate = &Candidate{
		Build:          core.CandidateBuild{Dockerfile: dockerfile, Context: context, AllowedBases: slices.Clone(c.GetAllowedBases())},
		Serve:          slices.Clone(c.GetServe()),
		Ready:          c.GetReady(),
		StartupTimeout: time.Duration(c.GetStartupTimeoutSeconds()) * time.Second,
	}
	bench := &benchSection{ResultProtocol: int(b.GetResultProtocol())}
	if err := score(&t, bench, m.GetMetric(), o); err != nil {
		return Task{}, err
	}
	return t, nil
}

// repositoryPath checks a path of the candidate's checkout: relative, clean, and inside it. A
// directory may be the checkout's root, ".".
func repositoryPath(key, p string, dir bool) (string, error) {
	switch {
	case path.IsAbs(p) || path.Clean(p) != p:
		return "", invalid("%s: %s must be a clean path in the candidate's checkout, relative to its root, got %q", ManifestName, key, p)
	case p == ".." || strings.HasPrefix(p, "../"):
		return "", invalid("%s: %s must be inside the candidate's checkout, got %q", ManifestName, key, p)
	case p == "." && !dir:
		return "", invalid("%s: %s must name a file, got %q", ManifestName, key, p)
	}
	return p, nil
}

// needsOf reads a list of resource kinds, each named once. A name that is not a kind is refused,
// naming the kinds there are, instead of being read as no need.
func needsOf(key string, names []string) ([]core.ResourceKind, error) {
	var out []core.ResourceKind
	for i, name := range names {
		kind, err := core.ParseResourceKind(name)
		if err != nil {
			var f *core.Failure
			if errors.As(err, &f) {
				return nil, invalid("%s: %s[%d]: %v", ManifestName, key, i, f.Message)
			}
			return nil, err
		}
		if slices.Contains(out, kind) {
			return nil, invalid("%s: %s names %q twice", ManifestName, key, name)
		}
		out = append(out, kind)
	}
	return out, nil
}
