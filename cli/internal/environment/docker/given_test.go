package docker_test

import (
	"archive/tar"
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
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

// someAMDGPU are the facts a fake machine's AMD GPUs report.
var someAMDGPU = map[string]string{"amd_gpu_model": "AMD Instinct MI210", "amd_gpu_count": "1", "amdgpu_driver": "6.16.13"}

// amdOf is a resource that attaches AMD GPUs the way the rocm resource does: the GPUs, and a check
// the container runs first, here a script of the fake's.
func amdOf(check ...string) *fakeresource.Fake {
	return &fakeresource.Fake{
		Of: core.ResourceROCm, Facts: someAMDGPU, Attached: resource.Attachment{GPUs: resource.GPUs{ROCm: true}, Check: check},
		Claimed: []core.Claim{{Resource: "rocm:local", Amount: 1}},
	}
}

// remoteMachine is a Machine whose directories live under another root, as a machine reached
// over ssh has them, and that records what it was asked.
type remoteMachine struct {
	mu        sync.Mutex
	staged    []docker.Dirs
	collected []docker.Dirs
	replaced  []docker.Dirs
	added     []string
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

func (m *remoteMachine) Replace(_ context.Context, local, _ docker.Dirs) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.replaced = append(m.replaced, local)
	return nil
}

func (m *remoteMachine) Add(_ context.Context, _, staged docker.Dirs, rel string) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.added = append(m.added, staged.Artifacts+"/"+rel)
	return nil
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

func (m *remoteMachine) Program(_ context.Context, local string) (string, error) {
	return "/far/bin" + local, nil
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

// Placements are interchangeable, so a fingerprint records the GPUs it ran with, for diagnosis, and
// other GPUs leave the image's results comparable: the fact changes and equality does not.
func TestAGPUFactThatChangesIsRecordedAndComparesTheSame(t *testing.T) {
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
		if after.Facts[name] != changed[name] {
			t.Errorf("a different %s was recorded as %q, want %q", name, after.Facts[name], changed[name])
		}
		if !after.Equal(before) {
			t.Errorf("a different %s made the image's results incomparable", name)
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
		"HOME": "/tmp", "VIBESYS_CACHE_DIR": docker.CacheDir, "VIBESYS_ARTIFACTS_DIR": docker.ArtifactsDir, "VIBESYS_MOUNTS": "hf-cache,models",
		"VIBESYS_JOB_TIMEOUT_SECONDS": "3600",
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

// A link of the task is in the build context as the same link, so the image has it: none is
// dropped and none is replaced by what it points at.
func TestTheBuildContextHoldsEveryFileAndLinkOfTheTask(t *testing.T) {
	files := task.Tree{
		"k8s/manifest.yaml": {Data: []byte("kind: List\n")},
		"k8s/deployed.yaml": {Link: "manifest.yaml"},
		"bin":               {Link: "k8s"},
		"run.sh":            {Data: []byte("#!/bin/sh\n"), Executable: true},
	}
	ctx, err := docker.Context(docker.Spec{Dockerfile: []byte(simpleDockerfile), Files: files})
	if err != nil {
		t.Fatal(err)
	}
	got := task.Tree{}
	r := tar.NewReader(bytes.NewReader(ctx))
	for {
		h, err := r.Next()
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			t.Fatal(err)
		}
		switch h.Typeflag {
		case tar.TypeSymlink:
			got[h.Name] = task.File{Link: h.Linkname}
		case tar.TypeReg:
			data, _ := io.ReadAll(r)
			got[h.Name] = task.File{Data: data, Executable: h.Mode&0o111 != 0}
		default:
			t.Errorf("%s is an entry of type %q", h.Name, h.Typeflag)
		}
	}
	want := maps.Clone(files)
	want["Dockerfile"] = task.File{Data: []byte(simpleDockerfile)}
	if !maps.EqualFunc(got, want, func(a, b task.File) bool {
		return string(a.Data) == string(b.Data) && a.Executable == b.Executable && a.Link == b.Link
	}) {
		t.Errorf("the build context holds %+v, want %+v", got, want)
	}
	moved := maps.Clone(files)
	moved["bin"] = task.File{Link: "run.sh"}
	if other, _ := docker.Context(docker.Spec{Dockerfile: []byte(simpleDockerfile), Files: moved}); string(other) == string(ctx) {
		t.Error("a link's new target left the context as it was")
	}
}

func TestAChangedScriptBuildsANewImage(t *testing.T) {
	const copying = simpleDockerfile + "COPY check.sh /task/\n"
	s := newScene(t, options{spec: docker.Spec{Dockerfile: []byte(copying), Files: task.Tree{"check.sh": {Data: []byte("exit 0")}}}})
	first, err := s.env.Fingerprint(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	other := newRig(t, s.daemon, options{spec: docker.Spec{Dockerfile: []byte(copying), Files: task.Tree{"check.sh": {Data: []byte("exit 1")}}}, process: 2})
	second, err := other.env(t).Fingerprint(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if s.daemon.Builds() != 2 || first.ImageDigest == second.ImageDigest {
		t.Errorf("%d builds, digests %s and %s: a changed script must build a new image", s.daemon.Builds(), first.ImageDigest, second.ImageDigest)
	}
}

// Regression: the task's image was named by the task's files alone, so a base image rebuilt under
// the same tag (FROM a locally built image:tag) left the task on the image built on the old base
// until the operator removed it by hand.
func TestABaseImageRebuiltUnderTheSameTagBuildsTheTasksImageAgain(t *testing.T) {
	const ref = "registry.example/team/base:dev"
	s := newScene(t, options{spec: docker.Spec{Dockerfile: []byte("FROM " + ref + "\nRUN true\n")}})
	s.daemon.Load(ref, "base v1")
	first := s.fingerprint()
	s.mustRun("print", "a")
	if again := s.fingerprint(); !again.Equal(first) || s.daemon.Builds() != 1 {
		t.Fatalf("an unchanged base: %d builds, fingerprints %v and %v", s.daemon.Builds(), first, again)
	}
	s.daemon.Load(ref, "base v2")
	rebuilt := s.fingerprint()
	if s.daemon.Builds() != 2 || rebuilt.ImageDigest == first.ImageDigest {
		t.Fatalf("the base %s changed under its tag: %d builds, digests %s and %s; want the task's image built again on it", ref, s.daemon.Builds(), first.ImageDigest, rebuilt.ImageDigest)
	}
}

// Every way a Dockerfile names an image it builds on from outside the build (FROM, COPY --from, an
// ARG default, a stage of a multi-stage build, a continued line, another escape character, either
// line ending, any case) makes a change of that image under its reference build the task's image
// again; a change of an image the Dockerfile does not name, or of nothing, builds nothing.
func TestEveryBaseADockerfileNamesIsPartOfTheImagesName(t *testing.T) {
	shapes := []func(repo, tag string) string{
		func(repo, tag string) string { return "FROM " + repo + ":" + tag + "\nRUN true\n" },
		func(repo, tag string) string { return "from " + repo + ":" + tag + " as base\nrun true\n" },
		func(repo, tag string) string {
			return "# escape=`\nFROM `\n  " + repo + ":" + tag + "\nRUN true\n"
		},
		func(repo, tag string) string {
			return "ARG BASE=" + repo + ":" + tag + "\nFROM ${BASE} AS build\nRUN make\nFROM build\nRUN true\n"
		},
		func(repo, tag string) string {
			return "ARG TAG=" + tag + "\nFROM " + repo + ":$TAG\nARG TAG=other\nRUN true\n"
		},
		func(repo, tag string) string {
			return "FROM --platform=linux/amd64 " + repo + ":" + tag + " AS deps\nFROM scratch\nCOPY --from=deps /x /x\nCOPY --from=0 /y /y\n"
		},
		func(repo, tag string) string {
			return "FROM scratch\r\n# a comment\r\nCOPY --chown=1:1 \\\r\n  --from=" + repo + ":" + tag + " /opt /opt\r\n"
		},
	}
	rng := rand.New(rand.NewPCG(3, 4))
	for i := range 40 {
		repo := fmt.Sprintf("registry.example/team%d/base", rng.IntN(1000))
		tag := fmt.Sprintf("v%d", rng.IntN(1000))
		shape := rng.IntN(len(shapes))
		dockerfile := shapes[shape](repo, tag)
		s := newScene(t, options{spec: docker.Spec{Dockerfile: []byte(dockerfile)}})
		s.daemon.Load(repo+":"+tag, "one")
		s.daemon.Load(repo+":unrelated", "one")
		s.fingerprint()
		s.daemon.Load(repo+":unrelated", "two")
		s.fingerprint()
		if n := s.daemon.Builds(); n != 1 {
			t.Fatalf("case %d (shape %d): %d builds with the base unchanged, want 1:\n%s", i, shape, n, dockerfile)
		}
		s.daemon.Load(repo+":"+tag, "two")
		s.fingerprint()
		s.fingerprint()
		if n := s.daemon.Builds(); n != 2 {
			t.Fatalf("case %d (shape %d): %d builds after %s:%s changed, want 2:\n%s", i, shape, n, repo, tag, dockerfile)
		}
	}
}

// A reference that names a variable the Dockerfile gives no value is not looked up, and the image
// is built as before.
func TestABaseNamedByAnUnsetVariableIsNotLookedUp(t *testing.T) {
	s := newScene(t, options{spec: docker.Spec{Dockerfile: []byte("ARG TAG\nFROM registry.example/team/base:${TAG}\nRUN true\n")}})
	s.daemon.SetFaults(func(c dockerfake.Call) *dockerfake.Fault {
		if c.Op == dockerfake.OpImage && strings.Contains(c.Name, "$") {
			return &dockerfake.Fault{Err: errors.New("invalid reference format")}
		}
		return nil
	})
	s.fingerprint()
	if n := s.daemon.Builds(); n != 1 {
		t.Fatalf("%d builds, want 1", n)
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
