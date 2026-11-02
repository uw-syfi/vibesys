//go:build docker && unix

package dockercli_test

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker/dockercli"
	"github.com/uw-syfi/vibesys/cli/internal/environment/envtest"
)

// A task of a real git repository runs in its pinned image, from a clean checkout of the exact
// commit, as the invoking user. These tests are the nearest thing to a job's run that this package
// can show before a daemon wires the docker environment in.

func git(t *testing.T, dir string, args ...string) string {
	t.Helper()
	cmd := exec.Command("git", append([]string{"-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false"}, args...)...)
	cmd.Dir = dir
	cmd.Env = append(os.Environ(), "GIT_CONFIG_GLOBAL=/dev/null", "GIT_CONFIG_SYSTEM=/dev/null")
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("git %v: %v\n%s", args, err, out)
	}
	return strings.TrimSpace(string(out))
}

// commit writes the files into the repository and commits them, and returns the commit.
func commit(t *testing.T, repo string, files map[string]string) core.Commit {
	t.Helper()
	for name, text := range files {
		path := filepath.Join(repo, name)
		if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, []byte(text), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	git(t, repo, "add", "-A")
	git(t, repo, "commit", "-q", "-m", "c")
	return core.Commit(git(t, repo, "rev-parse", "HEAD"))
}

// gitSource reads a task's Dockerfile at the commit of the target.
type gitSource struct{ t *testing.T }

func (s gitSource) Spec(_ context.Context, target environment.Target) (docker.Spec, error) {
	text := git(s.t, target.Repository, "show", string(target.Commit)+":.vibesys/tasks/"+target.Task+"/Dockerfile")
	return docker.Spec{Dockerfile: []byte(text + "\n")}, nil
}

// checkout makes a clean checkout of the exact commit, in a directory of the test's own.
func checkout(t *testing.T, repo string, c core.Commit) core.Checkout {
	t.Helper()
	dir := filepath.Join(t.TempDir(), "checkout")
	git(t, repo, "clone", "-q", "--no-checkout", repo, dir)
	git(t, dir, "checkout", "-q", "--detach", string(c))
	real, err := filepath.EvalSymlinks(dir)
	if err != nil {
		t.Fatal(err)
	}
	return core.Checkout{Commit: c, Dir: real}
}

func providerOver(t *testing.T, scope string, engine docker.Engine) *docker.Provider {
	t.Helper()
	p, err := docker.New(docker.Config{
		Engine: engine, Source: gitSource{t}, Backend: cpuBackend(), Timers: clock.System{},
		IDs: dockercli.RandomIDs{}, Scope: scope, User: docker.User{UID: os.Getuid(), GID: os.Getgid()},
	})
	if err != nil {
		t.Fatal(err)
	}
	return p
}

func TestATaskRunsFromACleanCheckoutOfItsCommitInItsPinnedImage(t *testing.T) {
	scope := "t" + randomHex(6)
	t.Cleanup(func() { removeScope(scope) })
	repo := t.TempDir()
	git(t, repo, "init", "-q")
	first := commit(t, repo, map[string]string{
		".vibesys/tasks/t/Dockerfile": dockerfile,
		"run.sh":                      "echo running in $(cat /etc/os-release | head -1 | cut -d= -f1) at $(pwd)\ntouch made-by-the-run\n",
	})
	engine := &countingEngine{Engine: newEngine()}

	env := func(c core.Commit) environment.Environment {
		e, err := providerOver(t, scope, engine).Environment(environment.Target{Repository: repo, Commit: c, Task: "t"})
		if err != nil {
			t.Fatal(err)
		}
		return e
	}
	ctx, cancel := context.WithTimeout(context.Background(), envtest.Backstop)
	defer cancel()

	co := checkout(t, repo, first)
	// An untracked file in the repository's own tree must not reach the run: it is a checkout of
	// the commit, not of the working tree.
	if err := os.WriteFile(filepath.Join(repo, "dirty"), []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	res, out := run(t, env(first), co.Dir, "sh", "run.sh")
	if res.Kind != core.ExecExited || res.ExitCode != 0 || !strings.Contains(out, "at "+co.Dir) {
		t.Fatalf("outcome %+v, output %q", res, out)
	}
	info, err := os.Stat(filepath.Join(co.Dir, "made-by-the-run"))
	if err != nil {
		t.Fatal(err)
	}
	if uid := info.Sys().(*syscall.Stat_t).Uid; int(uid) != os.Getuid() {
		t.Errorf("the run's file belongs to user %d, want the invoking user %d", uid, os.Getuid())
	}
	if _, err := os.Stat(filepath.Join(co.Dir, "dirty")); err == nil {
		t.Error("the run saw a file that is not in the commit")
	}

	// The same task again, as a later job: nothing is built, and the result is comparable.
	builtBefore := engine.builds.Load()
	ctxFP, cancelFP := context.WithTimeout(ctx, envtest.Backstop)
	defer cancelFP()
	fp1, err := env(first).Fingerprint(ctxFP)
	if err != nil {
		t.Fatal(err)
	}
	second := commit(t, repo, map[string]string{"run.sh": "echo changed code, same task\n"})
	fp2, err := env(second).Fingerprint(ctxFP)
	if err != nil {
		t.Fatal(err)
	}
	if n := engine.builds.Load(); n != builtBefore {
		t.Errorf("a later job of an unchanged task built the image: %d builds, then %d", builtBefore, n)
	}
	if !fp1.Equal(fp2) {
		t.Errorf("the same image gave two environments: %s and %s", fp1, fp2)
	}

	// A change to the Dockerfile is a different environment, and the image is built for it.
	third := commit(t, repo, map[string]string{".vibesys/tasks/t/Dockerfile": dockerfile + "RUN echo " + randomHex(6) + "\n"})
	fp3, err := env(third).Fingerprint(ctxFP)
	if err != nil {
		t.Fatal(err)
	}
	// The image is this test's own, with a line no other run has: remove it, so that the tests
	// leave one image of each fixed Dockerfile and not one for every run.
	t.Cleanup(func() { _ = exec.Command("docker", "rmi", fp3.ImageDigest).Run() })
	if fp1.Equal(fp3) || fp1.ImageDigest == fp3.ImageDigest {
		t.Errorf("a changed Dockerfile gave the same environment %s", fp3)
	}
	if n := engine.builds.Load(); n != builtBefore+1 {
		t.Errorf("the changed Dockerfile was built %d times, want once", n-builtBefore)
	}
}
