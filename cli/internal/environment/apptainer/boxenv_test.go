package apptainer_test

import (
	"strings"
	"testing"
	"testing/quick"

	"github.com/uw-syfi/vibesys/cli/internal/environment/apptainer"
	"github.com/uw-syfi/vibesys/cli/internal/environment/apptainer/clusterfake"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker/dockerfake"
	"github.com/uw-syfi/vibesys/cli/internal/environment/fakeenv/fakeworld"
)

// A process on a node runs with the image's own environment, as a container of the docker
// environment does, under every runtime. Pyxis gives a step the login node's PATH, so a box that
// named only the job's variables ran a check without the tools the image's PATH names (a check
// of a task whose image puts Go under /usr/local/go/bin did not find it).
func TestABoxNamesTheImagesEnvironmentToEveryProcessUnderEveryRuntime(t *testing.T) {
	for name, runtime := range map[string]apptainer.Runtime{"apptainer": apptainer.RuntimeApptainer, "pyxis": apptainer.RuntimePyxis} {
		t.Run(name, func(t *testing.T) {
			w := fakeworld.New()
			cluster := clusterfake.New(w.MemoryGates)
			r := newRig(t, w, cluster, setup{runtime: runtime})
			a, _ := run(t, r.env, "print", "hello")
			exited(t, a, 0)
			boxes := cluster.Started()
			if len(boxes) == 0 {
				t.Fatal("no box started")
			}
			for _, b := range boxes {
				for _, kv := range dockerfake.Env {
					k, v, _ := strings.Cut(kv, "=")
					if got, ok := b.Env[k]; !ok || got != v {
						t.Errorf("the box's %s is %q (set %v), want the image's %q", k, got, ok, v)
					}
				}
			}
		})
	}
}

// A box's environment is the image's with the job's variables over it: nothing of the image's is
// dropped but what the job sets, and the job's value wins.
func TestABoxEnvironmentIsTheImagesWithTheJobsOverIt(t *testing.T) {
	prop := func(image map[string]string, own map[string]string) bool {
		var kvs []string
		for k, v := range image {
			if k == "" || strings.ContainsAny(k, "=") {
				continue
			}
			kvs = append(kvs, k+"="+v)
		}
		got := apptainer.BoxEnv(kvs, own)
		for k, v := range own {
			if got[k] != v {
				return false
			}
		}
		for _, kv := range kvs {
			k, v, _ := strings.Cut(kv, "=")
			if _, overridden := own[k]; !overridden && got[k] != v {
				return false
			}
		}
		want := map[string]bool{}
		for _, kv := range kvs {
			k, _, _ := strings.Cut(kv, "=")
			want[k] = true
		}
		for k := range own {
			want[k] = true
		}
		return len(got) == len(want)
	}
	if err := quick.Check(prop, nil); err != nil {
		t.Error(err)
	}
}
