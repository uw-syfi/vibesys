package server_test

import (
	"context"
	"crypto/sha256"
	"fmt"
	"os"
	"path/filepath"
	"syscall"
	"testing"
	"time"

	"buf.build/go/protovalidate"
	apiv1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/api/v1"
	storev1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/store/v1"
	"google.golang.org/protobuf/proto"
)

// Addresses do not depend on the presence, health or writer generation of run.head.
func TestWatchRunsCarriesPublishedProject(t *testing.T) {
	for variant := 0; variant < 8; variant++ {
		t.Run(fmt.Sprint(variant), func(t *testing.T) {
			ticker := &fakeTicker{ticks: make(chan time.Time, 1), stopped: make(chan struct{})}
			f := startWithClock(t, &fakeClock{ticker: ticker})
			project := externalFixture(t, &f, variant)
			lock, err := os.OpenFile(filepath.Join(f.run, "run.lock"), os.O_CREATE|os.O_RDWR, 0600)
			if err != nil {
				t.Fatal(err)
			}
			t.Cleanup(func() { lock.Close() })
			if err := syscall.Flock(int(lock.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
				t.Fatal(err)
			}
			directory, err := filepath.EvalSymlinks(f.run)
			if err != nil {
				t.Fatal(err)
			}
			if err := os.MkdirAll(filepath.Join(f.root, "run-addresses"), 0700); err != nil {
				t.Fatal(err)
			}
			address, err := proto.Marshal(&storev1.RunAddress{ProjectRoot: project, LogDirectory: directory})
			if err != nil {
				t.Fatal(err)
			}
			write(t, filepath.Join(f.root, "run-addresses", "run-1.pb"), false, address)
			if variant%2 == 0 {
				write(t, filepath.Join(f.run, "run.head"), false, []byte{255})
			}
			ctx, cancel := context.WithCancel(f.ctx)
			defer cancel()
			stream, err := watchRuns(ctx, client(t, f, false))
			if err != nil {
				t.Fatal(err)
			}
			got := watchEntry(t, stream)
			if got.GetProjectRoot() != project {
				t.Fatalf("project = %q, want %q", got.GetProjectRoot(), project)
			}
			if err := protovalidate.Validate(got); err != nil {
				t.Fatal(err)
			}
			if !watchEntry(t, stream).InitialComplete {
				t.Fatal("missing initial marker")
			}
			if err := lock.Close(); err != nil {
				t.Fatal(err)
			}
			ticker.ticks <- time.Time{}
			changed := watchEntry(t, stream)
			if changed.Liveness != apiv1.RunLiveness_RUN_LIVENESS_INTERRUPTED || changed.GetProjectRoot() != project {
				t.Fatalf("a liveness update lost its project address: %v", changed)
			}
		})
	}
}

func TestWatchRunsNeverPublishesAnEmptyOrRelativeAddress(t *testing.T) {
	for _, project := range []string{"", "relative"} {
		f := start(t)
		directory, err := filepath.EvalSymlinks(f.run)
		if err != nil {
			t.Fatal(err)
		}
		if err := os.MkdirAll(filepath.Join(f.root, "run-addresses"), 0700); err != nil {
			t.Fatal(err)
		}
		address, err := proto.Marshal(&storev1.RunAddress{ProjectRoot: project, LogDirectory: directory})
		if err != nil {
			t.Fatal(err)
		}
		write(t, filepath.Join(f.root, "run-addresses", "run-1.pb"), false, address)
		runs := listing(t, f, client(t, f, false))
		if runs["run-1"].ProjectRoot != nil {
			t.Fatal("published an invalid project")
		}
	}
	for _, project := range []string{"", "relative"} {
		response := &apiv1.RunEntry{ProjectRoot: proto.String(project)}
		if err := protovalidate.Validate(response); err == nil {
			t.Fatalf("schema accepted %q", project)
		}
	}
}

// Republishing only routing metadata wakes an existing stream even when its index link is unchanged.
func TestWatchRunsObservesAddressBackfillAndRejectsStaleTargets(t *testing.T) {
	f := start(t)
	project := externalFixture(t, &f, 0)
	if err := os.Mkdir(filepath.Join(f.root, "run-addresses"), 0700); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(f.ctx)
	defer cancel()
	stream, err := watchRuns(ctx, client(t, f, false))
	if err != nil {
		t.Fatal(err)
	}
	if watchEntry(t, stream).ProjectRoot != nil {
		t.Fatal("address already present")
	}
	if !watchEntry(t, stream).InitialComplete {
		t.Fatal("missing initial marker")
	}
	directory, err := filepath.EvalSymlinks(f.run)
	if err != nil {
		t.Fatal(err)
	}
	data, err := proto.Marshal(&storev1.RunAddress{ProjectRoot: project, LogDirectory: directory})
	if err != nil {
		t.Fatal(err)
	}
	publishAddress(t, f, data)
	if got := watchEntry(t, stream); got.GetProjectRoot() != project {
		t.Fatalf("backfill not delivered: %v", got)
	}
	stale, err := proto.Marshal(&storev1.RunAddress{ProjectRoot: "/other-project", LogDirectory: "/other-logs"})
	if err != nil {
		t.Fatal(err)
	}
	publishAddress(t, f, stale)
	// A committed head is the receipt: inspect every intervening frame, rather
	// than accepting an earlier liveness update as evidence about the sidecar.
	head, err := proto.Marshal(&storev1.RunHead{Strategy: "search", Epoch: 1, CommittedSeq: proto.Uint64(0), PerformanceStated: true, AwaitingLifecycle: true})
	if err != nil {
		t.Fatal(err)
	}
	write(t, filepath.Join(f.run, "run.head"), false, head)
	for {
		got := watchEntry(t, stream)
		if got.GetProjectRoot() != project {
			t.Fatalf("lost validated location on stale sidecar: %v", got)
		}
		if got.GetHead().GetEpoch() == 1 {
			break
		}
	}

}

func publishAddress(t *testing.T, f fixture, data []byte) {
	t.Helper()
	staged := filepath.Join(f.root, "run-addresses", "staged.pb")
	write(t, staged, false, data)
	if err := os.Rename(staged, filepath.Join(f.root, "run-addresses", "run-1.pb")); err != nil {
		t.Fatal(err)
	}
}

func externalFixture(t *testing.T, f *fixture, variant int) string {
	t.Helper()
	base, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	project := filepath.Join(base, fmt.Sprintf("project-%d", variant))
	if err := os.Mkdir(project, 0700); err != nil {
		t.Fatal(err)
	}
	digest := sha256.Sum256([]byte(project))
	directory := filepath.Join(f.root, "projects", fmt.Sprintf("project-%d-%x", variant, digest[:6]), "runs", "run-1", "logs")
	if err := os.MkdirAll(directory, 0700); err != nil {
		t.Fatal(err)
	}
	link := filepath.Join(f.root, "runs", "run-1")
	if err := os.Remove(link); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(directory, link); err != nil {
		t.Fatal(err)
	}
	f.run = directory
	return project
}

// A syntactically correct external key cannot make state inside a project routable.
// Project's forward layout rejects these roots as well.
func TestWatchRunsRejectsExternalStateInsideRecordedProject(t *testing.T) {
	for depth := 0; depth < 3; depth++ {
		f := start(t)
		home, err := filepath.EvalSymlinks(f.root)
		if err != nil {
			t.Fatal(err)
		}
		root := home
		for i := 0; i < depth; i++ {
			root = filepath.Dir(root)
		}
		digest := sha256.Sum256([]byte(root))
		slug := filepath.Base(root)
		if root == filepath.Dir(root) {
			slug = "project"
		}
		directory := filepath.Join(home, "projects", fmt.Sprintf("%s-%x", slug, digest[:6]), "runs", "run-1", "logs")
		if err := os.MkdirAll(directory, 0700); err != nil {
			t.Fatal(err)
		}
		link := filepath.Join(home, "runs", "run-1")
		if err := os.Remove(link); err != nil {
			t.Fatal(err)
		}
		if err := os.Symlink(directory, link); err != nil {
			t.Fatal(err)
		}
		if err := os.MkdirAll(filepath.Join(home, "run-addresses"), 0700); err != nil {
			t.Fatal(err)
		}
		address, err := proto.Marshal(&storev1.RunAddress{ProjectRoot: root, LogDirectory: directory})
		if err != nil {
			t.Fatal(err)
		}
		write(t, filepath.Join(home, "run-addresses", "run-1.pb"), false, address)
		if got := listing(t, f, client(t, f, false))["run-1"]; got.ProjectRoot != nil {
			t.Fatalf("routed an invalid external layout: %v", got)
		}
	}
}
