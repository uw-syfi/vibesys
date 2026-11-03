package daemon_test

import (
	"fmt"
	"testing"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon/daemontest"
)

// The vibesys-cli an agent runs is the one the daemon chose, for every agent and every session of
// it, not whichever its shell finds first on a PATH it re-reads from the user's profile.
func TestEverySessionIsToldTheCLIThatTheDaemonChose(t *testing.T) {
	const cli = "/opt/vibesys/bin/vibesys-cli"
	r := daemontest.NewRig(t)
	r.AgentCLI = cli
	r.Restart(t)
	run, lead := beginRun(t, r)
	token := r.Sidecars.Session(core.Session{Agent: lead.ID, N: 1}).Env[daemon.EnvToken]
	for i, role := range []pb.AgentRole{pb.AgentRole_AGENT_ROLE_WORKER} {
		resp, err := r.AgentService.Dispatch(agentContext(lead.ID, 1, token), &pb.DispatchRequest{Command: &pb.Command{
			Id: fmt.Sprintf("spawn-%d", i), Body: &pb.Command_SpawnAgent{SpawnAgent: &pb.SpawnAgent{RunId: string(run.ID), Role: role, Brief: "work"}},
		}})
		if err != nil || resp.GetReceipt().GetRefusal() != pb.Refusal_REFUSAL_UNSPECIFIED {
			t.Fatalf("spawn %d: %v, %v", i, resp, err)
		}
	}
	// The lead's sidecar dies, and its session is resumed: the new session is told as well.
	r.Settle(t)
	r.Sidecars.Session(core.Session{Agent: lead.ID, N: 1}).Die()
	r.Restart(t)
	r.Settle(t)
	sessions := r.Sidecars.Sessions()
	if len(sessions) != 3 {
		t.Fatalf("%d sessions, want the lead's two and the worker's one", len(sessions))
	}
	for _, s := range sessions {
		if got := s.Env[daemon.EnvCLI]; got != cli {
			t.Errorf("session %s/%d is told %q, want %q", s.Launch.Session.Agent, s.Launch.Session.N, got, cli)
		}
	}
}

// With no CLI chosen the name is absent, and not an empty value an agent would run: the skills say
// what to do then, and a daemon never hands out a guess.
func TestNoCLIIsToldWhenTheDaemonChoseNone(t *testing.T) {
	r := daemontest.NewRig(t)
	_, lead := beginRun(t, r)
	env := r.Sidecars.Session(core.Session{Agent: lead.ID, N: 1}).Env
	if v, set := env[daemon.EnvCLI]; set {
		t.Fatalf("the session is told CLI %q although none was chosen", v)
	}
}
