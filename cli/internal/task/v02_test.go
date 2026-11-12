package task_test

import (
	"fmt"
	"math/rand/v2"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/task"
)

// v02Manifest is a task of two images, in task schema 0.2.
const v02Manifest = `
version = "0.2"
backends = ["cuda"]
metric = "tokens_per_second"

[candidate]
dockerfile = "serve/Dockerfile"
context = "serve"
serve = ["python", "${PROJECT_ROOT}/serve/server.py", "--port", "${PORT}"]
ready = "/health"
startup_timeout_seconds = 600
allowed_bases = ["vllm/vllm-openai:v0.6.3"]

[accuracy]
command = ["python", "${PROJECT_ROOT}/.vibesys/tasks/t/check.py"]
timeout_seconds = 1800

[benchmark]
command = ["python", "/opt/bench.py"]
timeout_seconds = 3600
result_protocol = 2

[profile]
command = ["python", "/opt/load.py", "--short"]
timeout_seconds = 900

[environment]
needs = ["gpu"]
`

const v02Objectives = `
[[objective]]
name = "tokens_per_second"
direction = "max"
`

func v02Files(manifest string) map[string]string {
	files := taskFiles("t", manifest, v02Objectives)
	files[".vibesys/tasks/t/Dockerfile"] = "FROM python:3.12\n"
	return files
}

func TestReadsATaskOfTwoImages(t *testing.T) {
	got, err := task.Read(repo(v02Files(v02Manifest)), "t")
	if err != nil {
		t.Fatal(err)
	}
	if got.Schema != task.Schema02 || got.Schema.String() != "0.2" {
		t.Fatalf("schema %s", got.Schema)
	}
	c := got.Candidate
	if c == nil {
		t.Fatal("no candidate")
	}
	want := core.CandidateBuild{Dockerfile: "serve/Dockerfile", Context: "serve", AllowedBases: []string{"vllm/vllm-openai:v0.6.3"}}
	if fmt.Sprint(c.Build) != fmt.Sprint(want) || c.Ready != "/health" || c.StartupTimeout != 10*time.Minute {
		t.Fatalf("candidate %+v", c)
	}
	serve := c.ServeAt("/work", []string{"nsys", "profile"})
	if got := strings.Join(serve.Argv, " "); got != "nsys profile python /work/serve/server.py --port ${PORT}" {
		t.Fatalf("serve %q", got)
	}
	if got.Benchmark.Timeout != time.Hour {
		t.Fatalf("benchmark %+v", got.Benchmark)
	}
	if !got.Setup().TwoImages || len(got.Needs) != 1 || got.Needs[0] != core.ResourceGPU {
		t.Fatalf("setup %+v, needs %v", got.Setup(), got.Needs)
	}
}

// withoutCandidate is v02Manifest with no [candidate] section.
func withoutCandidate() string {
	start := strings.Index(v02Manifest, "[candidate]")
	end := strings.Index(v02Manifest, "[accuracy]")
	return v02Manifest[:start] + v02Manifest[end:]
}

// [candidate] is optional: without it a 0.2 task is a task of one image, whose commands run in the
// task image, and it reads the same as with it in every other key.
func TestSchema02ReadsWithoutACandidate(t *testing.T) {
	manifest := withoutCandidate()
	if strings.Contains(manifest, "[candidate]") {
		t.Fatal("the manifest still has a candidate")
	}
	with, err := task.Read(repo(v02Files(v02Manifest)), "t")
	if err != nil {
		t.Fatal(err)
	}
	got, err := task.Read(repo(v02Files(manifest)), "t")
	if err != nil {
		t.Fatal(err)
	}
	if got.Schema != task.Schema02 || got.Candidate != nil || got.Setup().TwoImages {
		t.Fatalf("schema %s, candidate %+v, setup %+v", got.Schema, got.Candidate, got.Setup())
	}
	if got.Benchmark.Timeout != time.Hour {
		t.Fatalf("benchmark %+v", got.Benchmark)
	}
	if !reflect.DeepEqual(got.Check, with.Check) || !reflect.DeepEqual(got.Benchmark, with.Benchmark) || got.Metric != with.Metric {
		t.Fatalf("without a candidate the commands read differently:\n%+v\n%+v", got, with)
	}
}

// A task of one image needs no Dockerfile of its own, as a 0.1 task needs none; a task with a
// [candidate] does.
func TestASchema02TaskWithoutACandidateNeedsNoDockerfile(t *testing.T) {
	files := v02Files(withoutCandidate())
	delete(files, ".vibesys/tasks/t/Dockerfile")
	got, err := task.Read(repo(files), "t")
	if err != nil {
		t.Fatal(err)
	}
	if got.HasDockerfile || got.Candidate != nil {
		t.Fatalf("read %+v", got)
	}
}

func TestATaskOfTwoImagesNeedsItsOwnDockerfile(t *testing.T) {
	files := v02Files(v02Manifest)
	delete(files, ".vibesys/tasks/t/Dockerfile")
	_, err := task.Read(repo(files), "t")
	if f := failureOf(t, err); f.Kind != core.FailureInvalidInput || !strings.Contains(f.Message, "Dockerfile") {
		t.Fatalf("got %+v", f)
	}
}

// What a 0.2 manifest may not say is refused, naming the key or the rule it breaks.
func TestWhatSchema02RefusesIsNamed(t *testing.T) {
	cases := []struct {
		name, from, to, says string
	}{
		{"an unknown top-level key", `metric = "tokens_per_second"`, "metric = \"tokens_per_second\"\nflavour = 1", `"flavour"`},
		{"an unknown candidate key", `ready = "/health"`, "ready = \"/health\"\nport = 8000", `"candidate.port"`},
		{"an unknown section", `[environment]`, "[flavour]\nx = 1\n[environment]", `"flavour"`},
		{"an unknown agent key", `[environment]`, "[agent]\ndomain = \"x\"\nmodel = \"y\"\n[environment]", `"agent.model"`},
		{"an agent of no domain", `[environment]`, "[agent]\ninterface = \"service\"\n[environment]", "domain"},
		{"an unknown interface", `[environment]`, "[agent]\ndomain = \"x\"\ninterface = \"rpc\"\n[environment]", "interface"},
		{"args", `timeout_seconds = 1800`, "timeout_seconds = 1800\nargs = [\"x\"]", "[accuracy] args"},
		{"check beside accuracy", `[environment]`, "[check]\ncommand = [\"x\"]\n[environment]", "not both"},
		{"measure beside benchmark", `[environment]`, "[measure]\ncommand = [\"x\"]\n[environment]", "not both"},
		{"no accuracy", `[accuracy]`, `[accuracy_]`, "accuracy"},
		{"a modal environment", `needs = ["gpu"]`, "needs = [\"gpu\"]\n[environment.modal]\nentrypoint = \"m.py\"", "modal"},
		{"a benchmark that needs no check", `result_protocol = 2`, "result_protocol = 2\nrequires_accuracy = false", "requires_accuracy"},
		{"an absolute build", `[environment]`, "[build]\ncommand = [\"/bin/make\"]\n[environment]", "[build]"},
		{"docker_in_docker that is not a boolean", `needs = ["gpu"]`, "needs = [\"gpu\"]\ndocker_in_docker = \"yes\"", "docker_in_docker"},
		{"the evaluator section", `[environment]`, "[evaluator]\nimage = \"x\"\n[environment]", "[evaluator]"},
		{"an entrypoint", `timeout_seconds = 1800`, "timeout_seconds = 1800\nentrypoint = \"x\"", "[accuracy] entrypoint"},
		{"no candidate", `[candidate]`, `[candidate_]`, "candidate"},
		{"no dockerfile", `dockerfile = "serve/Dockerfile"`, ``, "dockerfile"},
		{"no serve", `serve = ["python", "${PROJECT_ROOT}/serve/server.py", "--port", "${PORT}"]`, ``, "serve"},
		{"a ready that is not a path", `ready = "/health"`, `ready = "health"`, "ready"},
		{"no startup timeout", `startup_timeout_seconds = 600`, ``, "startup_timeout_seconds"},
		{"a zero startup timeout", `startup_timeout_seconds = 600`, `startup_timeout_seconds = 0`, "startup_timeout_seconds"},
		{"an absolute dockerfile", `dockerfile = "serve/Dockerfile"`, `dockerfile = "/serve/Dockerfile"`, "candidate.dockerfile"},
		{"a context outside the checkout", `context = "serve"`, `context = "../serve"`, "candidate.context"},
		{"an empty allowed base", `allowed_bases = ["vllm/vllm-openai:v0.6.3"]`, `allowed_bases = [""]`, "allowed_bases"},
		{"an unknown placeholder", `"--port", "${PORT}"]`, `"--port", "${HOST}"]`, "${HOST}"},
		{"a port in the task's command", `"/opt/bench.py"]`, `"/opt/bench.py", "${PORT}"]`, "${PORT}"},
		{"two backends", `backends = ["cuda"]`, `backends = ["cuda", "cpu"]`, "backends"},
		{"an unknown need", `needs = ["gpu"]`, `needs = ["fpga"]`, "environment.needs"},
		{"a wrong type", `startup_timeout_seconds = 600`, `startup_timeout_seconds = "600"`, "startup_timeout_seconds"},
		{"a result protocol other than 2", `result_protocol = 2`, `result_protocol = 1`, "result_protocol"},
		{"an unknown key of the retired profile", `timeout_seconds = 900`, "timeout_seconds = 900\nwrap = [\"x\"]", `"profile.wrap"`},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if !strings.Contains(v02Manifest, c.from) {
				t.Fatalf("the case does not apply: %q", c.from)
			}
			manifest := strings.Replace(v02Manifest, c.from, c.to, 1)
			_, err := task.Parse("t", []byte(manifest), []byte(v02Objectives))
			f := failureOf(t, err)
			if !strings.Contains(f.Message, c.says) {
				t.Fatalf("got %+v, want it to name %q", f, c.says)
			}
		})
	}
}

// [profile] is retired with the profile job: a manifest that has it still reads, as the same task
// as one without it, so a task written for it keeps loading.
func TestSchema02AcceptsTheRetiredProfileAndDoesNotReadIt(t *testing.T) {
	start, end := strings.Index(v02Manifest, "[profile]"), strings.Index(v02Manifest, "[environment]")
	without := v02Manifest[:start] + v02Manifest[end:]
	with, err := task.Read(repo(v02Files(v02Manifest)), "t")
	if err != nil {
		t.Fatal(err)
	}
	got, err := task.Read(repo(v02Files(without)), "t")
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(got.Check, with.Check) || !reflect.DeepEqual(got.Benchmark, with.Benchmark) || !reflect.DeepEqual(got.Candidate, with.Candidate) || !reflect.DeepEqual(got.Needs, with.Needs) {
		t.Fatalf("[profile] changed what the task reads as:\n%+v\n%+v", got, with)
	}
}

// Schema 0.2 keeps every key 0.1 reads, with its meaning: a 0.1 manifest that declares 0.2 and
// adds a candidate reads as the same task, with the candidate.
func TestSchema02KeepsEveryKeyOf01(t *testing.T) {
	withCandidate := func(m string) string {
		return strings.Replace(m, `version = "0.1"`, `version = "0.2"`, 1) + `
[candidate]
dockerfile = "Dockerfile"
context = "."
serve = ["./serve", "${PORT}"]
ready = "/"
startup_timeout_seconds = 30
`
	}
	extras := `
[workspace]
unread = ["data"]
[plan]
anything = 1
[profile_guided]
command = ["x"]
[resources]
gpus = 1
[[comparison]]
name = "baseline"
when = 2026-01-01
[environment]
needs = ["kubernetes"]
docker_in_docker = true
`
	for name, c := range map[string]struct{ manifest, objectives string }{
		"json result":         {jsonManifest + extras, objectives},
		"aliases, protocol 2": {protocol2Manifest, objectives},
	} {
		t.Run(name, func(t *testing.T) {
			old, err := task.Parse("t", []byte(c.manifest), []byte(c.objectives))
			if err != nil {
				t.Fatal(err)
			}
			got, err := task.Parse("t", []byte(withCandidate(c.manifest)), []byte(c.objectives))
			if err != nil {
				t.Fatal(err)
			}
			if got.Candidate == nil || got.Schema != task.Schema02 {
				t.Fatalf("read %+v", got)
			}
			got.Candidate, got.Schema = nil, old.Schema
			if !reflect.DeepEqual(got, old) {
				t.Fatalf("0.2 reads\n%+v\n0.1 reads\n%+v", got, old)
			}
		})
	}
}

func versioned(version string) []byte {
	return []byte(strings.Replace(v02Manifest, `version = "0.2"`, version, 1))
}

func TestTheSchemaVersionIsDeclaredExactly(t *testing.T) {
	cases := []struct {
		version string
		// says is in the refusal; empty means the version is read.
		says string
	}{
		{`version = "0.2"`, ""},
		{`version = 1`, `write version = "0.1"`},
		{`version = 2`, "MAJOR.MINOR"},
		{`version = "0.3"`, "vibesys-cli upgrade"},
		{`version = "0.10"`, "vibesys-cli upgrade"},
		{`version = "1.0"`, "vibesys-cli upgrade"},
		{`version = "3.0"`, "vibesys-cli upgrade"},
		{`version = "0.0"`, "not one this vibesys-cli reads"},
		{`version = "2"`, "MAJOR.MINOR"},
		{`version = "2.x"`, "MAJOR.MINOR"},
		{`version = "02.0"`, "MAJOR.MINOR"},
		{`version = "0.02"`, "MAJOR.MINOR"},
		{`version = " 0.2"`, "MAJOR.MINOR"},
		{`version = "0.2.0"`, "MAJOR.MINOR"},
		{`version = 0.2`, "MAJOR.MINOR"},
		{`version = true`, "MAJOR.MINOR"},
		{``, "no version"},
	}
	for _, c := range cases {
		t.Run(c.version, func(t *testing.T) {
			_, err := task.Parse("t", versioned(c.version), []byte(v02Objectives))
			if c.says == "" {
				if err != nil {
					t.Fatal(err)
				}
				return
			}
			f := failureOf(t, err)
			if f.Kind != core.FailureInvalidInput || !strings.Contains(f.Message, c.says) {
				t.Fatalf("got %+v, want it to say %q", f, c.says)
			}
		})
	}
}

// A 0.1 manifest is read with the 0.1 rules: the two-image keys are not part of it.
func TestSchema01ReadsNoCandidate(t *testing.T) {
	got, err := task.Parse("t", []byte(protocol2Manifest), nil)
	if err == nil && (got.Candidate != nil || got.Schema != task.Schema01) {
		t.Fatalf("read %+v", got)
	}
	manifest := protocol2Manifest + "\n[candidate]\ndockerfile = \"Dockerfile\"\n"
	if _, err := task.Parse("t", []byte(manifest), nil); err == nil || !strings.Contains(err.Error(), "candidate") {
		t.Fatalf("got %v", err)
	}
}

// Every MAJOR.MINOR is read only when it is a schema this build reads; any other is refused
// naming it, and one past them all says to upgrade.
func TestEverySchemaVersionIsReadOrRefusedNamingIt(t *testing.T) {
	rng := rand.New(rand.NewPCG(7, 7))
	for range 500 {
		v := task.SchemaVersion{Major: uint32(rng.IntN(4)), Minor: uint32(rng.IntN(12))}
		_, err := task.Parse("t", versioned(fmt.Sprintf("version = %q", v)), []byte(v02Objectives))
		switch {
		case v == task.Schema02:
			if err != nil {
				t.Fatalf("%s: %v", v, err)
			}
		case v == task.Schema01:
			// A 0.2 body under 0.1 is refused by the 0.1 rules, not for its version.
			if err == nil || strings.Contains(err.Error(), "vibesys-cli upgrade") {
				t.Fatalf("%s: %v", v, err)
			}
		default:
			if err == nil || !strings.Contains(err.Error(), fmt.Sprintf("version %q", v)) {
				t.Fatalf("%s: %v", v, err)
			}
			newer := v.Major > 0 || v.Minor > task.Schema02.Minor
			if newer != strings.Contains(err.Error(), "vibesys-cli upgrade") {
				t.Fatalf("%s: %v", v, err)
			}
		}
	}
}

func TestParseSchemaVersionReadsWhatItPrints(t *testing.T) {
	rng := rand.New(rand.NewPCG(1, 2))
	for range 1000 {
		v := task.SchemaVersion{Major: rng.Uint32N(1_000_000_000), Minor: rng.Uint32N(1_000_000_000)}
		got, err := task.ParseSchemaVersion(v.String())
		if err != nil || got != v {
			t.Fatalf("%s read as %s, %v", v, got, err)
		}
	}
	if task.SchemasText() != "0.1, 0.2" {
		t.Fatalf("schemas %q", task.SchemasText())
	}
}

func candidateRepo(files map[string]string) map[string]string {
	out := map[string]string{
		"serve/Dockerfile": "# syntax=docker/dockerfile:1\nARG V=1\nFROM --platform=linux/amd64 \\\n  vllm/vllm-openai:v0.6.3 AS base\nCOPY . /srv\n",
		"serve/server.py":  "print('serving')\n",
		"serve/weights/a":  "big",
		"README.md":        "outside the context",
	}
	for k, v := range files {
		if v == "" {
			delete(out, k)
		} else {
			out[k] = v
		}
	}
	return out
}

var serveBuild = core.CandidateBuild{Dockerfile: "serve/Dockerfile", Context: "serve", AllowedBases: []string{"vllm/vllm-openai:v0.6.3"}}

func TestACandidateIsReadFromItsContextAsTheCommitHasIt(t *testing.T) {
	got, err := task.ReadCandidate(repo(candidateRepo(nil)), serveBuild)
	if err != nil {
		t.Fatal(err)
	}
	if _, ok := got.Files["server.py"]; !ok || got.Files["Dockerfile"].Data == nil || len(got.Files) != 3 {
		t.Fatalf("files %v", got.Files)
	}
	if _, ok := got.Files["README.md"]; ok {
		t.Fatal("a file outside the context is in it")
	}
	ignored, err := task.ReadCandidate(repo(candidateRepo(map[string]string{"serve/.dockerignore": "weights\n"})), serveBuild)
	if err != nil {
		t.Fatal(err)
	}
	if _, ok := ignored.Files["weights/a"]; ok {
		t.Fatal("an ignored file is in the context")
	}
	if _, ok := ignored.Files[".dockerignore"]; !ok {
		t.Fatal("the .dockerignore is not kept")
	}
	changedIgnored, err := task.ReadCandidate(repo(candidateRepo(map[string]string{"serve/.dockerignore": "weights\n", "serve/weights/a": "other"})), serveBuild)
	if err != nil {
		t.Fatal(err)
	}
	if changedIgnored.Digest != ignored.Digest {
		t.Fatal("a change to an ignored file changed the digest")
	}
	if ignored.Digest == got.Digest {
		t.Fatal("a .dockerignore did not change the digest")
	}
	outside, err := task.ReadCandidate(repo(candidateRepo(map[string]string{"README.md": "changed"})), serveBuild)
	if err != nil || outside.Digest != got.Digest {
		t.Fatalf("a change outside the context changed the digest: %v", err)
	}
	inside, err := task.ReadCandidate(repo(candidateRepo(map[string]string{"serve/server.py": "print('faster')\n"})), serveBuild)
	if err != nil || inside.Digest == got.Digest {
		t.Fatalf("a change inside the context kept the digest: %v", err)
	}
}

func TestACandidateTheCommitCannotBuildIsRefusedNamingWhy(t *testing.T) {
	cases := []struct {
		name  string
		files map[string]string
		build core.CandidateBuild
		says  string
	}{
		{"no dockerfile", map[string]string{"serve/Dockerfile": ""}, serveBuild, "there is no file serve/Dockerfile"},
		{"no context", nil, core.CandidateBuild{Dockerfile: "serve/Dockerfile", Context: "missing"}, "there is no directory missing"},
		{"a base not allowed", map[string]string{"serve/Dockerfile": "FROM python:3.12\n"}, serveBuild, `"python:3.12"`},
		{"a base by argument", map[string]string{"serve/Dockerfile": "ARG B=x\nFROM ${B}\n"}, serveBuild, "build argument"},
		{"no FROM first", map[string]string{"serve/Dockerfile": "RUN true\nFROM vllm/vllm-openai:v0.6.3\n"}, serveBuild, "not FROM"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			_, err := task.ReadCandidate(repo(candidateRepo(c.files)), c.build)
			if f := failureOf(t, err); f.Kind != core.FailureInvalidInput || !strings.Contains(f.Message, c.says) {
				t.Fatalf("got %+v, want %q", f, c.says)
			}
		})
	}
	// Without allowed_bases any base builds.
	free := serveBuild
	free.AllowedBases = nil
	if _, err := task.ReadCandidate(repo(candidateRepo(map[string]string{"serve/Dockerfile": "FROM python:3.12\n"})), free); err != nil {
		t.Fatal(err)
	}
}

func TestFirstBaseIsTheImageOfTheFirstFrom(t *testing.T) {
	cases := map[string]string{
		"FROM a:1\nFROM b:2\n":                   "a:1",
		"# c\n\nARG X\nfrom a:1 AS s\n":          "a:1",
		"FROM --platform=$P \\\n  # c\n  a:1\n":  "a:1",
		"# syntax=docker/dockerfile:1\nFROM a\n": "a",
	}
	for text, want := range cases {
		if got, err := task.FirstBase([]byte(text)); err != nil || got != want {
			t.Errorf("%q: %q, %v; want %q", text, got, err, want)
		}
	}
	for _, text := range []string{"", "# only\n", "RUN x\n", "FROM --platform=x\n"} {
		if got, err := task.FirstBase([]byte(text)); err == nil {
			t.Errorf("%q: read %q", text, got)
		}
	}
}
