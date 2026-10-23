package server_test

import (
	"fmt"
	"os"
	"path/filepath"
	"testing"

	apiv1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/api/v1"
	storev1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/store/v1"
	"github.com/uw-syfi/vibesys/gateway/internal/runlog"
	"google.golang.org/protobuf/proto"
)

// Generated layouts cover absent/present addresses, absent/retained/current heads,
// project names with spaces, multiple epochs, and a root-free successor header.
func TestWatchRunsRecoversPreviousPublisherProject(t *testing.T) {
	for seed := 0; seed < 24; seed++ {
		t.Run(fmt.Sprint(seed), func(t *testing.T) {
			f := start(t)
			base, err := filepath.EvalSymlinks(t.TempDir())
			if err != nil {
				t.Fatal(err)
			}
			project := filepath.Join(base, fmt.Sprintf("project %d", seed))
			directory := filepath.Join(project, ".vibesys", "state", "local", "runs", "run-1", "logs")
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
			first := &storev1.Record{Seq: 1, Epoch: 1, Body: &storev1.Record_EpochStarted{EpochStarted: &storev1.EpochStarted{Epoch: 1, ProjectRoot: proto.String(project)}}}
			second := &storev1.Record{Seq: 2, Epoch: 2, Body: &storev1.Record_EpochStarted{EpochStarted: &storev1.EpochStarted{Epoch: 2}}}
			data := append([]byte(runlog.Magic), frame(encode(t, first))...)
			data = append(data, frame(encode(t, second))...)
			write(t, filepath.Join(directory, "run.log"), false, data)
			if seed%3 != 0 {
				head := &storev1.RunHead{Strategy: "search", Epoch: 2, CommittedSeq: proto.Uint64(2), AwaitingLifecycle: true}
				if seed%3 == 1 {
					head.Epoch = 0
					head.AwaitingLifecycle = false
					head.Lifecycle = &storev1.Lifecycle{State: &storev1.Lifecycle_Running{Running: &storev1.Running{}}}
				}
				data, err := proto.Marshal(head)
				if err != nil {
					t.Fatal(err)
				}
				write(t, filepath.Join(directory, "run.head"), false, data)
			}
			if seed%2 != 0 {
				if err := os.MkdirAll(filepath.Join(f.root, "run-addresses"), 0700); err != nil {
					t.Fatal(err)
				}
				address, err := proto.Marshal(&storev1.RunAddress{ProjectRoot: project, LogDirectory: directory})
				if err != nil {
					t.Fatal(err)
				}
				write(t, filepath.Join(f.root, "run-addresses", "run-1.pb"), false, address)
			}
			got := listing(t, f, client(t, f, false))["run-1"]
			if got == nil || got.GetProjectRoot() != project {
				t.Fatalf("seed %d: project = %v, want %q", seed, got, project)
			}
			if got.Liveness == apiv1.RunLiveness_RUN_LIVENESS_ALIVE {
				t.Fatal("no writer holds this run")
			}
			if seed%2 == 0 {
				if _, err := os.Stat(filepath.Join(f.root, "run-addresses", "run-1.pb")); !os.IsNotExist(err) {
					t.Fatalf("read rewrote old publication: %v", err)
				}
			}
		})
	}
}
