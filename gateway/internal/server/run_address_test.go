package server_test

import (
	"context"
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
			lock, err := os.OpenFile(filepath.Join(f.run, "run.lock"), os.O_CREATE|os.O_RDWR, 0600)
			if err != nil {
				t.Fatal(err)
			}
			t.Cleanup(func() { lock.Close() })
			if err := syscall.Flock(int(lock.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
				t.Fatal(err)
			}
			project := fmt.Sprintf("/projects/project %d", variant)
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
			stream, err := client(t, f, false).WatchRuns(ctx, &apiv1.WatchRunsRequest{})
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
		response := &apiv1.WatchRunsResponse{RunId: "run-1", ProjectRoot: proto.String(project)}
		if err := protovalidate.Validate(response); err == nil {
			t.Fatalf("schema accepted %q", project)
		}
	}
}

// Republishing only routing metadata wakes an existing stream even when its index link is unchanged.
func TestWatchRunsObservesAddressBackfillAndRejectsStaleTargets(t *testing.T) {
	f := start(t)
	if err := os.Mkdir(filepath.Join(f.root, "run-addresses"), 0700); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(f.ctx)
	defer cancel()
	stream, err := client(t, f, false).WatchRuns(ctx, &apiv1.WatchRunsRequest{})
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
	data, err := proto.Marshal(&storev1.RunAddress{ProjectRoot: "/project", LogDirectory: directory})
	if err != nil {
		t.Fatal(err)
	}
	publishAddress(t, f, data)
	if got := watchEntry(t, stream); got.GetProjectRoot() != "/project" {
		t.Fatalf("backfill not delivered: %v", got)
	}
	stale, err := proto.Marshal(&storev1.RunAddress{ProjectRoot: "/other-project", LogDirectory: "/other-logs"})
	if err != nil {
		t.Fatal(err)
	}
	publishAddress(t, f, stale)
	if got := watchEntry(t, stream); got.ProjectRoot != nil {
		t.Fatalf("misrouted stale index address: %v", got)
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
