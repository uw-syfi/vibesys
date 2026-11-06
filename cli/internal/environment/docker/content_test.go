package docker_test

import (
	"context"
	"fmt"
	"math/rand/v2"
	"slices"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/environment/docker"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker/dockerfake"
	"github.com/uw-syfi/vibesys/cli/internal/environment/fakeenv/fakeworld"
	"github.com/uw-syfi/vibesys/cli/internal/task"
)

// copyingDockerfile copies one file of the task into its image, and none of the others.
const copyingDockerfile = simpleDockerfile + "COPY bench.py /task/\n"

// Regression: the fingerprint pinned the task's image by its ID, which under Docker's containerd
// image store is a manifest digest that differs from one build of one content to the next. A CLI
// release that changed the image's tag built the image again, and every result after it compared
// as another environment than every result before it.
//
// For any task, an image built again to the same content under another ID (its tag changed by a
// file the Dockerfile does not copy, or the image pruned and built again) is one environment; an
// image of other content is another; and the ID of the earlier build, while the engine still has
// it, resolves to the content the fingerprint pins.
func TestTwoBuildsOfOneContentAreOneEnvironment(t *testing.T) {
	rng := rand.New(rand.NewPCG(7, 11))
	for i := range 20 {
		bench := fmt.Sprintf("print(%d)", rng.Uint64())
		notes := fmt.Sprintf("notes %d", rng.Uint64())
		files := func(bench, notes string) task.Tree {
			return task.Tree{"bench.py": {Data: []byte(bench)}, "notes.txt": {Data: []byte(notes)}}
		}
		w := fakeworld.New()
		daemon := dockerfake.NewDaemon(w.MemoryGates, dockerfake.AttestedBuilds())
		fingerprint := func(process uint32, tree task.Tree) string {
			r := newRig(t, daemon, options{spec: docker.Spec{Dockerfile: []byte(copyingDockerfile), Files: tree}, process: process})
			env := r.env(t)
			f, err := env.Fingerprint(context.Background())
			if err != nil {
				t.Fatalf("case %d: Fingerprint: %v", i, err)
			}
			return f.ImageDigest
		}

		first := fingerprint(1, files(bench, notes))
		firstIDs := daemon.Connect().Images()
		rebuilt := fingerprint(2, files(bench, notes+" changed"))
		if daemon.Builds() != 2 {
			t.Fatalf("case %d: %d builds, want the changed context built again", i, daemon.Builds())
		}
		if ids := daemon.Connect().Images(); len(ids) != 2 || !slices.Contains(ids, firstIDs[0]) {
			t.Fatalf("case %d: images %v after the second build, want two of their own IDs", i, ids)
		}
		if rebuilt != first {
			t.Errorf("case %d: two builds of one content pinned %s and %s; want one environment", i, first, rebuilt)
		}

		daemon.Prune()
		again := fingerprint(3, files(bench, notes))
		if again != first {
			t.Errorf("case %d: the image pruned and built again pinned %s, before %s; want one environment", i, again, first)
		}

		if other := fingerprint(4, files(bench+"\n", notes)); other == first {
			t.Errorf("case %d: an image of another content pinned the same digest %s", i, other)
		}
	}
}

// The ID of an image the engine still has resolves to the content its fingerprint pins; an ID it
// no longer has, a content digest and what is not an ID resolve to nothing.
func TestAnImagesIDResolvesToItsContentWhileTheEngineHasIt(t *testing.T) {
	w := fakeworld.New()
	daemon := dockerfake.NewDaemon(w.MemoryGates, dockerfake.AttestedBuilds())
	r := newRig(t, daemon, options{spec: docker.Spec{Dockerfile: []byte(copyingDockerfile), Files: task.Tree{"bench.py": {Data: []byte("print(1)")}}}})
	env := r.env(t)
	ctx := context.Background()
	f, err := env.Fingerprint(ctx)
	if err != nil {
		t.Fatal(err)
	}
	ids := r.client.Images()
	if len(ids) != 1 || ids[0] == f.ImageDigest {
		t.Fatalf("images %v of the fingerprint %s, want one whose ID is not its content", ids, f)
	}
	if content, found, err := env.ImageContent(ctx, ids[0]); err != nil || !found || content != f.ImageDigest {
		t.Errorf("ImageContent(%s) = %q, %v, %v; want %s", ids[0], content, found, err, f.ImageDigest)
	}
	for _, id := range []string{f.ImageDigest, "busybox:1.36.1", "not an id"} {
		if content, found, err := env.ImageContent(ctx, id); err != nil || found {
			t.Errorf("ImageContent(%q) = %q, %v, %v; want nothing", id, content, found, err)
		}
	}
	daemon.Prune()
	if content, found, err := env.ImageContent(ctx, ids[0]); err != nil || found {
		t.Errorf("ImageContent of a pruned image = %q, %v, %v; want nothing", content, found, err)
	}
}
