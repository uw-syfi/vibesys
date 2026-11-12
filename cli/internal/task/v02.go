package task

import (
	"errors"
	"path"
	"slices"
	"strings"
	"time"

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

// Service is the job-scoped server of a task of one image: a job starts it once and runs its
// samples, and its check when ShareWithCheck, against it.
type Service struct {
	// Serve is the server's command, run in a container of the task's image from the checkout's
	// root, with ProjectRoot and core.PortPlaceholder not yet replaced. It names the port.
	Serve []string
	// Ready is the HTTP path that answers 2xx once the server is ready.
	Ready string
	// StartupTimeout bounds the wait for it.
	StartupTimeout time.Duration
	// Reset runs against the server before every sample that reuses it; exit 0 lets the sample run.
	Reset Command
	// ShareWithCheck starts the server before the check, which then runs against it, as the samples
	// do. Without it the check runs as a task without a server runs it, and the server starts after.
	ShareWithCheck bool
}

// ServeAt is the server as an execution starts it in the checkout at root: ProjectRoot replaced,
// and the port left to the environment.
func (s Service) ServeAt(root string) []string { return Command{Argv: s.Serve}.Resolve(root) }

// parseV02 reads a manifest of task schema 0.2 (TaskManifestV02 in task.proto). Without [candidate]
// the task has no Candidate, as a task of 0.1: its commands run in the task image and own the system
// under test.
func parseV02(name string, table map[string]any, o *cliv1.TaskObjectives) (Task, error) {
	if err := refusedV02(table); err != nil {
		return Task{}, err
	}
	var m cliv1.TaskManifestV02
	declared := Schema02
	if err := tomlproto.DecodeTable(table, &m, tomlproto.Options{Declared: &declared}); err != nil {
		return Task{}, invalid("%s: %s", ManifestName, core.AsFailure(err).Message)
	}
	if err := validate(ManifestName, &m); err != nil {
		return Task{}, err
	}
	if c, ok := table["catalogue"].(map[string]any); ok {
		if _, err := Catalogue(c); err != nil {
			return Task{}, err
		}
	}
	t := Task{Name: name, Samples: DefaultSamples, Schema: Schema02}
	var err error
	if t.Backend, err = backendOf(m.GetBackends()); err != nil {
		return Task{}, err
	}
	env := m.GetEnvironment()
	if env.GetModal() != nil {
		return Task{}, unsupported("%s: [environment.modal] cannot be honored on this machine", ManifestName)
	}
	t.NestedDocker = env.GetDockerInDocker()
	if t.Needs, err = needsOf("environment.needs", env.GetNeeds()); err != nil {
		return Task{}, err
	}
	check, err := pickV02("accuracy", m.GetAccuracy(), "check", m.GetCheck())
	if err != nil {
		return Task{}, err
	}
	if t.Check, err = makeCommand("accuracy", check.GetCommand(), int(check.GetTimeoutSeconds())); err != nil {
		return Task{}, err
	}
	b, err := pickV02("benchmark", m.GetBenchmark(), "measure", m.GetMeasure())
	if err != nil {
		return Task{}, err
	}
	if t.Benchmark, err = makeCommand("benchmark", b.GetCommand(), int(b.GetTimeoutSeconds())); err != nil {
		return Task{}, err
	}
	if b.RequiresAccuracy != nil && !b.GetRequiresAccuracy() {
		return Task{}, unsupported("%s: requires_accuracy = false is not supported; a measurement is saved only after its check passed", ManifestName)
	}
	if bc := m.GetBuild(); bc != nil {
		c, err := buildCommand(buildSection{Command: bc.GetCommand(), TimeoutSeconds: int(bc.GetTimeoutSeconds())})
		if err != nil {
			return Task{}, err
		}
		if err := checkPlaceholders(c); err != nil {
			return Task{}, err
		}
		t.Build = &c
	}
	// [profile] is retired: it is held to its shape, as the decoder already did, and not read.
	for _, c := range []Command{t.Check, t.Benchmark} {
		if err := checkPlaceholders(c); err != nil {
			return Task{}, err
		}
	}
	if m.GetCandidate() != nil {
		if t.Candidate, err = candidateOf(m.GetCandidate()); err != nil {
			return Task{}, err
		}
	}
	if m.GetService() != nil {
		if t.Candidate != nil {
			return Task{}, invalid("%s: [service] is for a task of one image; a task with [candidate] starts its candidate's server for each command, and cannot keep it for the job", ManifestName)
		}
		if t.Service, err = serviceOf(m.GetService()); err != nil {
			return Task{}, err
		}
	}
	bench := &benchSection{ResultProtocol: int(b.GetResultProtocol())}
	if r := b.GetResult(); r != nil {
		bench.Result = &resultSection{JSONArgument: r.GetJsonArgument(), Metric: r.GetMetric()}
	}
	if err := score(&t, bench, m.GetMetric(), o); err != nil {
		return Task{}, err
	}
	return t, nil
}

// candidateOf reads [candidate]: the image the candidate's commit builds, and the server it starts.
func candidateOf(c *cliv1.TaskCandidateV02) (*Candidate, error) {
	dockerfile, err := repositoryPath("candidate.dockerfile", c.GetDockerfile(), false)
	if err != nil {
		return nil, err
	}
	context, err := repositoryPath("candidate.context", c.GetContext(), true)
	if err != nil {
		return nil, err
	}
	if err := checkPlaceholders(Command{Argv: c.GetServe()}, core.PortPlaceholder); err != nil {
		return nil, err
	}
	return &Candidate{
		Build:          core.CandidateBuild{Dockerfile: dockerfile, Context: context, AllowedBases: slices.Clone(c.GetAllowedBases())},
		Serve:          slices.Clone(c.GetServe()),
		Ready:          c.GetReady(),
		StartupTimeout: time.Duration(c.GetStartupTimeoutSeconds()) * time.Second,
	}, nil
}

// serviceOf reads [service]: the server a job starts once, and the reset before each reuse of it.
func serviceOf(s *cliv1.TaskServiceV02) (*Service, error) {
	serve := Command{Argv: s.GetCommand()}
	if err := checkPlaceholders(serve, core.PortPlaceholder); err != nil {
		return nil, err
	}
	if !slices.ContainsFunc(serve.Argv, func(a string) bool { return strings.Contains(a, core.PortPlaceholder) }) {
		return nil, invalid("%s: [service] command must name %s, the port the CLI chooses for the server and reaches it on", ManifestName, core.PortPlaceholder)
	}
	reset, err := makeCommand("service.reset", s.GetReset_().GetCommand(), int(s.GetReset_().GetTimeoutSeconds()))
	if err != nil {
		return nil, err
	}
	if err := checkPlaceholders(reset); err != nil {
		return nil, err
	}
	return &Service{
		Serve:          slices.Clone(serve.Argv),
		Ready:          s.GetReady(),
		StartupTimeout: time.Duration(s.GetStartupTimeoutSeconds()) * time.Second,
		Reset:          reset,
		ShareWithCheck: s.GetShareWithAccuracy(),
	}, nil
}

// commandSections are the sections of a manifest that hold a command.
var commandSections = []string{"accuracy", "check", "benchmark", "measure", "profile", "build"}

// refusedV02 refuses, by name, what 0.1 has and 0.2 does not: an evaluator package, and a command
// given as an evaluator's entrypoint and args.
func refusedV02(table map[string]any) error {
	if _, ok := table["evaluator"]; ok {
		return invalid("%s: [evaluator] is not part of task schema %s: the task's commands run in its own image, and the candidate's server in the candidate's", ManifestName, Schema02)
	}
	for _, section := range commandSections {
		s, _ := table[section].(map[string]any)
		for _, key := range []string{"entrypoint", "args"} {
			if _, ok := s[key]; ok {
				return invalid("%s: [%s] %s is not part of task schema %s: declare the command, which runs in the task's image", ManifestName, section, key, Schema02)
			}
		}
	}
	return nil
}

// pickV02 is the section of the two names the manifest uses: one of them, never both.
func pickV02[T interface{ comparable }](nameA string, a T, nameB string, b T) (T, error) {
	var none T
	switch {
	case a != none && b != none:
		return none, invalid("%s: declare [%s] or [%s], not both", ManifestName, nameA, nameB)
	case a != none:
		return a, nil
	case b != none:
		return b, nil
	}
	return none, invalid("%s: no [%s] (or [%s]) section", ManifestName, nameA, nameB)
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
