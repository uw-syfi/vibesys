package daemon_test

import (
	"errors"
	"strings"
	"testing"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon/daemontest"
)

// failedToStart is what a session says when it cannot open.
func failedToStart(reason pb.StartFailureReason, detail string) []*pb.SidecarEvent {
	return []*pb.SidecarEvent{{Body: &pb.SidecarEvent_Failed{Failed: &pb.SessionFailed{Detail: detail, StartFailureReason: reason}}}}
}

// startFault is one stage of a session's start that fails, and how it shows to the daemon.
type startFault struct {
	name string
	// arm makes the fake host fail that stage.
	arm    func(r *daemontest.Rig)
	reason core.StartFailure
	// detail is text the agent's end detail must hold.
	detail string
	// never says the session never opens, so only the launch timer ends it.
	never bool
}

var startFaults = []startFault{
	{
		name: "the working directory cannot be made",
		arm: func(r *daemontest.Rig) {
			r.Sidecars.LaunchFailures = 1000
			r.Sidecars.LaunchError = &daemon.LaunchError{Reason: core.StartWorkingDirUnavailable, Err: errors.New("create the directory: no space left")}
		},
		reason: core.StartWorkingDirUnavailable, detail: "no space left",
	},
	{
		name: "the process cannot start",
		arm: func(r *daemontest.Rig) {
			r.Sidecars.LaunchFailures = 1000
			r.Sidecars.LaunchError = errors.New("the agent session exited at start")
		},
		reason: core.StartOther, detail: "could not start the agent: the agent session exited at start",
	},
	{
		name: "the agent program is missing",
		arm: func(r *daemontest.Rig) {
			r.Sidecars.Opening = func(core.LaunchSession) []*pb.SidecarEvent {
				return failedToStart(pb.StartFailureReason_START_FAILURE_REASON_AGENT_NOT_INSTALLED, "could not start the agent: claude binary not found in PATH")
			}
		},
		reason: core.StartAgentNotInstalled, detail: "claude binary not found",
	},
	{
		name: "the agent is not signed in",
		arm: func(r *daemontest.Rig) {
			r.Sidecars.Opening = func(core.LaunchSession) []*pb.SidecarEvent {
				return failedToStart(pb.StartFailureReason_START_FAILURE_REASON_NOT_SIGNED_IN, "could not start the agent: please sign in")
			}
		},
		reason: core.StartNotSignedIn, detail: "please sign in",
	},
	{
		name: "the plugin cannot be installed",
		arm: func(r *daemontest.Rig) {
			r.Sidecars.Opening = func(core.LaunchSession) []*pb.SidecarEvent {
				return failedToStart(pb.StartFailureReason_START_FAILURE_REASON_PLUGIN_UNAVAILABLE, "could not start the agent: plugin installation failed")
			}
		},
		reason: core.StartPluginUnavailable, detail: "plugin installation failed",
	},
	{
		name: "the agent has no tools",
		arm: func(r *daemontest.Rig) {
			r.Sidecars.Opening = func(core.LaunchSession) []*pb.SidecarEvent {
				return failedToStart(pb.StartFailureReason_START_FAILURE_REASON_TOOLS_UNAVAILABLE, "could not start the agent: tool server vibesys failed")
			}
		},
		reason: core.StartToolsUnavailable, detail: "tool server vibesys failed",
	},
	{
		name: "the session never opens",
		arm: func(r *daemontest.Rig) {
			r.Sidecars.Opening = func(core.LaunchSession) []*pb.SidecarEvent { return nil }
		},
		reason: core.StartTimedOut, detail: "did not open", never: true,
	},
}

// Whatever stage of the start fails, the run's lead ends failed with that stage's reason, nothing
// of it is left running, and the agent was never shown live.
func TestEveryStageOfAStartThatFailsEndsTheRunWithItsReason(t *testing.T) {
	for _, fault := range startFaults {
		t.Run(fault.name, func(t *testing.T) {
			r := daemontest.NewRig(t)
			fault.arm(r)
			receipt := dispatch(t, r, startRun("c1"))
			r.Settle(t)
			if fault.never {
				r.Clock.Advance(core.LaunchTimeout)
				r.Settle(t)
			}
			run, _ := r.Daemon.State().Run(core.RunID(receipt.GetRunId()))
			lead, _ := r.Daemon.State().Agent(run.Lead)
			if !lead.Status.Over() || lead.StartFailure != fault.reason {
				t.Fatalf("lead %s with reason %q, want over with %q", lead.Status, lead.StartFailure, fault.reason)
			}
			if lead.ConversationID != "" || lead.Capabilities != (core.Capabilities{}) {
				t.Fatalf("lead %+v was shown open", lead)
			}
			if run.Status != core.RunFailed {
				t.Fatalf("run is %s, want failed", run.Status)
			}
			if !strings.Contains(lead.EndDetail, fault.detail) {
				t.Fatalf("end detail %q does not hold %q", lead.EndDetail, fault.detail)
			}
			for _, session := range r.Sidecars.Sessions() {
				if !session.Dead() {
					t.Fatalf("session %v is still running after the start failed", session.Launch.Session)
				}
			}
		})
	}
}

// A session that opens in time is left alone by the timer that was armed for it.
func TestASessionThatOpensInTimeIsLeftAloneByItsLaunchTimer(t *testing.T) {
	r := daemontest.NewRig(t)
	receipt := dispatch(t, r, startRun("c1"))
	r.Settle(t)
	r.Clock.Advance(2 * core.LaunchTimeout)
	r.Settle(t)
	run, _ := r.Daemon.State().Run(core.RunID(receipt.GetRunId()))
	lead, _ := r.Daemon.State().Agent(run.Lead)
	if lead.Status.Over() || lead.StartFailure != "" {
		t.Fatalf("lead %+v, want it left alone after it opened", lead)
	}
}

// A daemon that restarts while a session is still launching ends with one clear failure, and no
// session is left running.
func TestARestartDuringALaunchEndsInOneOutcome(t *testing.T) {
	r := daemontest.NewRig(t)
	r.Sidecars.Opening = func(core.LaunchSession) []*pb.SidecarEvent { return nil }
	receipt := dispatch(t, r, startRun("c1"))
	r.Settle(t)
	r.Restart(t)
	r.Settle(t)
	r.Clock.Advance(core.LaunchTimeout)
	r.Settle(t)
	run, _ := r.Daemon.State().Run(core.RunID(receipt.GetRunId()))
	lead, _ := r.Daemon.State().Agent(run.Lead)
	if lead.Status != core.AgentFailed || lead.StartFailure != core.StartTimedOut || run.Status != core.RunFailed {
		t.Fatalf("lead %s/%q and run %s, want a failed lead that timed out", lead.Status, lead.StartFailure, run.Status)
	}
	for _, session := range r.Sidecars.Sessions() {
		if !session.Dead() {
			t.Fatal("a session outlived the failed launch")
		}
	}
}
