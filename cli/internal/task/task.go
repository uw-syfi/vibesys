package task

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io/fs"
	"maps"
	"path"
	"reflect"
	"slices"
	"sort"
	"strings"
	"time"

	"github.com/BurntSushi/toml"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
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

// MaxCommandTimeoutSeconds is the longest a command's timeout_seconds may ask for: one week. It
// matches the proto cap on TaskCommandV02.timeout_seconds (buf.validate uint32.lte = 604800), so a
// schema 0.1 manifest is held to the same bound the schema 0.2 reader enforces. A larger value
// also overflows time.Duration once scaled to nanoseconds, yielding a negative or tiny timeout, so
// it is refused here rather than silently accepted.
const MaxCommandTimeoutSeconds = 604800

// ProjectRoot is the placeholder a command spells the checkout's root with.
const ProjectRoot = core.ProjectRootPlaceholder

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
	return core.ResolveProjectRoot(c.Argv, root)
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
	// Files are the files Version names, by path from the task's directory (VersionedFiles).
	Files map[string]string
	// Context is the content of the same files when the task has a Dockerfile: its image's build
	// context, which with Version pins the image the jobs run in. Its symbolic links are kept as
	// links, and each leads to a file or a directory of the context: Read refuses a task with
	// a Dockerfile whose links do not. It is nil when HasDockerfile is false.
	Context Tree
	Backend core.BackendKind
	// HasDockerfile says the task ships a Dockerfile, so its jobs run in docker.
	HasDockerfile bool
	// Dockerfile is that file's text at the task's commit, which pins the image the jobs run in.
	// It is empty when HasDockerfile is false.
	Dockerfile []byte
	// NestedDocker says the task starts containers of its own, with [environment]
	// docker_in_docker = true. A container run by this build has no container runtime in it, so
	// a docker environment refuses the task instead of running it without what it needs.
	NestedDocker bool
	// Needs are the resources a docker job of the task needs its placement to give its container,
	// from [environment] needs, each once, in the order written. A placement that lacks one does
	// not run the task's docker jobs.
	Needs []core.ResourceKind
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
	// each improves. They are recorded beside the result, and scored only as Objectives.
	Others []core.Metric
	// Objectives is set for a multi-objective (Pareto) task: one whose objectives.toml has a
	// [pareto] section and more than one objective. Its measurements are judged on every objective,
	// in the order declared, under the [pareto] relative_noise. It is the zero value for any other
	// task, which is judged on Metric alone.
	Objectives core.Objectives

	// Schema is the task schema the manifest declares.
	Schema SchemaVersion
	// Candidate is set for a task of two images (a Schema02 manifest with [candidate]): the candidate's
	// image and server. Its
	// Check and Benchmark run in the task's image against that server.
	Candidate *Candidate
	// Service is set for a task of one image with a job-scoped server (a Schema02 manifest with
	// [service]): a job starts it once and runs its samples against it.
	Service *Service
}

// Declared is every objective the task declares, which way each improves: the scored metric first,
// then the other metrics its benchmark reports, in the order the task lists them. A task that
// declares one metric has that one.
func (t Task) Declared() []core.Metric {
	return append([]core.Metric{t.Metric}, t.Others...)
}

// Setup is what the task declares that decides where its jobs run.
func (t Task) Setup() core.TaskSetup {
	return core.TaskSetup{Backend: t.Backend, HasDockerfile: t.HasDockerfile, TwoImages: t.Candidate != nil, JobServer: t.Service != nil}
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
		// Parse names the file by its name, so the refusal also says whose directory it is in.
		var f *core.Failure
		if errors.As(err, &f) {
			return Task{}, &core.Failure{Kind: f.Kind, Message: fmt.Sprintf("task %q (%s): %s", name, dir, f.Message)}
		}
		return Task{}, err
	}
	switch text, err := fs.ReadFile(fsys, path.Join(dir, dockerfile)); {
	case err == nil:
		t.HasDockerfile, t.Dockerfile = true, text
	case !errors.Is(err, fs.ErrNotExist):
		return Task{}, &core.Failure{Kind: core.FailureUnavailable, Message: fmt.Sprintf("task %q: reading %s: %v", name, path.Join(dir, dockerfile), err)}
	}
	tree, err := versioned(fsys, dir)
	if err != nil {
		return Task{}, err
	}
	if t.Service != nil && !t.HasDockerfile {
		return Task{}, invalid("task %q: a task with a [service] needs a %s in %s: its server runs in a container of the task's image, beside the commands that run against it", name, dockerfile, dir)
	}
	if t.Candidate != nil && !t.HasDockerfile {
		return Task{}, invalid("task %q: a task with a [candidate] needs a %s in %s, which builds the task's image; the candidate's image is built from %s", name, dockerfile, dir, t.Candidate.Build.Dockerfile)
	}
	t.Files = fingerprints(tree)
	t.Version = versionOf(t.Files)
	if t.HasDockerfile {
		if err := imageLinks(name, dir, tree); err != nil {
			return Task{}, err
		}
		t.Context = tree
	}
	return t, nil
}

// imageLinks refuses a task whose image would lose a symbolic link of its directory. The image is
// built from the task's directory alone, so a link in it is kept as a link and works there only
// when it leads, link by link, to a file or a directory of the task: an absolute link, one that
// leaves the directory, or one to a file the commit does not have (a generated file, say) would
// be a link to nothing in the image, and a command would meet it only as a missing file. A task
// with no Dockerfile runs in the checkout, where git makes each link as the commit holds it, so
// its links are not checked.
func imageLinks(name, dir string, tree Tree) error {
	for _, rel := range slices.Sorted(maps.Keys(tree)) {
		if !tree[rel].IsLink() {
			continue
		}
		if _, err := tree.resolve(rel); err != nil {
			var bad *linkError
			if !errors.As(err, &bad) {
				return err
			}
			return invalid("task %q: %s/%s is a symbolic link to %q, which %s. The task's image is built from %s alone, so "+
				"a link there must lead to a file or a directory of it; make the link relative to one, commit the file itself, "+
				"or have the task's commands write it when they run",
				name, dir, bad.Path, bad.Target, bad.why("the task directory"), dir)
		}
	}
	return nil
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

// needs reads [environment] needs: a list of resource kinds, each named once. A name that is not
// a kind is refused, naming the kinds there are, instead of being read as no need.
func needs(environment map[string]any) ([]core.ResourceKind, error) {
	v, ok := environment["needs"]
	if !ok {
		return nil, nil
	}
	list, ok := v.([]any)
	if !ok {
		return nil, invalid("%s: [environment] needs must be a list of resource names, got %v", ManifestName, v)
	}
	var out []core.ResourceKind
	for i, item := range list {
		name, ok := item.(string)
		if !ok {
			return nil, invalid("%s: [environment] needs[%d] must be a resource name, got %v", ManifestName, i, item)
		}
		kind, err := core.ParseResourceKind(name)
		if err != nil {
			return nil, invalid("%s: [environment] needs[%d]: %v", ManifestName, i, core.AsFailure(err).Message)
		}
		if slices.Contains(out, kind) {
			return nil, invalid("%s: [environment] needs names %q twice", ManifestName, name)
		}
		out = append(out, kind)
	}
	return out, nil
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

// Version names the files under dir that belong to the task (VersionedFiles): each by its path
// from dir, its executable bit and the digest of its content. It does not depend on the order
// files are listed in, on times, or on anything outside the directory, and files a tool
// generates (Generated) do not count.
func Version(fsys fs.FS, dir string) (string, error) {
	files, err := VersionedFiles(fsys, dir)
	if err != nil {
		return "", err
	}
	return versionOf(files), nil
}

// VersionedFiles returns the files under dir that the task's version names: every regular file
// and symbolic link that is not Generated, by its path from dir, with a fingerprint of a file's
// executable bit and content or of a link's target. Two trees have one version exactly when they
// have equal VersionedFiles. A file of any other kind (a device, a socket) is refused.
func VersionedFiles(fsys fs.FS, dir string) (map[string]string, error) {
	tree, err := versioned(fsys, dir)
	if err != nil {
		return nil, err
	}
	return fingerprints(tree), nil
}

// versioned reads the files under dir that the task's version names.
func versioned(fsys fs.FS, dir string) (Tree, error) {
	files := Tree{}
	err := fs.WalkDir(fsys, dir, func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if p == dir {
			return nil
		}
		rel := strings.TrimPrefix(p, dir+"/")
		if d.IsDir() {
			if Generated(rel) {
				return fs.SkipDir
			}
			return nil
		}
		if Generated(rel) {
			return nil
		}
		if d.Type()&fs.ModeSymlink != 0 {
			target, err := fs.ReadLink(fsys, p)
			if err != nil {
				return err
			}
			files[rel] = File{Link: target}
			return nil
		}
		info, err := d.Info()
		if err != nil {
			return err
		}
		if !info.Mode().IsRegular() {
			return fmt.Errorf("%s is a %v and not a file, a directory or a symbolic link, which a task cannot hold", p, info.Mode().Type())
		}
		data, err := fs.ReadFile(fsys, p)
		if err != nil {
			return err
		}
		files[rel] = File{Data: data, Executable: info.Mode()&0o111 != 0}
		return nil
	})
	if err != nil {
		return nil, &core.Failure{Kind: core.FailureUnavailable, Message: fmt.Sprintf("reading the files of %s: %v", dir, err)}
	}
	return files, nil
}

// fingerprints are the files' executable bits and content digests, and the links' target digests,
// as VersionedFiles names them. A regular file's fingerprint is what it was before links were
// versioned, so a task with no link keeps its version.
func fingerprints(tree Tree) map[string]string {
	files := make(map[string]string, len(tree))
	for p, f := range tree {
		kind, content := "-", f.Data
		switch {
		case f.IsLink():
			kind, content = "l", []byte(f.Link)
		case f.Executable:
			kind = "x"
		}
		files[p] = fmt.Sprintf("%s %x", kind, sha256.Sum256(content))
	}
	return files
}

func versionOf(files map[string]string) string {
	paths := make([]string, 0, len(files))
	for p := range files {
		paths = append(paths, p)
	}
	sort.Strings(paths)
	h := sha256.New()
	for _, p := range paths {
		exec, sum, _ := strings.Cut(files[p], " ")
		fmt.Fprintf(h, "%s %s %s\n", exec, p, sum)
	}
	return hex.EncodeToString(h.Sum(nil))
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
	Version  string   `toml:"version"`
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

// decode decodes TOML into v and rejects every key v has no field for, naming it. A key nested
// inside a section v holds as data, a map, is not checked. A key is v's only as v's toml tag
// spells it: the TOML decoder also fills a field whose name differs from the key only in case, so
// it would read `[Accuracy]` as [accuracy], and of the two written together whichever it met last.
func decode(file string, data []byte, v any) error {
	meta, err := toml.Decode(string(data), v)
	if err != nil {
		return invalid("%s: %v", file, err)
	}
	for _, k := range meta.Keys() {
		if !declares(reflect.TypeOf(v), k) {
			return invalid("%s: unknown key %q", file, k.String())
		}
	}
	return nil
}

// declares says whether the type t has a field for the key, spelled exactly as the field's toml
// tag spells it. A map holds any key under it.
func declares(t reflect.Type, key toml.Key) bool {
	for _, elem := range key {
		for t.Kind() == reflect.Pointer || t.Kind() == reflect.Slice {
			t = t.Elem()
		}
		switch t.Kind() {
		case reflect.Map:
			return true
		case reflect.Struct:
			f, ok := fieldTagged(t, elem)
			if !ok {
				return false
			}
			t = f.Type
		default:
			return false
		}
	}
	return true
}

// fieldTagged is the field of the struct type t whose toml tag is name, looking into an embedded
// struct without a tag as the decoder does.
func fieldTagged(t reflect.Type, name string) (reflect.StructField, bool) {
	for i := range t.NumField() {
		f := t.Field(i)
		tag, _, _ := strings.Cut(f.Tag.Get("toml"), ",")
		if f.Anonymous && tag == "" && f.Type.Kind() == reflect.Struct {
			if inner, ok := fieldTagged(f.Type, name); ok {
				return inner, true
			}
			continue
		}
		if tag == name {
			return f, true
		}
	}
	return reflect.StructField{}, false
}

// Parse reads a manifest and an objectives file (nil when the task has none) without touching
// the task's directory. Version and HasDockerfile are left for Read, which sees the directory. The
// manifest's version says which schema it is read by: 0.1, the single-image format, or 0.2, the
// format of two images (TaskManifestV02 in task.proto).
func Parse(name string, manifestTOML, objectivesTOML []byte) (Task, error) {
	var table map[string]any
	if _, err := toml.Decode(string(manifestTOML), &table); err != nil {
		return Task{}, invalid("%s: %v", ManifestName, err)
	}
	schema, err := declaredVersion(table)
	if err != nil {
		return Task{}, err
	}
	o, err := ParseObjectives(objectivesTOML)
	if err != nil {
		return Task{}, err
	}
	if schema == Schema02 {
		return parseV02(name, table, o)
	}
	var m manifest
	if err := decode(ManifestName, manifestTOML, &m); err != nil {
		return Task{}, err
	}
	return build(name, m, o)
}

// build reads a manifest of task schema 0.1, the single-image format.
func build(name string, m manifest, o *cliv1.TaskObjectives) (Task, error) {
	if m.Evaluator != nil {
		return Task{}, unsupported("%s: [evaluator] tasks are not supported by this build; their commands are run by an evaluator package", ManifestName)
	}
	if _, modal := m.Environment["modal"]; modal {
		return Task{}, unsupported("%s: [environment.modal] cannot be honored on this machine", ManifestName)
	}
	t := Task{Name: name, Samples: DefaultSamples, Schema: Schema01}
	nested, err := nestedDocker(m.Environment)
	if err != nil {
		return Task{}, err
	}
	t.NestedDocker = nested
	if t.Needs, err = needs(m.Environment); err != nil {
		return Task{}, err
	}
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
	if err := score(&t, bench, m.Metric, o); err != nil {
		return Task{}, err
	}
	return t, nil
}

// score reads what the benchmark reports and what it is judged on, from the manifest's benchmark
// and metric and from the objectives.
func score(t *Task, bench *benchSection, metric string, o *cliv1.TaskObjectives) error {
	axes := axesOf(o)
	if err := resultOf(t, bench, metric, axes); err != nil {
		return err
	}
	if o.GetPareto() != nil && len(axes) > 1 {
		if !t.Result.Protocol2 {
			return invalid("%s: a [pareto] task with %d objectives needs result_protocol = 2 in [benchmark], so every sample reports every objective; [benchmark.result] reads one field", ObjectiveName, len(axes))
		}
		t.Objectives.RelativeNoise = o.GetPareto().GetRelativeNoise()
		for _, a := range axes {
			t.Objectives.Metrics = append(t.Objectives.Metrics, core.Metric{Name: a.name, Direction: a.direction, Unit: a.unit})
			if a.noise != nil {
				t.Objectives.Noise = append(t.Objectives.Noise, core.ObjectiveNoise{Name: a.name, RelativeNoise: *a.noise})
			}
		}
	}
	return nil
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
	if seconds > MaxCommandTimeoutSeconds {
		return Command{}, invalid("%s: [%s] timeout_seconds must be at most %d (one week), got %d", ManifestName, section, MaxCommandTimeoutSeconds, seconds)
	}
	timeout := DefaultCommandTimeout
	if seconds > 0 {
		timeout = time.Duration(seconds) * time.Second
	}
	return Command{Argv: append([]string(nil), argv...), Timeout: timeout}, nil
}

// checkPlaceholders refuses a ${...} other than ${PROJECT_ROOT} and the ones also allowed: it
// would reach the program unreplaced.
func checkPlaceholders(c Command, also ...string) error {
	allowed := append([]string{ProjectRoot}, also...)
	for _, a := range c.Argv {
		rest := a
		for _, p := range allowed {
			rest = strings.ReplaceAll(rest, p, "")
		}
		if i := strings.Index(rest, "${"); i >= 0 {
			end := strings.IndexByte(rest[i:], '}')
			if end < 0 {
				end = len(rest) - i - 1
			}
			verb := "is"
			if len(allowed) > 1 {
				verb = "are"
			}
			return unsupported("%s: the placeholder %s is not supported here; only %s %s", ManifestName, rest[i:i+end+1], strings.Join(allowed, " and "), verb)
		}
	}
	return nil
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
// objectives. metric is the manifest's top-level metric, empty when it names none.
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
		// The product's reader refuses the same disagreement, so a task is never scored on a metric
		// its manifest says it is not.
		if metric != "" && metric != r.Metric {
			return invalid("%s: metric %q disagrees with [benchmark.result] metric %q; declare the scored metric once", ManifestName, metric, r.Metric)
		}
		a, ok := find(axes, r.Metric)
		if !ok {
			return invalid("%s: has no objective for the metric %q; declare it with its direction", ObjectiveName, r.Metric)
		}
		t.Result = Result{JSONArgument: r.JSONArgument, Field: r.Metric}
		t.Metric = core.Metric{Name: r.Metric, Direction: a.direction, Unit: a.unit}
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
		t.Metric = core.Metric{Name: scored, Direction: a.direction, Unit: a.unit}
		for _, other := range axes {
			if other.name != scored {
				t.Others = append(t.Others, core.Metric{Name: other.name, Direction: other.direction, Unit: other.unit})
			}
		}
	case b.ResultProtocol != 0:
		return invalid("%s: result_protocol %d is not supported; the protocol version is 2", ManifestName, b.ResultProtocol)
	default:
		return unsupported("%s: the benchmark declares no result contract; this build measures only a benchmark with [benchmark.result] or result_protocol = 2", ManifestName)
	}
	return nil
}
