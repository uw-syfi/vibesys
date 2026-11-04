package task_test

import (
	"errors"
	"fmt"
	"io/fs"
	"math/rand/v2"
	"os"
	"path"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"testing/fstest"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/task"
)

const jsonManifest = `
version = 1
backends = ["cpu"]

[agent]
domain = "data-structures"

[build]
command = ["make", "-j4"]
timeout_seconds = 600

[accuracy]
command = ["python", "${PROJECT_ROOT}/.vibesys/tasks/t/check.py", "--strict"]
timeout_seconds = 3600

[benchmark]
command = ["python", "${PROJECT_ROOT}/.vibesys/tasks/t/bench.py"]
timeout_seconds = 1800

[benchmark.result]
json_argument = "--output-json"
metric = "ops_per_sec"

[catalogue]
title = "anything"
[catalogue.hardware]
cpu = "any"
`

const protocol2Manifest = `
version = 1

[agent]
domain = "kernels"

[check]
command = ["./check"]

[measure]
command = ["./bench"]
result_protocol = 2
`

const objectives = `
[[objective]]
name = "ops_per_sec"
direction = "max"
`

const objectives2 = `
[[objective]]
name = "latency_ms"
direction = "min"

[[objective]]
name = "throughput"
direction = "max"

[pareto]
relative_noise = 0.02
`

func repo(files map[string]string) fs.FS {
	m := fstest.MapFS{}
	for name, content := range files {
		m[name] = &fstest.MapFile{Data: []byte(content)}
	}
	return m
}

func taskFiles(name, manifest, objectives string) map[string]string {
	files := map[string]string{filepath.ToSlash(filepath.Join(".vibesys/tasks", name, "vibesys.input.toml")): manifest}
	if objectives != "" {
		files[".vibesys/tasks/"+name+"/objectives.toml"] = objectives
	}
	return files
}

func TestReadsAJSONFileTask(t *testing.T) {
	got, err := task.Read(repo(taskFiles("t", jsonManifest, objectives)), "t")
	if err != nil {
		t.Fatal(err)
	}
	if got.Name != "t" || got.Backend != core.BackendCPU || got.Samples != task.DefaultSamples {
		t.Errorf("task %+v", got)
	}
	if got.Build == nil || strings.Join(got.Build.Argv, " ") != "make -j4" || got.Build.Timeout != 600*time.Second {
		t.Errorf("build %+v", got.Build)
	}
	if got.Check.Timeout != 3600*time.Second || got.Benchmark.Timeout != 1800*time.Second {
		t.Errorf("timeouts %v %v", got.Check.Timeout, got.Benchmark.Timeout)
	}
	if got.Result != (task.Result{JSONArgument: "--output-json", Field: "ops_per_sec"}) || got.Result.OutputFlag() != "--output-json" {
		t.Errorf("result %+v", got.Result)
	}
	if got.Metric != (core.Metric{Name: "ops_per_sec", Direction: core.Maximize}) {
		t.Errorf("metric %+v", got.Metric)
	}
	argv := got.Check.Resolve("/work/checkout")
	if argv[1] != "/work/checkout/.vibesys/tasks/t/check.py" {
		t.Errorf("resolved %v", argv)
	}
	if got.Version == "" {
		t.Error("no version")
	}
}

func TestReadsAProtocol2TaskWithShortFormSections(t *testing.T) {
	got, err := task.Read(repo(taskFiles("p", protocol2Manifest, objectives2)), "p")
	if err != nil {
		t.Fatal(err)
	}
	if !got.Result.Protocol2 || got.Result.OutputFlag() != task.OutputFlagProtocol2 {
		t.Errorf("result %+v", got.Result)
	}
	// The first objective is the scored metric; the other is recorded beside it.
	if got.Metric != (core.Metric{Name: "latency_ms", Direction: core.Minimize}) {
		t.Errorf("metric %+v", got.Metric)
	}
	if len(got.Others) != 1 || got.Others[0] != (core.Metric{Name: "throughput", Direction: core.Maximize}) {
		t.Errorf("others %+v", got.Others)
	}
	if got.Check.Timeout != task.DefaultCommandTimeout {
		t.Errorf("default timeout %v", got.Check.Timeout)
	}
}

// A task whose objectives.toml has [pareto] and more than one objective is judged on every
// objective, in the order declared, under the [pareto] noise floor. Any other task, with [pareto]
// and one objective or with several objectives and no [pareto], is judged on its one metric as
// before.
func TestAParetoTaskIsJudgedOnEveryObjective(t *testing.T) {
	got, err := task.Read(repo(taskFiles("p", protocol2Manifest, objectives2)), "p")
	if err != nil {
		t.Fatal(err)
	}
	want := core.Objectives{
		Metrics:       []core.Metric{{Name: "latency_ms", Direction: core.Minimize}, {Name: "throughput", Direction: core.Maximize}},
		RelativeNoise: 0.02,
	}
	if !got.Objectives.Equal(want) || !got.Objectives.Pareto() {
		t.Errorf("objectives %+v, want %+v", got.Objectives, want)
	}

	noPareto := strings.Replace(objectives2, "[pareto]\nrelative_noise = 0.02\n", "", 1)
	onePareto := objectives + "[pareto]\nrelative_noise = 0.02\n"
	for what, files := range map[string]map[string]string{
		"several objectives and no [pareto]": taskFiles("p", protocol2Manifest, noPareto),
		"[pareto] and one objective":         taskFiles("t", jsonManifest, onePareto),
	} {
		name := "p"
		if strings.Contains(what, "one objective") {
			name = "t"
		}
		got, err := task.Read(repo(files), name)
		if err != nil {
			t.Fatalf("%s: %v", what, err)
		}
		if len(got.Objectives.Metrics) != 0 || got.Objectives.RelativeNoise != 0 {
			t.Errorf("%s: objectives %+v, want none", what, got.Objectives)
		}
	}
}

// A Pareto task reads every objective from each sample, which only an evaluator stream reports.
func TestAParetoTaskNeedsAnEvaluatorStream(t *testing.T) {
	jsonObjectives := strings.Replace(objectives2, `name = "latency_ms"`, `name = "ops_per_sec"`, 1)
	_, err := task.Parse("t", []byte(jsonManifest), []byte(jsonObjectives))
	if f := failureOf(t, err); f.Kind != core.FailureInvalidInput || !strings.Contains(f.Message, "result_protocol = 2") {
		t.Fatalf("%v, want invalid input that names result_protocol = 2", err)
	}
}

func TestAMetricKeyChoosesTheScoredObjective(t *testing.T) {
	manifest := protocol2Manifest + "\n"
	manifest = strings.Replace(manifest, "version = 1\n", "version = 1\nmetric = \"throughput\"\n", 1)
	got, err := task.Read(repo(taskFiles("p", manifest, objectives2)), "p")
	if err != nil {
		t.Fatal(err)
	}
	if got.Metric.Name != "throughput" || len(got.Others) != 1 || got.Others[0].Name != "latency_ms" {
		t.Errorf("metric %+v others %+v", got.Metric, got.Others)
	}
}

func TestADockerfileIsNoted(t *testing.T) {
	files := taskFiles("t", jsonManifest, objectives)
	files[".vibesys/tasks/t/Dockerfile"] = "FROM scratch"
	got, err := task.Read(repo(files), "t")
	if err != nil || !got.HasDockerfile || !got.Setup().HasDockerfile {
		t.Fatalf("got %+v, %v", got, err)
	}
	if string(got.Dockerfile) != "FROM scratch" {
		t.Errorf("the task carries the Dockerfile %q, want its text", got.Dockerfile)
	}
}

func TestATaskWithoutADockerfileCarriesNone(t *testing.T) {
	got, err := task.Read(repo(taskFiles("t", jsonManifest, objectives)), "t")
	if err != nil || got.HasDockerfile || len(got.Dockerfile) != 0 {
		t.Fatalf("got %+v, %v", got, err)
	}
}

func TestADockerfileThatCannotBeReadIsNotTakenForAbsent(t *testing.T) {
	files := taskFiles("t", jsonManifest, objectives)
	files[".vibesys/tasks/t/Dockerfile/inner"] = "x"
	_, err := task.Read(repo(files), "t")
	var f *core.Failure
	if !errors.As(err, &f) || f.Kind != core.FailureUnavailable || !strings.Contains(f.Message, "Dockerfile") {
		t.Fatalf("err = %v, want an unavailable failure naming the Dockerfile", err)
	}
}

func TestDockerInDockerIsNotedAndMustBeABoolean(t *testing.T) {
	read := func(section string) (task.Task, error) {
		return task.Parse("t", []byte(strings.Replace(jsonManifest, "[catalogue]", section+"[catalogue]", 1)), []byte(objectives))
	}
	for section, want := range map[string]bool{
		"": false,
		"[environment]\ndocker_in_docker = true\n":  true,
		"[environment]\ndocker_in_docker = false\n": false,
		"[environment]\nsomething_else = 1\n":       false,
	} {
		got, err := read(section)
		if err != nil || got.NestedDocker != want {
			t.Errorf("%q: NestedDocker = %v, %v; want %v", section, got.NestedDocker, err, want)
		}
	}
	_, err := read("[environment]\ndocker_in_docker = \"yes\"\n")
	if f := failureOf(t, err); f.Kind != core.FailureInvalidInput || !strings.Contains(f.Message, "docker_in_docker") {
		t.Errorf("a string value: %v, want invalid input naming docker_in_docker", err)
	}
}

func failureOf(t *testing.T, err error) *core.Failure {
	t.Helper()
	var f *core.Failure
	if !errors.As(err, &f) {
		t.Fatalf("error %v is not a *core.Failure", err)
	}
	return f
}

func TestWhatItCannotRunIsRefusedAndSaysWhy(t *testing.T) {
	cases := map[string]struct {
		manifest string
		want     string
		kind     core.FailureKind
	}{
		"an evaluator": {
			strings.Replace(jsonManifest, "[catalogue]", "[evaluator]\nname = \"x\"\nversion = \"1\"\n[catalogue]", 1),
			"[evaluator]", core.FailureUnavailable,
		},
		"an entrypoint check": {
			strings.Replace(jsonManifest, `command = ["python", "${PROJECT_ROOT}/.vibesys/tasks/t/check.py", "--strict"]`, `entrypoint = "checker"`, 1),
			"entrypoint", core.FailureUnavailable,
		},
		"a modal environment": {
			strings.Replace(jsonManifest, "[catalogue]", "[environment.modal]\nentrypoint = \"x\"\n[catalogue]", 1),
			"modal", core.FailureUnavailable,
		},
		"a placeholder it cannot fill": {
			strings.Replace(jsonManifest, "${PROJECT_ROOT}/.vibesys/tasks/t/bench.py", "${PACKAGE_ROOT}/bench.py", 1),
			"${PACKAGE_ROOT}", core.FailureUnavailable,
		},
		"a benchmark that needs no check": {
			strings.Replace(jsonManifest, "[benchmark.result]", "requires_accuracy = false\n[benchmark.result]", 1),
			"requires_accuracy", core.FailureUnavailable,
		},
		"two backends": {
			strings.Replace(jsonManifest, `["cpu"]`, `["cpu", "cuda"]`, 1),
			"backends", core.FailureUnavailable,
		},
		"a version it does not know": {
			strings.Replace(jsonManifest, "version = 1", "version = 2", 1),
			"version", core.FailureInvalidInput,
		},
		"no result": {
			strings.Replace(jsonManifest, "[benchmark.result]\njson_argument = \"--output-json\"\nmetric = \"ops_per_sec\"\n", "", 1),
			"declares no result contract", core.FailureUnavailable,
		},
		"a protocol version it does not know": {
			strings.Replace(protocol2Manifest, "result_protocol = 2", "result_protocol = 3", 1),
			"result_protocol 3", core.FailureInvalidInput,
		},
		"both aliases": {
			strings.Replace(jsonManifest, "[accuracy]", "[check]\ncommand = [\"x\"]\n[accuracy]", 1),
			"not both", core.FailureInvalidInput,
		},
		"an empty command": {
			strings.Replace(jsonManifest, `command = ["make", "-j4"]`, `command = []`, 1),
			"[build]", core.FailureInvalidInput,
		},
		"an empty element": {
			strings.Replace(jsonManifest, `"-j4"`, `""`, 1),
			"empty", core.FailureInvalidInput,
		},
		"an absolute build program": {
			strings.Replace(jsonManifest, `"make"`, `"/usr/bin/make"`, 1),
			"project-relative", core.FailureInvalidInput,
		},
		"a negative timeout": {
			strings.Replace(jsonManifest, "timeout_seconds = 600", "timeout_seconds = -5", 1),
			"timeout_seconds", core.FailureInvalidInput,
		},
		"an option that is not one": {
			strings.Replace(jsonManifest, `json_argument = "--output-json"`, `json_argument = "output"`, 1),
			"json_argument", core.FailureInvalidInput,
		},
	}
	for name, c := range cases {
		_, err := task.Read(repo(taskFiles("t", c.manifest, objectives)), "t")
		if err == nil {
			t.Errorf("%s: accepted", name)
			continue
		}
		f := failureOf(t, err)
		if f.Kind != c.kind || !strings.Contains(f.Message, c.want) {
			t.Errorf("%s: %q (%s), want %s naming %q", name, f.Message, f.Kind, c.kind, c.want)
		}
	}
}

func TestTheObjectivesMustDeclareTheMetric(t *testing.T) {
	for name, text := range map[string]string{
		"none":          "",
		"another":       "[[objective]]\nname = \"other\"\ndirection = \"max\"\n",
		"a bad way":     "[[objective]]\nname = \"ops_per_sec\"\ndirection = \"up\"\n",
		"twice":         objectives + objectives,
		"bad noise":     objectives + "[pareto]\nrelative_noise = 1.5\n",
		"unknown key":   objectives + "[pareto]\nrelative_noise = 0.1\nextra = 1\n",
		"unknown field": "[[objective]]\nname = \"ops_per_sec\"\ndirection = \"max\"\nunit = \"s\"\n",
	} {
		_, err := task.Read(repo(taskFiles("t", jsonManifest, text)), "t")
		if err == nil {
			t.Errorf("%s: accepted", name)
		}
	}
}

// Whatever key a task puts where this reader reads keys, the error names it. The key is added at
// each place a key can be: the top level and every section the reader interprets.
func TestUnknownKeysAreRejectedByName(t *testing.T) {
	r := rand.New(rand.NewPCG(1, 2))
	sections := []string{"", "[build]", "[accuracy]", "[benchmark]", "[benchmark.result]"}
	for i := 0; i < 200; i++ {
		key := fmt.Sprintf("zz_%c%c%d", 'a'+rune(r.IntN(26)), 'a'+rune(r.IntN(26)), r.IntN(1000))
		section := sections[r.IntN(len(sections))]
		var manifest string
		if section == "" {
			manifest = strings.Replace(jsonManifest, "version = 1\n", "version = 1\n"+key+" = 1\n", 1)
		} else {
			manifest = strings.Replace(jsonManifest, section+"\n", section+"\n"+key+" = 1\n", 1)
		}
		_, err := task.Read(repo(taskFiles("t", manifest, objectives)), "t")
		if err == nil {
			t.Fatalf("a manifest with the unknown key %q in %q was accepted", key, section)
		}
		if f := failureOf(t, err); f.Kind != core.FailureInvalidInput || !strings.Contains(f.Message, key) {
			t.Fatalf("key %q in %q: error %q does not name it", key, section, f.Message)
		}
	}
}

func TestMissingFilesAreNotFound(t *testing.T) {
	for name, files := range map[string]map[string]string{
		"no task":     {},
		"no manifest": {".vibesys/tasks/t/objectives.toml": objectives},
	} {
		_, err := task.Read(repo(files), "t")
		if f := failureOf(t, err); f.Kind != core.FailureNotFound {
			t.Errorf("%s: %v", name, f)
		}
	}
	for _, bad := range []string{"", ".", "..", "a/b", `a\b`} {
		if _, err := task.Read(repo(nil), bad); err == nil {
			t.Errorf("task name %q accepted", bad)
		}
	}
}

// The version names the task's files and nothing else.
func TestVersionNamesExactlyTheTaskFiles(t *testing.T) {
	base := taskFiles("t", jsonManifest, objectives)
	base[".vibesys/tasks/t/check.py"] = "print(1)"
	base[".vibesys/tasks/t/data/input.txt"] = "1 2 3"
	version := func(files map[string]string, modes map[string]fs.FileMode) string {
		m := fstest.MapFS{}
		for n, c := range files {
			m[n] = &fstest.MapFile{Data: []byte(c), Mode: modes[n]}
		}
		v, err := task.Version(m, ".vibesys/tasks/t")
		if err != nil {
			t.Fatal(err)
		}
		return v
	}
	want := version(base, nil)

	other := map[string]string{"README.md": "elsewhere", ".vibesys/tasks/u/vibesys.input.toml": "x"}
	for k, v := range base {
		other[k] = v
	}
	if got := version(other, nil); got != want {
		t.Error("files outside the task changed its version")
	}
	for name, change := range map[string]func(map[string]string){
		"a changed file": func(f map[string]string) { f[".vibesys/tasks/t/check.py"] = "print(2)" },
		"a new file":     func(f map[string]string) { f[".vibesys/tasks/t/new.txt"] = "" },
		"a removed file": func(f map[string]string) { delete(f, ".vibesys/tasks/t/data/input.txt") },
		"a renamed file": func(f map[string]string) {
			f[".vibesys/tasks/t/data/other.txt"] = f[".vibesys/tasks/t/data/input.txt"]
			delete(f, ".vibesys/tasks/t/data/input.txt")
		},
	} {
		changed := map[string]string{}
		for k, v := range base {
			changed[k] = v
		}
		change(changed)
		if version(changed, nil) == want {
			t.Errorf("%s did not change the version", name)
		}
	}
	if version(base, map[string]fs.FileMode{".vibesys/tasks/t/check.py": 0o755}) == want {
		t.Error("the executable bit did not change the version")
	}
	if version(base, map[string]fs.FileMode{".vibesys/tasks/t/check.py": 0o644}) != want {
		t.Error("a mode without the executable bit changed the version")
	}
}

// Every task in the repository's examples that uses a result format this build reads is read
// without edits, and every other is refused as unsupported and never as invalid.
func TestTheRepositorysExamplesAreReadOrRefusedAsUnsupported(t *testing.T) {
	root, err := filepath.Abs("../../../examples")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(root); err != nil {
		t.Skip("the examples are not in this checkout")
	}
	var read, refused int
	err = filepath.WalkDir(root, func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			return nil
		}
		if d.IsDir() && (d.Name() == "node_modules" || d.Name() == "repositories") {
			return filepath.SkipDir
		}
		if d.IsDir() || d.Name() != task.ManifestName {
			return nil
		}
		dir := filepath.Dir(p)
		name := filepath.Base(dir)
		project := filepath.Dir(filepath.Dir(filepath.Dir(dir)))
		_, err = task.Read(os.DirFS(project), name)
		if err == nil {
			read++
			return nil
		}
		if f := failureOf(t, err); f.Kind == core.FailureUnavailable {
			refused++
		} else {
			t.Errorf("%s: %v", p, err)
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	t.Logf("%d example tasks read, %d refused as unsupported", read, refused)
}

func FuzzParse(f *testing.F) {
	f.Add([]byte(jsonManifest), []byte(objectives))
	f.Add([]byte(protocol2Manifest), []byte(objectives2))
	f.Add([]byte(""), []byte(""))
	f.Add([]byte("version = 1\n[check]\ncommand=['a']\n[measure]\ncommand=['b']\nresult_protocol=2\n"), []byte("[[objective]]\nname='m'\ndirection='min'\n"))
	f.Fuzz(func(t *testing.T, manifest, objectives []byte) {
		got, err := task.Parse("t", manifest, objectives)
		if err != nil {
			var failure *core.Failure
			if !errors.As(err, &failure) || failure.Message == "" {
				t.Fatalf("error %v is not a *core.Failure with a message", err)
			}
			return
		}
		// What is accepted can be run: commands have programs, limits are positive, the metric has
		// a direction, the result has one way to be read, and no placeholder is left unfilled.
		commands := []task.Command{got.Check, got.Benchmark}
		if got.Build != nil {
			commands = append(commands, *got.Build)
		}
		for _, c := range commands {
			if len(c.Argv) == 0 || c.Argv[0] == "" || c.Timeout <= 0 {
				t.Fatalf("accepted a command that cannot run: %+v", c)
			}
			for _, a := range c.Resolve("/root") {
				if strings.Contains(a, "${") {
					t.Fatalf("an argument keeps a placeholder after resolving: %q", a)
				}
			}
		}
		if got.Metric.Name == "" || (got.Metric.Direction != core.Maximize && got.Metric.Direction != core.Minimize) {
			t.Fatalf("accepted a metric without a direction: %+v", got.Metric)
		}
		if got.Result.Protocol2 == (got.Result.JSONArgument != "") || got.Result.OutputFlag() == "" {
			t.Fatalf("accepted a result that cannot be read: %+v", got.Result)
		}
		if got.Samples == 0 || !got.Backend.Known() {
			t.Fatalf("accepted %+v", got)
		}
		if o := got.Objectives; (len(o.Metrics) > 0 || o.RelativeNoise != 0) &&
			(!o.Pareto() || !got.Result.Protocol2 || !slices.Contains(o.Metrics, got.Metric)) {
			t.Fatalf("accepted objectives a measure cannot follow: %+v for %+v", o, got.Result)
		}
	})
}

// Generated files never change a task's version, whatever they hold, wherever under the task they
// are, and whether they are added, rewritten or removed; any change to another file does. And
// ChangesFrom is empty exactly when the versions are equal, and otherwise names every file that
// differs.
func TestGeneratedFilesNeverChangeTheVersionAndOtherFilesAlwaysDo(t *testing.T) {
	rng := rand.New(rand.NewPCG(13, 17))
	segments := []string{"bench", "checker", "data", "lib", "a", "b"}
	dirOf := func(generated bool) string {
		parts := []string{}
		for range rng.IntN(3) {
			parts = append(parts, segments[rng.IntN(len(segments))])
		}
		if generated {
			parts = append(parts[:rng.IntN(len(parts)+1)], append([]string{[]string{"__pycache__", ".pytest_cache"}[rng.IntN(2)]}, parts...)...)
		}
		return strings.Join(parts, "/")
	}
	generatedPath := func() string {
		name := fmt.Sprintf("f%d", rng.IntN(5))
		switch rng.IntN(3) {
		case 0: // a bytecode file anywhere
			return path.Join(dirOf(false), name+[]string{".pyc", ".pyo"}[rng.IntN(2)])
		default: // anything under a cache directory
			return path.Join(dirOf(true), name+[]string{".pyc", ".txt", ""}[rng.IntN(3)])
		}
	}
	ordinaryPath := func() string {
		return path.Join(dirOf(false), fmt.Sprintf("g%d", rng.IntN(5))+[]string{".py", ".toml", ".json", ""}[rng.IntN(4)])
	}
	read := func(files map[string]string) task.Task {
		tk, err := task.Read(repo(files), "t")
		if err != nil {
			t.Fatal(err)
		}
		return tk
	}
	const dir = ".vibesys/tasks/t/"
	for i := range 200 {
		base := taskFiles("t", jsonManifest, objectives)
		for range rng.IntN(6) {
			base[dir+ordinaryPath()] = fmt.Sprint(rng.Uint32())
		}
		for range rng.IntN(4) {
			base[dir+generatedPath()] = fmt.Sprint(rng.Uint32())
		}
		clone := func() map[string]string {
			out := map[string]string{}
			for k, v := range base {
				out[k] = v
			}
			return out
		}
		want := read(base)

		// Generated files added, rewritten and removed.
		churned := clone()
		for range 1 + rng.IntN(4) {
			p := dir + generatedPath()
			if _, ok := churned[p]; ok && rng.IntN(2) == 0 {
				delete(churned, p)
			} else {
				churned[p] = fmt.Sprint(rng.Uint32(), "churn")
			}
		}
		got := read(churned)
		if got.Version != want.Version {
			t.Fatalf("example %d: generated files changed the version", i)
		}
		if changes := got.ChangesFrom(want); len(changes) != 0 {
			t.Fatalf("example %d: generated files are changes: %v", i, changes)
		}

		// An ordinary file added, rewritten or removed.
		edited := clone()
		p := dir + ordinaryPath()
		var kind string
		if old, ok := edited[p]; ok && rng.IntN(2) == 0 {
			delete(edited, p)
			kind = "removed"
		} else if ok {
			edited[p] = old + "!"
			kind = "changed"
		} else {
			edited[p] = "new"
			kind = "added"
		}
		got = read(edited)
		if got.Version == want.Version {
			t.Fatalf("example %d: %s %s did not change the version", i, kind, p)
		}
		changes := got.ChangesFrom(want)
		if len(changes) != 1 || changes[0] != (task.Change{Path: p, Kind: kind}) {
			t.Fatalf("example %d: changes = %v, want %s (%s)", i, changes, p, kind)
		}
	}
}

func TestGeneratedNamesWhatATaskDirectoryHoldsButATaskDoesNotUse(t *testing.T) {
	for p, want := range map[string]bool{
		"__pycache__/x.cpython-312.pyc": true,
		"bench/__pycache__/x.pyc":       true,
		"bench/__pycache__/notes.txt":   true,
		"a/b/mod.pyc":                   true,
		"mod.pyo":                       true,
		".pytest_cache/v/cache":         true,
		"__pycache__":                   true,
		"checker.py":                    false,
		"bench/benchmark.py":            false,
		"pycache/x.txt":                 false,
		"data/file.pyc.txt":             false,
		"my__pycache__/x.txt":           false,
	} {
		if got := task.Generated(p); got != want {
			t.Errorf("Generated(%q) = %v, want %v", p, got, want)
		}
	}
}

// Any list of distinct resource kinds, in any order, is read as written; a list that names a kind
// twice, or a name that is not a kind, is refused, naming the key, and never read as fewer needs.
func TestNeedsAreReadAsWrittenAndRefusedWhenNotKinds(t *testing.T) {
	read := func(section string) (task.Task, error) {
		return task.Parse("t", []byte(strings.Replace(jsonManifest, "[catalogue]", section+"[catalogue]", 1)), []byte(objectives))
	}
	quote := func(names []string) string {
		q := make([]string, len(names))
		for i, n := range names {
			q[i] = fmt.Sprintf("%q", n)
		}
		return "[environment]\nneeds = [" + strings.Join(q, ", ") + "]\n"
	}
	r := rand.New(rand.NewPCG(1, 2))
	for range 200 {
		kinds := core.ResourceKinds()
		r.Shuffle(len(kinds), func(i, j int) { kinds[i], kinds[j] = kinds[j], kinds[i] })
		kinds = kinds[:r.IntN(len(kinds)+1)]
		names := make([]string, len(kinds))
		for i, k := range kinds {
			names[i] = string(k)
		}
		got, err := read(quote(names))
		if err != nil || !slices.Equal(got.Needs, kinds) {
			t.Fatalf("%v: Needs = %v, %v", names, got.Needs, err)
		}
		if len(names) > 0 {
			twice := append(slices.Clone(names), names[r.IntN(len(names))])
			if _, err := read(quote(twice)); failureOf(t, err).Kind != core.FailureInvalidInput || !strings.Contains(err.Error(), "needs") {
				t.Fatalf("%v: %v, want a refusal naming needs", twice, err)
			}
		}
		bad := append(slices.Clone(names), []string{"GPU", "k8s", "", "gpus"}[r.IntN(4)])
		if _, err := read(quote(bad)); failureOf(t, err).Kind != core.FailureInvalidInput || !strings.Contains(err.Error(), "needs[") {
			t.Fatalf("%v: %v, want a refusal naming the entry", bad, err)
		}
	}
	for _, section := range []string{"[environment]\nneeds = \"gpu\"\n", "[environment]\nneeds = [1]\n"} {
		if _, err := read(section); failureOf(t, err).Kind != core.FailureInvalidInput {
			t.Errorf("%q: %v, want invalid input", section, err)
		}
	}
	if got, err := read(""); err != nil || got.Needs != nil {
		t.Errorf("no needs: %v, %v", got.Needs, err)
	}
}
