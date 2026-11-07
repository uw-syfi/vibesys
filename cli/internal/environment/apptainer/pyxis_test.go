package apptainer_test

import (
	"context"
	"path"
	"slices"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment/apptainer"
	"github.com/uw-syfi/vibesys/cli/internal/environment/apptainer/clusterfake"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker/dockerfake"
	"github.com/uw-syfi/vibesys/cli/internal/environment/envtest"
	"github.com/uw-syfi/vibesys/cli/internal/environment/fakeenv/fakeworld"
)

// squashOf is where the cluster keeps the squashfs of the image of the ID under the pyxis runtime.
func squashOf(t testing.TB, local *dockerfake.Client, id string) string {
	t.Helper()
	content, err := local.Content(context.Background(), id)
	if err != nil {
		t.Fatal(err)
	}
	return path.Join(root, "images", strings.TrimPrefix(content, "sha256:")+".sqsh")
}

// Under the pyxis runtime an image's files are streamed to the login node, which makes its
// squashfs once, named by its content's digest, with what made it beside it: the image's files as
// Enroot runs them, its environment in etc/environment, and nothing of Docker's own. The boxes run
// it. A later run of another process makes nothing, and nothing else is put on the cluster: no SIF,
// no archive.
func TestUnderPyxisTheLoginNodeMakesAnImagesSquashfsOnce(t *testing.T) {
	w := fakeworld.New()
	cluster := clusterfake.New(w.MemoryGates)
	r := newRig(t, w, cluster, setup{runtime: apptainer.RuntimePyxis})
	a, out := run(t, r.env, "print", "hello")
	exited(t, a, 0)
	if strings.TrimSpace(out) != "hello" {
		t.Errorf("output %q", out)
	}
	f, err := r.env.Fingerprint(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	sqsh := squashOf(t, r.local, f.ImageDigest)
	if files := imagesOn(cluster); !slices.Equal(files, []string{sqsh, sqsh + apptainer.BuiltBy}) {
		t.Fatalf("images on the cluster %v, want the squashfs %s and what made it", files, sqsh)
	}
	by, _ := cluster.File(sqsh + apptainer.BuiltBy)
	if !strings.Contains(string(by), clusterfake.Squasher) {
		t.Errorf("made by %q", by)
	}
	data, _ := cluster.File(sqsh)
	files := map[string]string{}
	var names []string
	for _, e := range entriesOf(t, data) {
		files[e.name] = e.data
		names = append(names, e.name)
	}
	content, _ := r.local.Content(context.Background(), f.ImageDigest)
	if files["etc/environment"] != strings.Join(dockerfake.Env, "\n")+"\n" || files[apptainer.ImageMarker] != content+"\n" {
		t.Errorf("the squashfs's environment %q and marker %q", files["etc/environment"], files[apptainer.ImageMarker])
	}
	for _, name := range names {
		if name == ".dockerenv" || (strings.HasPrefix(name, "dev/") && name != "dev/") {
			t.Errorf("the squashfs has Docker's own %s", name)
		}
	}
	if _, ok := files["etc/os-release"]; !ok {
		t.Errorf("the squashfs lacks the image's own files: %v", names)
	}
	for _, b := range cluster.Started() {
		if b.Image != sqsh {
			t.Errorf("a box ran %s, want the squashfs %s", b.Image, sqsh)
		}
	}

	again := newRig(t, w, cluster, setup{runtime: apptainer.RuntimePyxis, process: 1})
	a, _ = run(t, again.env, "exit", "0")
	exited(t, a, 0)
	if n := cluster.Published(sqsh); n != 1 {
		t.Errorf("the squashfs was made %d times, want once", n)
	}
	nothingLeft(t, cluster)
}

// A squashfs whose stream failed, was cut short, or took longer than its bound is never on the
// cluster, and the run fails before its command saying why.
func TestUnderPyxisAFailedSquashLeavesNoImage(t *testing.T) {
	cases := []struct {
		name    string
		export  *dockerfake.Fault
		cluster error
		timers  clock.Timers
		want    string
	}{
		{"the export fails", &dockerfake.Fault{Err: &core.Failure{Kind: core.FailureUnavailable, Message: "no space left on device"}}, nil, nil, "no space left on device"},
		{"the export is cut short", &dockerfake.Fault{Err: &core.Failure{Kind: core.FailureUnavailable, Message: "the daemon went away"}, Late: true}, nil, nil, "the daemon went away"},
		{"sqfstar fails", nil, &core.Failure{Kind: core.FailureUnavailable, Message: "FATAL ERROR: out of space"}, nil, "out of space"},
		{"too slow", nil, clusterfake.ErrHang, clock.Exactly{D: apptainer.DefaultConvertTimeout, Others: envtest.NewTimers()}, "convert_timeout_seconds"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			w := fakeworld.New()
			cluster := clusterfake.New(w.MemoryGates)
			faults := func(d *dockerfake.Daemon) {
				d.SetFaults(func(call dockerfake.Call) *dockerfake.Fault {
					if call.Op == dockerfake.OpExport {
						return c.export
					}
					return nil
				})
			}
			r := newRig(t, w, cluster, setup{runtime: apptainer.RuntimePyxis, engineTimers: c.timers, daemon: []dockerfake.Option{faults}})
			cluster.SetFaults(func(op string) error {
				if op == "squash" {
					return c.cluster
				}
				return nil
			})
			a, _ := run(t, r.env, "mark", "ran")
			f := failure(t, a)
			if !strings.Contains(f.Message, c.want) || w.Marked("ran") {
				t.Errorf("failure %q, want one saying %q; ran %v", f.Message, c.want, w.Marked("ran"))
			}
			if files := imagesOn(cluster); len(files) != 0 {
				t.Errorf("files on the cluster %v", files)
			}
			nothingLeft(t, cluster)
		})
	}
}

// An engine of the pyxis runtime needs no converter and no local directory, and one of no runtime
// it knows is refused.
func TestAPyxisEngineConvertsNothingHere(t *testing.T) {
	w := fakeworld.New()
	local := dockerfake.NewDaemon(w.MemoryGates).Connect()
	cfg := apptainer.Config{
		Local: local, Export: local, Cluster: clusterfake.New(w.MemoryGates).Connect(), Root: root,
		Timers: envtest.NewTimers(), Clock: clock.System{}, Runtime: apptainer.RuntimePyxis,
	}
	if _, err := apptainer.New(cfg); err != nil {
		t.Errorf("a pyxis engine without a converter: %v", err)
	}
	cfg.Runtime = apptainer.Runtime(7)
	if _, err := apptainer.New(cfg); err == nil {
		t.Error("an engine of an unknown runtime was made")
	}
}
