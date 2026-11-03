package daemon_test

import (
	"testing"

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon/daemontest"
)

func locationA() core.CliLocation {
	return core.CliLocation{CLI: "/opt/a/vibesys-cli", RuntimeDir: "/run/user/1", StateHome: "/state/a", Home: "/home/a"}
}

func locationB() core.CliLocation {
	return core.CliLocation{CLI: "/opt/b/vibesys-cli", RuntimeDir: "/run/user/2", StateHome: "/state/b", Home: "/home/b"}
}

// The vibesys-cli an agent runs, and the daemon and store it talks to, are the ones recorded when
// the run started: every session of the run is launched with them, also a session resumed after the
// daemon restarted with another environment, and a run that starts later records the new ones.
func TestEverySessionOfARunIsLaunchedWithTheLocationItRecorded(t *testing.T) {
	r := daemontest.NewRig(t)
	r.Location = locationA()
	r.Restart(t)
	first, lead := beginRun(t, r)
	if first.Location != locationA() {
		t.Fatalf("the run recorded %+v, want %+v", first.Location, locationA())
	}
	r.Settle(t)
	r.Sidecars.Session(core.Session{Agent: lead.ID, N: 1}).Die()
	r.Location = locationB()
	r.Restart(t)
	r.Settle(t)
	sessions := r.Sidecars.Sessions()
	if len(sessions) != 2 {
		t.Fatalf("%d sessions, want the lead's two", len(sessions))
	}
	for _, s := range sessions {
		got := s.Launch.Location
		if got != locationA() || s.Launch.Task != first.Task {
			t.Errorf("session %s/%d was launched with %+v for task %q, want %+v for %q",
				s.Launch.Session.Agent, s.Launch.Session.N, got, s.Launch.Task, locationA(), first.Task)
		}
	}
}

// A daemon with no location to give records none, and the session is launched with none: the
// skills say what to do then, and a daemon never hands out a guess.
func TestARunStartedWithoutALocationRecordsNone(t *testing.T) {
	r := daemontest.NewRig(t)
	run, lead := beginRun(t, r)
	if run.Location != (core.CliLocation{}) {
		t.Fatalf("the run recorded %+v with none given", run.Location)
	}
	if got := r.Sidecars.Session(core.Session{Agent: lead.ID, N: 1}).Launch.Location; got != (core.CliLocation{}) {
		t.Fatalf("the session was launched with %+v with none recorded", got)
	}
}
