//go:build e2e

package e2e

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

// toyTask is the task the scripted agents optimize: a repository with one file, score.txt, and a
// benchmark that reports it. It has no Dockerfile, so vibesys-cli runs it on the host, with no
// toolchain and no container runtime.
const toyTask = "toy"

// toy is a git repository holding the task, with its result branch at the base commit.
type toy struct {
	t    *testing.T
	path string
	// Base is the commit the run starts from, whose score is 10.
	base string
}

// newToy writes the repository under dir.
//
// The benchmark reports score.txt plus a hundredth for each sample in turn (+0.00, +0.01, +0.02),
// counted in a file outside the checkout. Identical samples would make vibesys-cli's compare
// answer inconclusive (constant_samples) and refuse the promotion; this jitter is deterministic,
// since the daemon runs one job at a time and every measure here takes three samples.
func newToy(t *testing.T, dir string) *toy {
	t.Helper()
	path := filepath.Join(dir, "toy-repo")
	counter := filepath.Join(dir, "toy-samples")
	if err := os.WriteFile(counter, nil, 0o600); err != nil {
		t.Fatal(err)
	}
	r := &toy{t: t, path: path}
	r.write("score.txt", "10\n")
	r.write(".vibesys/tasks/toy/vibesys.input.toml", fmt.Sprintf(`version = 1
backends = ["cpu"]

[accuracy]
command = ['sh', '-c', 'test -s ${PROJECT_ROOT}/score.txt']

[benchmark]
command = ['sh', '-c', 'n=$(wc -c < %[1]s); printf x >> %[1]s; printf "{\"score\": %%s.0%%s}" "$(cat ${PROJECT_ROOT}/score.txt)" "$((n %% 3))" > "$2"', 'sh']

[benchmark.result]
json_argument = "--out"
metric = "score"
`, counter))
	r.write(".vibesys/tasks/toy/objectives.toml", "[[objective]]\nname = \"score\"\ndirection = \"max\"\n")
	r.git("init", "-q", "-b", "main")
	r.git("add", "score.txt", ".vibesys")
	r.git("commit", "-q", "-m", "toy task")
	r.base = r.git("rev-parse", "HEAD")
	// vibesys-cli promotes by moving this branch, which no one may have checked out.
	r.git("branch", "vibesys/result/"+toyTask, r.base)
	return r
}

func (r *toy) write(rel, content string) {
	r.t.Helper()
	full := filepath.Join(r.path, filepath.FromSlash(rel))
	if err := os.MkdirAll(filepath.Dir(full), 0o755); err != nil {
		r.t.Fatal(err)
	}
	if err := os.WriteFile(full, []byte(content), 0o644); err != nil {
		r.t.Fatal(err)
	}
}

func (r *toy) git(args ...string) string {
	r.t.Helper()
	cmd := exec.Command("git", append([]string{"-C", r.path, "-c", "user.name=toy", "-c", "user.email=toy@example.com", "-c", "commit.gpgsign=false"}, args...)...)
	out, err := cmd.CombinedOutput()
	if err != nil {
		r.t.Fatalf("git %s: %v\n%s", strings.Join(args, " "), err, out)
	}
	return strings.TrimSpace(string(out))
}
