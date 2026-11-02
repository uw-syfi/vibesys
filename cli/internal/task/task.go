package task

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io/fs"
	"path"
	"sort"
	"strings"
	"time"

	"github.com/BurntSushi/toml"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// Where a task's files are, from the root of a repository.
const (
	TasksDir      = ".vibesys/tasks"
	ManifestName  = "vibesys.input.toml"
	ObjectiveName = "objectives.toml"
	dockerfile    = "Dockerfile"
)

// Defaults for what a task leaves out. The task format has no sample count, so the count is this
// built-in one until the format gains a key for it.
const (
	DefaultSamples        uint32 = 5
	DefaultCommandTimeout        = 30 * time.Minute
)

// ProjectRoot is the placeholder a command spells the checkout's root with.
const ProjectRoot = "${PROJECT_ROOT}"

// OutputFlagProtocol2 is the flag a benchmark that speaks the evaluator result protocol takes
// the path of its record stream after.
const OutputFlagProtocol2 = "--vs-output"

// Command is one program a task runs, from the root of the checkout.
type Command struct {
	// Argv is the program and its arguments, with ${PROJECT_ROOT} not yet replaced.
	Argv []string
	// Timeout bounds the command. It is positive.
	Timeout time.Duration
}

// Resolve returns the argv with ${PROJECT_ROOT} replaced by the checkout's root.
func (c Command) Resolve(root string) []string {
	out := make([]string, len(c.Argv))
	for i, a := range c.Argv {
		out[i] = strings.ReplaceAll(a, ProjectRoot, root)
	}
	return out
}

// Result says how a benchmark reports what it measured.
type Result struct {
	// Protocol2 is set when the benchmark speaks the evaluator result protocol (version 2): it
	// writes a stream of records to the path after OutputFlagProtocol2.
	Protocol2 bool
	// JSONArgument is the flag, for a benchmark that writes one JSON document, that the path of
	// the document follows.
	JSONArgument string
	// Field is the top-level field of that document that holds the scored value.
	Field string
}

// OutputFlag is the flag the benchmark takes the path of its result after.
func (r Result) OutputFlag() string {
	if r.Protocol2 {
		return OutputFlagProtocol2
	}
	return r.JSONArgument
}

// Task is a task as a check or a measure job runs it.
type Task struct {
	Name string
	// Version names the task's files: results taken under another version are not comparable.
	Version string
	Backend core.BackendKind
	// HasDockerfile says the task ships a Dockerfile, so its jobs run in docker.
	HasDockerfile bool
	// NestedDocker says the task starts containers of its own, with [environment]
	// docker_in_docker = true. A container run by this build has no container runtime in it, so
	// a docker environment refuses the task instead of running it without what it needs.
	NestedDocker bool
	// Build runs first, when the task has one.
	Build     *Command
	Check     Command
	Benchmark Command
	Result    Result
	// Metric is the scored quantity and which way it improves.
	Metric core.Metric
	// Samples is how many benchmark samples a measurement takes when the submission does not say.
	Samples uint32
	// Others are the metrics a protocol 2 benchmark reports besides the scored one, and which way
	// each improves. They are recorded beside the result and never scored.
	Others []core.Metric
}

// Setup is what the task declares that decides where its jobs run.
func (t Task) Setup() core.TaskSetup {
	return core.TaskSetup{Backend: t.Backend, HasDockerfile: t.HasDockerfile}
}

// ValidName says whether name is a task name: one path element, which cannot reach outside the
// tasks directory. It fails with FailureInvalidInput.
func ValidName(name string) error {
	if name == "" || strings.ContainsAny(name, "/\\") || name == "." || name == ".." {
		return invalid("task: %q is not a task name", name)
	}
	return nil
}

// Read reads the task called name from a repository whose root is fsys.
func Read(fsys fs.FS, name string) (Task, error) {
	if err := ValidName(name); err != nil {
		return Task{}, err
	}
	dir := path.Join(TasksDir, name)
	info, err := fs.Stat(fsys, dir)
	if err != nil || !info.IsDir() {
		return Task{}, &core.Failure{Kind: core.FailureNotFound, Message: fmt.Sprintf("task %q: there is no directory %s at this commit", name, dir)}
	}
	manifest, err := fs.ReadFile(fsys, path.Join(dir, ManifestName))
	if err != nil {
		return Task{}, &core.Failure{Kind: core.FailureNotFound, Message: fmt.Sprintf("task %q: no %s in %s", name, ManifestName, dir)}
	}
	objectives, err := readOptional(fsys, path.Join(dir, ObjectiveName))
	if err != nil {
		return Task{}, err
	}
	t, err := Parse(name, manifest, objectives)
	if err != nil {
		return Task{}, err
	}
	if _, err := fs.Stat(fsys, path.Join(dir, dockerfile)); err == nil {
		t.HasDockerfile = true
	}
	if t.Version, err = Version(fsys, dir); err != nil {
		return Task{}, err
	}
	return t, nil
}

// nestedDocker reads [environment] docker_in_docker. The section is otherwise held as data, but
// this key decides whether a container can run the task, so a value that is not a boolean is
// refused instead of read as false.
func nestedDocker(environment map[string]any) (bool, error) {
	v, ok := environment["docker_in_docker"]
	if !ok {
		return false, nil
	}
	b, ok := v.(bool)
	if !ok {
		return false, invalid("%s: [environment] docker_in_docker must be true or false, got %v", ManifestName, v)
	}
	return b, nil
}

func readOptional(fsys fs.FS, name string) ([]byte, error) {
	data, err := fs.ReadFile(fsys, name)
	if errors.Is(err, fs.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, &core.Failure{Kind: core.FailureUnavailable, Message: fmt.Sprintf("reading %s: %v", name, err)}
	}
	return data, nil
}

// Version names the files under dir: every regular file, by its path from dir, its executable
// bit and the digest of its content. It does not depend on the order files are listed in, on
// times, or on anything outside the directory.
func Version(fsys fs.FS, dir string) (string, error) {
	type entry struct{ path, line string }
	var entries []entry
	err := fs.WalkDir(fsys, dir, func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() {
			return nil
		}
		info, err := d.Info()
		if err != nil {
			return err
		}
		if !info.Mode().IsRegular() {
			return nil
		}
		data, err := fs.ReadFile(fsys, p)
		if err != nil {
			return err
		}
		sum := sha256.Sum256(data)
		exec := "-"
		if info.Mode()&0o111 != 0 {
			exec = "x"
		}
		rel := strings.TrimPrefix(p, dir+"/")
		entries = append(entries, entry{rel, fmt.Sprintf("%s %s %x\n", exec, rel, sum)})
		return nil
	})
	if err != nil {
		return "", &core.Failure{Kind: core.FailureUnavailable, Message: fmt.Sprintf("reading the files of %s: %v", dir, err)}
	}
	sort.Slice(entries, func(i, j int) bool { return entries[i].path < entries[j].path })
	h := sha256.New()
	for _, e := range entries {
		h.Write([]byte(e.line))
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}

func invalid(format string, args ...any) error {
	return &core.Failure{Kind: core.FailureInvalidInput, Message: fmt.Sprintf(format, args...)}
}

func unsupported(format string, args ...any) error {
	return &core.Failure{Kind: core.FailureUnavailable, Message: fmt.Sprintf(format, args...)}
}

// manifest is vibesys.input.toml as this reader sees it. A section the job flows do not use is
// held as data so that it is accepted without being interpreted; the Python product validates
// them. Every key that is not in a section below is an error.
type manifest struct {
	Version  int      `toml:"version"`
	Backends []string `toml:"backends"`
	Metric   string   `toml:"metric"`

	Build     *buildSection   `toml:"build"`
	Accuracy  *commandSection `toml:"accuracy"`
	Check     *commandSection `toml:"check"`
	Benchmark *benchSection   `toml:"benchmark"`
	Measure   *benchSection   `toml:"measure"`

	// Sections that do not change how a commit is checked or measured on this machine.
	Agent         map[string]any   `toml:"agent"`
	ProfileGuided map[string]any   `toml:"profile_guided"`
	Profile       map[string]any   `toml:"profile"`
	Resources     map[string]any   `toml:"resources"`
	Workspace     map[string]any   `toml:"workspace"`
	Plan          map[string]any   `toml:"plan"`
	Catalogue     map[string]any   `toml:"catalogue"`
	Comparison    []map[string]any `toml:"comparison"`

	// Sections that would change it, and so are refused.
	Environment map[string]any `toml:"environment"`
	Evaluator   map[string]any `toml:"evaluator"`
}

type buildSection struct {
	Command        []string `toml:"command"`
	TimeoutSeconds int      `toml:"timeout_seconds"`
}

type commandSection struct {
	Command        []string `toml:"command"`
	Entrypoint     string   `toml:"entrypoint"`
	Args           []string `toml:"args"`
	TimeoutSeconds int      `toml:"timeout_seconds"`
}

type benchSection struct {
	commandSection
	Result           *resultSection `toml:"result"`
	ResultProtocol   int            `toml:"result_protocol"`
	RequiresAccuracy *bool          `toml:"requires_accuracy"`
}

type resultSection struct {
	JSONArgument string `toml:"json_argument"`
	Metric       string `toml:"metric"`
}

// objectivesFile is objectives.toml.
type objectivesFile struct {
	Objective []struct {
		Name      string `toml:"name"`
		Direction string `toml:"direction"`
	} `toml:"objective"`
	Pareto *struct {
		RelativeNoise float64 `toml:"relative_noise"`
	} `toml:"pareto"`
}

// opaque lists the manifest sections held as data: the decoder reports the keys nested inside
// them as undecoded, and they are accepted without being read.
var opaque = map[string]bool{
	"agent": true, "profile_guided": true, "profile": true, "resources": true, "workspace": true,
	"plan": true, "catalogue": true, "comparison": true, "environment": true, "evaluator": true,
}

// decode decodes TOML into v and rejects every key v has no field for, naming it. A key nested
// inside an opaque section is not checked.
func decode(file string, data []byte, v any, skip map[string]bool) error {
	meta, err := toml.Decode(string(data), v)
	if err != nil {
		return invalid("%s: %v", file, err)
	}
	for _, k := range meta.Undecoded() {
		if len(k) > 0 && skip[k[0]] {
			continue
		}
		return invalid("%s: unknown key %q", file, k.String())
	}
	return nil
}

// Parse reads a manifest and an objectives file (nil when the task has none) without touching
// the task's directory. Version and HasDockerfile are left for Read, which sees the directory.
func Parse(name string, manifestTOML, objectivesTOML []byte) (Task, error) {
	var m manifest
	if err := decode(ManifestName, manifestTOML, &m, opaque); err != nil {
		return Task{}, err
	}
	var o objectivesFile
	if objectivesTOML != nil {
		if err := decode(ObjectiveName, objectivesTOML, &o, nil); err != nil {
			return Task{}, err
		}
	}
	return build(name, m, o)
}

func build(name string, m manifest, o objectivesFile) (Task, error) {
	if m.Version != 1 {
		return Task{}, invalid("%s: version must be 1, got %d", ManifestName, m.Version)
	}
	if m.Evaluator != nil {
		return Task{}, unsupported("%s: [evaluator] tasks are not supported by this build; their commands are run by an evaluator package", ManifestName)
	}
	if _, modal := m.Environment["modal"]; modal {
		return Task{}, unsupported("%s: [environment.modal] cannot be honored on this machine", ManifestName)
	}
	t := Task{Name: name, Samples: DefaultSamples}
	nested, err := nestedDocker(m.Environment)
	if err != nil {
		return Task{}, err
	}
	t.NestedDocker = nested
	backend, err := backendOf(m.Backends)
	if err != nil {
		return Task{}, err
	}
	t.Backend = backend

	check, err := pick("accuracy", m.Accuracy, "check", m.Check)
	if err != nil {
		return Task{}, err
	}
	if t.Check, err = check.command("accuracy"); err != nil {
		return Task{}, err
	}
	bench, err := pickBench(m.Benchmark, m.Measure)
	if err != nil {
		return Task{}, err
	}
	if t.Benchmark, err = bench.command("benchmark"); err != nil {
		return Task{}, err
	}
	if bench.RequiresAccuracy != nil && !*bench.RequiresAccuracy {
		return Task{}, unsupported("%s: requires_accuracy = false is not supported; a measurement is saved only after its check passed", ManifestName)
	}
	if m.Build != nil {
		c, err := buildCommand(*m.Build)
		if err != nil {
			return Task{}, err
		}
		t.Build = &c
	}
	for _, c := range []Command{t.Check, t.Benchmark} {
		if err := checkPlaceholders(c); err != nil {
			return Task{}, err
		}
	}
	if t.Build != nil {
		if err := checkPlaceholders(*t.Build); err != nil {
			return Task{}, err
		}
	}
	axes, err := objectives(o)
	if err != nil {
		return Task{}, err
	}
	if err := resultOf(&t, bench, m.Metric, axes); err != nil {
		return Task{}, err
	}
	return t, nil
}

func backendOf(names []string) (core.BackendKind, error) {
	switch len(names) {
	case 0:
		return core.BackendCPU, nil
	case 1:
		b := core.BackendKind(names[0])
		if !b.Known() {
			return "", invalid("%s: backends: %q is not a known backend", ManifestName, names[0])
		}
		return b, nil
	default:
		return "", unsupported("%s: backends: a task for several backends is not supported by this build", ManifestName)
	}
}

func pick(nameA string, a *commandSection, nameB string, b *commandSection) (*commandSection, error) {
	switch {
	case a != nil && b != nil:
		return nil, invalid("%s: declare [%s] or [%s], not both", ManifestName, nameA, nameB)
	case a != nil:
		return a, nil
	case b != nil:
		return b, nil
	}
	return nil, invalid("%s: no [%s] (or [%s]) section", ManifestName, nameA, nameB)
}

func pickBench(a, b *benchSection) (*benchSection, error) {
	switch {
	case a != nil && b != nil:
		return nil, invalid("%s: declare [benchmark] or [measure], not both", ManifestName)
	case a != nil:
		return a, nil
	case b != nil:
		return b, nil
	}
	return nil, invalid("%s: no [benchmark] (or [measure]) section", ManifestName)
}

func (c *commandSection) command(section string) (Command, error) {
	if c.Entrypoint != "" || len(c.Args) > 0 {
		return Command{}, unsupported("%s: [%s] names an evaluator entrypoint (%q), which this build does not run; declare a command", ManifestName, section, c.Entrypoint)
	}
	return makeCommand(section, c.Command, c.TimeoutSeconds)
}

func buildCommand(b buildSection) (Command, error) {
	c, err := makeCommand("build", b.Command, b.TimeoutSeconds)
	if err != nil {
		return Command{}, err
	}
	if path.IsAbs(c.Argv[0]) {
		return Command{}, invalid("%s: [build] command: the program must be a name or a project-relative path, got %q", ManifestName, c.Argv[0])
	}
	return c, nil
}

func makeCommand(section string, argv []string, seconds int) (Command, error) {
	if len(argv) == 0 {
		return Command{}, invalid("%s: [%s] needs a command with at least one element", ManifestName, section)
	}
	for i, a := range argv {
		if a == "" {
			return Command{}, invalid("%s: [%s] command element %d is empty", ManifestName, section, i)
		}
	}
	if seconds < 0 {
		return Command{}, invalid("%s: [%s] timeout_seconds must be positive, got %d", ManifestName, section, seconds)
	}
	timeout := DefaultCommandTimeout
	if seconds > 0 {
		timeout = time.Duration(seconds) * time.Second
	}
	return Command{Argv: append([]string(nil), argv...), Timeout: timeout}, nil
}

// checkPlaceholders refuses a ${...} other than ${PROJECT_ROOT}: it would reach the program
// unreplaced.
func checkPlaceholders(c Command) error {
	for _, a := range c.Argv {
		rest := strings.ReplaceAll(a, ProjectRoot, "")
		if i := strings.Index(rest, "${"); i >= 0 {
			end := strings.IndexByte(rest[i:], '}')
			if end < 0 {
				end = len(rest) - i - 1
			}
			return unsupported("%s: the placeholder %s is not supported; only %s is", ManifestName, rest[i:i+end+1], ProjectRoot)
		}
	}
	return nil
}

type axis struct {
	name      string
	direction core.Direction
}

func objectives(o objectivesFile) ([]axis, error) {
	var axes []axis
	seen := map[string]bool{}
	for i, obj := range o.Objective {
		if obj.Name == "" {
			return nil, invalid("%s: objective %d has no name", ObjectiveName, i+1)
		}
		if seen[obj.Name] {
			return nil, invalid("%s: objective %q is declared twice", ObjectiveName, obj.Name)
		}
		seen[obj.Name] = true
		var d core.Direction
		switch obj.Direction {
		case "max":
			d = core.Maximize
		case "min":
			d = core.Minimize
		default:
			return nil, invalid("%s: objective %q: direction must be \"max\" or \"min\", got %q", ObjectiveName, obj.Name, obj.Direction)
		}
		axes = append(axes, axis{obj.Name, d})
	}
	if o.Pareto != nil && (o.Pareto.RelativeNoise < 0 || o.Pareto.RelativeNoise >= 1) {
		return nil, invalid("%s: [pareto] relative_noise must be at least 0 and less than 1, got %v", ObjectiveName, o.Pareto.RelativeNoise)
	}
	return axes, nil
}

func find(axes []axis, name string) (axis, bool) {
	for _, a := range axes {
		if a.name == name {
			return a, true
		}
	}
	return axis{}, false
}

// resultOf reads the benchmark's result contract and the scored metric from it and from the
// objectives.
func resultOf(t *Task, b *benchSection, metric string, axes []axis) error {
	switch {
	case b.Result != nil && b.ResultProtocol != 0:
		return invalid("%s: [benchmark.result] and result_protocol are mutually exclusive; declare one", ManifestName)
	case b.Result != nil:
		r := b.Result
		if !strings.HasPrefix(r.JSONArgument, "-") || strings.ContainsAny(r.JSONArgument, " \t\n") {
			return invalid("%s: [benchmark.result] json_argument must be one option-style argument, got %q", ManifestName, r.JSONArgument)
		}
		if r.Metric == "" || strings.ContainsAny(r.Metric, " \t\n") {
			return invalid("%s: [benchmark.result] metric must be a field name without whitespace, got %q", ManifestName, r.Metric)
		}
		a, ok := find(axes, r.Metric)
		if !ok {
			return invalid("%s: has no objective for the metric %q; declare it with its direction", ObjectiveName, r.Metric)
		}
		t.Result = Result{JSONArgument: r.JSONArgument, Field: r.Metric}
		t.Metric = core.Metric{Name: r.Metric, Direction: a.direction}
	case b.ResultProtocol == 2:
		t.Result = Result{Protocol2: true}
		scored := metric
		if scored == "" && len(axes) > 0 {
			scored = axes[0].name
		}
		if scored == "" {
			return invalid("%s: a protocol 2 benchmark needs a scored metric: set metric, or declare an objective", ManifestName)
		}
		a, ok := find(axes, scored)
		if !ok {
			return invalid("%s: has no objective for the metric %q; declare it with its direction", ObjectiveName, scored)
		}
		t.Metric = core.Metric{Name: scored, Direction: a.direction}
		for _, other := range axes {
			if other.name != scored {
				t.Others = append(t.Others, core.Metric{Name: other.name, Direction: other.direction})
			}
		}
	case b.ResultProtocol != 0:
		return invalid("%s: result_protocol %d is not supported; the protocol version is 2", ManifestName, b.ResultProtocol)
	default:
		return unsupported("%s: the benchmark declares no result contract; this build measures only a benchmark with [benchmark.result] or result_protocol = 2", ManifestName)
	}
	return nil
}
