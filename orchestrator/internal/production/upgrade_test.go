package production_test

import (
	"context"
	"database/sql"
	"path/filepath"
	"testing"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon/daemontest"
	"github.com/uw-syfi/vibesys/orchestrator/internal/localfs"
	"github.com/uw-syfi/vibesys/orchestrator/internal/localfs/localfstest"
	"github.com/uw-syfi/vibesys/orchestrator/internal/store"
	"github.com/uw-syfi/vibesys/orchestrator/internal/wire"
	"google.golang.org/protobuf/proto"
)

func TestIdentityUpgradeDeliversBothRunsAndAttachesExistingSessions(t *testing.T) {
	for _, broken := range []bool{false, true} {
		t.Run(map[bool]string{false: "valid", true: "unmigratable"}[broken], func(t *testing.T) {
			r := daemontest.NewRig(t)
			first := dispatchUpgrade(t, r, startUpgrade("existing"))
			r.Settle(t)
			existing, _ := r.Daemon.State().Run(core.RunID(first.RunId))
			lead, _ := r.Daemon.State().Agent(existing.Lead)
			r.Settle(t)
			old := dispatchUpgrade(t, r, startUpgrade("old"))
			other := dispatchUpgrade(t, r, startUpgrade("other"))
			pending, err := r.Store.Claim(r.Clock.Now(), 100)
			if err != nil {
				t.Fatal(err)
			}
			dir, err := localfs.Resolve(t.TempDir(), localfstest.Fake{})
			if err != nil {
				t.Fatal(err)
			}
			disk, err := store.OpenSQLite(dir, store.SQLiteOptions{})
			if err != nil {
				t.Fatal(err)
			}
			if _, err := disk.Commit(store.Result{Events: r.Daemon.State().Checkpoint()}); err != nil {
				t.Fatal(err)
			}
			if err := disk.Close(); err != nil {
				t.Fatal(err)
			}
			// This is the old on-disk format, not a call through the new strict write API.
			db, err := sql.Open("sqlite", filepath.Join(dir.Path(), store.DatabaseFile))
			if err != nil {
				t.Fatal(err)
			}
			for _, queued := range pending {
				data, err := wire.EncodeEffect(queued.Effect)
				if err != nil {
					t.Fatal(err)
				}
				var effect pb.Effect
				if err := proto.Unmarshal(data, &effect); err != nil {
					t.Fatal(err)
				}
				if launch := effect.GetLaunchSession(); launch != nil && launch.RunId == old.RunId {
					launch.Task, launch.CliLocation = "", nil
					if broken {
						launch.Repository = ""
					}
					data, err = proto.Marshal(&effect)
					if err != nil {
						t.Fatal(err)
					}
					if _, err := wire.DecodeEffect(data); err == nil {
						t.Fatal("new launches must reject legacy input")
					}
				}
				if _, err := db.Exec(`INSERT INTO outbox(effect_id, effect, state, run_after) VALUES (?, ?, 0, 0)`, string(queued.Effect.ID()), data); err != nil {
					t.Fatal(err)
				}
			}
			if _, err := db.Exec(`UPDATE meta SET value = 2 WHERE key = 'schema_version'`); err != nil {
				t.Fatal(err)
			}
			if err := db.Close(); err != nil {
				t.Fatal(err)
			}
			disk, err = store.OpenSQLite(dir, store.SQLiteOptions{})
			if err != nil {
				t.Fatal(err)
			}
			defer disk.Close()
			d, err := daemon.Open(daemon.Config{Engine: daemon.EngineDeps{Store: disk, Clock: r.Clock, Nonces: r.Nonces, Hub: r.Hub, Location: core.CliLocation{CLI: "/opt/vibesys-cli"}}, Sidecars: r.Sidecars, Bounds: r.Bounds, Authority: r.Auth})
			if err != nil {
				t.Fatal(err)
			}
			if err := d.Reattach(context.Background()); err != nil {
				t.Fatal(err)
			}
			for range 12 {
				if _, err := d.Step(context.Background()); err != nil {
					t.Fatal(err)
				}
			}
			for _, id := range []core.RunID{existing.ID, core.RunID(old.RunId), core.RunID(other.RunId)} {
				run, _ := d.State().Run(id)
				if broken && id == core.RunID(old.RunId) {
					if run.Status != core.RunFailed {
						t.Fatalf("unmigratable run: %+v", run)
					}
					agent, _ := d.State().Agent(run.Lead)
					if agent.Status != core.AgentFailed || agent.EndDetail == "" {
						t.Fatalf("missing visible failure: %+v", agent)
					}
					continue
				}
				if run.Status != core.RunRunning {
					t.Fatalf("run not delivered: %+v", run)
				}
				agent, _ := d.State().Agent(run.Lead)
				if agent.Session != 1 {
					t.Fatalf("existing session was not attached: %+v", agent)
				}
				session := r.Sidecars.Session(core.Session{Agent: agent.ID, N: 1})
				if session == nil || session.Launch.Task != run.Task {
					t.Fatalf("missing migrated launch for %s", id)
				}
			}
			if session := r.Sidecars.Session(core.Session{Agent: lead.ID, N: 1}); session == nil {
				t.Fatal("existing session disappeared")
			}
			if err := disk.Verify(); err != nil {
				t.Fatal(err)
			}
		})
	}
}

func startUpgrade(id string) *pb.Command {
	return &pb.Command{Id: id, Body: &pb.Command_StartRun{StartRun: &pb.StartRun{Repository: "/repo-" + id, Task: "task", FanoutLimit: 1, Brief: "go", Lead: &pb.AgentSpec{Harness: pb.Harness_HARNESS_CLAUDE_CODE}}}}
}
func dispatchUpgrade(t *testing.T, r *daemontest.Rig, c *pb.Command) *pb.Receipt {
	t.Helper()
	response, err := r.Service.Dispatch(context.Background(), &pb.DispatchRequest{Command: c})
	if err != nil {
		t.Fatal(err)
	}
	return response.Receipt
}
