package docker_test

import (
	"context"
	"errors"
	"fmt"
	"maps"
	"math/rand/v2"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker/dockerfake"
	"github.com/uw-syfi/vibesys/cli/internal/environment/envtest"
	"github.com/uw-syfi/vibesys/cli/internal/resource"
	"github.com/uw-syfi/vibesys/cli/internal/resource/fakeresource"
	"github.com/uw-syfi/vibesys/cli/internal/task"
)

// someGPU are the facts a fake machine's GPUs report.
var someGPU = map[string]string{"gpu_model": "NVIDIA GB10", "gpu_count": "1", "gpu_driver": "580.159.03", "cuda_driver": "13.0"}

func gpuOf(facts map[string]string) *fakeresource.Fake {
	return &fakeresource.Fake{
		Of: core.ResourceGPU, Facts: facts, Attached: resource.Attachment{GPUs: resource.GPUs{Devices: "all"}},
		Claimed: []core.Claim{{Resource: "gpu:local/all", Amount: 1}},
	}
}

func withGPUs(c *docker.Config) { c.Resources = append(c.Resources, gpuOf(someGPU)) }

// remoteMachine is a Machine whose directories live under another root, as a machine reached
// over ssh has them, and that records what it was asked.
type remoteMachine struct {
	mu        sync.Mutex
	staged    []docker.Dirs
	collected []docker.Dirs
	caches    []string
	failWith  error // fails Collect
}

func (m *remoteMachine) Stage(_ context.Context, d docker.Dirs) (docker.Dirs, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.staged = append(m.staged, d)
	out := docker.Dirs{Checkout: "/far" + d.Checkout}
	if d.Scratch != "" {
		out.Scratch = "/far" + d.Scratch
	}
	if d.Artifacts != "" {
		out.Artifacts = "/far" + d.Artifacts
	}
	return out, nil
}

func (m *remoteMachine) Collect(_ context.Context, local, staged docker.Dirs) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.collected = append(m.collected, local)
	return m.failWith
}

func (m *remoteMachine) Cache(_ context.Context, key string) (string, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.caches = append(m.caches, key)
	return "/far/cache/" + key, nil
}

func (m *remoteMachine) User(context.Context) (docker.User, error) {
	return docker.User{UID: 1002, GID: 1002}, nil
}

func runIn(t *testing.T, s *scene, x core.Execution) core.Attempt {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), envtest.Backstop)
	defer cancel()
	return s.env.Run(ctx, x, nil)
}

func onlyContainer(t *testing.T, d *dockerfake.Daemon) docker.ContainerSpec {
	t.Helper()
	all := d.Containers()
	if len(all) != 1 {
		t.Fatalf("%d containers, want the run's one", len(all))
	}
	return all[0].Spec
}

func TestAContainerGetsTheGPUsAndTheFingerprintPinsThem(t *testing.T) {
	s := newScene(t, options{spec: docker.Spec{Dockerfile: []byte(simpleDockerfile), Backend: core.BackendCUDA}, configure: withGPUs})
	f, err := s.env.Fingerprint(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if f.Backend != core.BackendCUDA {
		t.Errorf("backend %q, want cuda", f.Backend)
	}
	for k, v := range someGPU {
		if f.Facts[k] != v {
			t.Errorf("fact %s = %q, want %q", k, f.Facts[k], v)
		}
	}
	s.daemon.SetFaults(func(c dockerfake.Call) *dockerfake.Fault {
		if c.Op == dockerfake.OpRemove {
			return &dockerfake.Fault{Err: errors.New("kept for the test")}
		}
		return nil
	})
	_ = runIn(t, s, core.Execution{Checkout: checkout(t), Argv: []string{"print", "x"}, Timeout: time.Hour})
	if got := onlyContainer(t, s.daemon).GPUs; got != (docker.GPUs{Devices: "all"}) {
		t.Errorf("the container got GPUs %+v, want all", got)
	}
}

func TestAGPUFactThatChangesChangesTheFingerprint(t *testing.T) {
	base := newScene(t, options{spec: docker.Spec{Dockerfile: []byte(simpleDockerfile), Backend: core.BackendCUDA}, configure: withGPUs})
	before, err := base.env.Fingerprint(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	for name := range someGPU {
		changed := maps.Clone(someGPU)
		changed[name] += "-other"
		s := newScene(t, options{spec: docker.Spec{Dockerfile: []byte(simpleDockerfile), Backend: core.BackendCUDA}, configure: func(c *docker.Config) {
			c.Resources = []resource.Resource{gpuOf(changed)}
		}})
		after, err := s.env.Fingerprint(context.Background())
		if err != nil {
			t.Fatal(err)
		}
		if after.Equal(before) {
			t.Errorf("a different %s left the fingerprint as it was", name)
		}
	}
}

// mountsOf is a resource that mounts the directories the way the mounts resource does.
func mountsOf() *fakeresource.Fake {
	return &fakeresource.Fake{
		Of: core.ResourceMounts, Facts: map[string]string{"mounts": "hf-cache,models"},
		Attached: resource.Attachment{
			Mounts: []resource.Mount{
				{Host: "/data/hf", Container: "/vibesys/mounts/hf-cache", ReadOnly: true},
				{Host: "/data/models", Container: "/vibesys/mounts/models", ReadOnly: true},
			},
			Env: map[string]string{"VIBESYS_MOUNTS": "hf-cache,models"},
		},
	}
}

func TestARunSeesItsDirectoriesTheCacheAndTheMountsAtFixedPlaces(t *testing.T) {
	m := &remoteMachine{}
	s := newScene(t, options{configure: func(c *docker.Config) {
		c.Machine = m
		c.Resources = []resource.Resource{mountsOf()}
	}})
	s.daemon.SetFaults(func(c dockerfake.Call) *dockerfake.Fault {
		if c.Op == dockerfake.OpRemove {
			return &dockerfake.Fault{Err: errors.New("kept for the test")}
		}
		return nil
	})
	co := checkout(t)
	x := core.Execution{Checkout: co, Scratch: "/scratch/s1", Artifacts: "/art/01-check", Argv: []string{"print", "x"}, Timeout: time.Hour}
	_ = runIn(t, s, x)
	spec := onlyContainer(t, s.daemon)
	want := []docker.Mount{
		{Host: "/far" + co.Dir, Container: co.Dir},
		{Host: "/far/scratch/s1", Container: "/scratch/s1"},
		{Host: "/far/cache/" + m.caches[0], Container: docker.CacheDir},
		{Host: "/far/art/01-check", Container: docker.ArtifactsDir},
		{Host: "/data/hf", Container: "/vibesys/mounts/hf-cache", ReadOnly: true},
		{Host: "/data/models", Container: "/vibesys/mounts/models", ReadOnly: true},
	}
	if !slices.Equal(spec.Mounts, want) {
		t.Errorf("mounts\n%+v\nwant\n%+v", spec.Mounts, want)
	}
	if spec.Dir != co.Dir || spec.User != (docker.User{UID: 1002, GID: 1002}) {
		t.Errorf("dir %q user %+v: want the caller's checkout path and the machine's user", spec.Dir, spec.User)
	}
	wantEnv := map[string]string{
		"HOME": "/tmp", docker.EnvCacheDir: docker.CacheDir, docker.EnvArtifactsDir: docker.ArtifactsDir, "VIBESYS_MOUNTS": "hf-cache,models",
	}
	if !maps.Equal(spec.Env, wantEnv) {
		t.Errorf("env %v, want %v", spec.Env, wantEnv)
	}
	if spec.Limit != time.Hour {
		t.Errorf("limit %v, want the execution's timeout", spec.Limit)
	}
	if !strings.HasPrefix(m.caches[0], "task-") {
		t.Errorf("cache key %q does not name the task", m.caches[0])
	}
	if len(m.collected) != 1 || m.collected[0].Scratch != x.Scratch || m.collected[0].Artifacts != x.Artifacts {
		t.Errorf("collected %+v, want the run's directories once", m.collected)
	}
}

// Every ending of a command brings back what it wrote, and an ending whose files did not come back
// cannot be vouched for.
func TestWhatARunWroteIsBroughtBackOnEveryEnding(t *testing.T) {
	for _, script := range [][]string{{"print", "x"}, {"exit", "3"}} {
		for _, fail := range []bool{false, true} {
			t.Run(fmt.Sprintf("%v/fail=%v", script, fail), func(t *testing.T) {
				m := &remoteMachine{}
				if fail {
					m.failWith = errors.New("rsync: connection closed")
				}
				s := newScene(t, options{configure: func(c *docker.Config) { c.Machine = m }})
				a := runIn(t, s, core.Execution{Checkout: checkout(t), Scratch: "/scratch/s", Argv: script, Timeout: time.Hour})
				if len(m.collected) != 1 {
					t.Fatalf("collected %d times, want once", len(m.collected))
				}
				_, unobserved := a.(core.Unobserved)
				if unobserved != fail {
					t.Errorf("attempt %+v with a failed collect=%v", a, fail)
				}
				if fail && !strings.Contains(a.(core.Unobserved).Failure.Message, "brought back") {
					t.Errorf("the failure %q does not say the files were not brought back", a.(core.Unobserved).Failure.Message)
				}
			})
		}
	}
}

// The image is named by every file of the task, in any order: a changed script is a new image,
// and the same files are the same one.
func TestTheImageIsNamedByEveryFileOfTheTask(t *testing.T) {
	rng := rand.New(rand.NewPCG(1, 2))
	for i := range 50 {
		files := task.Tree{}
		for j := range 1 + rng.IntN(6) {
			files[fmt.Sprintf("d%d/f%d.sh", rng.IntN(3), j)] = task.File{Data: []byte(fmt.Sprint(rng.Int())), Executable: rng.IntN(2) == 0}
		}
		spec := docker.Spec{Dockerfile: []byte(simpleDockerfile), Files: files}
		a, err := docker.Context(spec)
		if err != nil {
			t.Fatal(err)
		}
		again, _ := docker.Context(docker.Spec{Dockerfile: spec.Dockerfile, Files: maps.Clone(files)})
		if string(a) != string(again) {
			t.Fatalf("case %d: the same files made two contexts", i)
		}
		name := slices.Sorted(maps.Keys(files))[rng.IntN(len(files))]
		for _, change := range []func(task.File) task.File{
			func(f task.File) task.File { f.Data = append(slices.Clone(f.Data), 'x'); return f },
			func(f task.File) task.File { f.Executable = !f.Executable; return f },
		} {
			other := maps.Clone(files)
			other[name] = change(other[name])
			b, _ := docker.Context(docker.Spec{Dockerfile: spec.Dockerfile, Files: other})
			if string(a) == string(b) {
				t.Fatalf("case %d: changing %s left the context as it was", i, name)
			}
		}
	}
}

func TestAChangedScriptBuildsANewImage(t *testing.T) {
	s := newScene(t, options{spec: docker.Spec{Dockerfile: []byte(simpleDockerfile), Files: task.Tree{"check.sh": {Data: []byte("exit 0")}}}})
	first, err := s.env.Fingerprint(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	other := newRig(t, s.daemon, options{spec: docker.Spec{Dockerfile: []byte(simpleDockerfile), Files: task.Tree{"check.sh": {Data: []byte("exit 1")}}}, process: 2})
	second, err := other.env(t).Fingerprint(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if s.daemon.Builds() != 2 || first.ImageDigest == second.ImageDigest {
		t.Errorf("%d builds, digests %s and %s: a changed script must build a new image", s.daemon.Builds(), first.ImageDigest, second.ImageDigest)
	}
}

// A measured command's container says so, so an engine that shares the machine can give it all
// of it; an unmeasured one does not.
func TestAContainerIsMeasuredExactlyWhenItsCommandIs(t *testing.T) {
	for _, measured := range []bool{false, true} {
		s := newScene(t, options{spec: docker.Spec{Dockerfile: []byte(simpleDockerfile)}})
		s.daemon.SetFaults(func(c dockerfake.Call) *dockerfake.Fault {
			if c.Op == dockerfake.OpRemove {
				return &dockerfake.Fault{Err: errors.New("kept for the test")}
			}
			return nil
		})
		_ = runIn(t, s, core.Execution{Checkout: checkout(t), Argv: []string{"print", "x"}, Timeout: time.Hour, Measured: measured})
		if got := onlyContainer(t, s.daemon).Measured; got != measured {
			t.Errorf("command measured %v: container measured %v", measured, got)
		}
	}
}
