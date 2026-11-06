package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"google.golang.org/protobuf/encoding/protojson"

	"github.com/uw-syfi/vibesys/cli/envcodec"
	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/store/filestore"
)

// placeVariable is the variable of JobToolPlace, from the message.
func placeVariable(t *testing.T) string {
	t.Helper()
	names, err := envcodec.Names(&cliv1.JobToolPlace{})
	if err != nil || len(names) != 1 {
		t.Fatalf("JobToolPlace declares %v, %v", names, err)
	}
	return names[0]
}

// ensureAnswer runs `daemon ensure --json` and reads the answer as a host does.
func ensureAnswer(t *testing.T, m *machine, env []string) *cliv1.DaemonEnsureResponse {
	t.Helper()
	r := m.cliWith(env, "daemon", "ensure", "--json")
	if r.code != exitOK {
		t.Fatalf("ensure --json = %+v", r)
	}
	answer := &cliv1.DaemonEnsureResponse{}
	if err := protojson.Unmarshal([]byte(r.stdout), answer); err != nil {
		t.Fatalf("ensure --json printed %q: %v", r.stdout, err)
	}
	return answer
}

// What a host does and what its agents then meet, with real processes: the answer names the daemon,
// a process told that directory reaches that daemon whatever the rest of its environment says, also
// after the daemon died, when it starts it again over the one store, and it reads its own HOME.
func TestRealAnAgentReachesTheDaemonItWasTold(t *testing.T) {
	m := newMachine(t)
	host := append(m.daemonAt("rt-host", true), filestore.EnvStateHome+"="+filepath.Join(m.root, "state-host"))
	answer := ensureAnswer(t, m, host)
	if answer.GetRuntimeDirectory() != m.runtimeOf("rt-host") || filepath.Dir(answer.GetSocket()) != answer.GetRuntimeDirectory() || answer.GetVersions().GetBinaryVersion() == "" {
		t.Fatalf("the answer is %v", answer)
	}
	if _, err := os.Stat(filepath.Join(m.root, "state-host", "vibesys")); err != nil {
		t.Fatalf("the daemon's store: %v", err)
	}

	// An agent of an operator whose environment points somewhere else altogether.
	elsewhere := filepath.Join(m.root, "state-elsewhere")
	agent := append(m.daemonAt("rt-elsewhere", true),
		placeVariable(t)+"="+answer.GetRuntimeDirectory(), filestore.EnvStateHome+"="+elsewhere, "HOME="+filepath.Join(m.root, "home-agent"))
	pid := m.pidOf(host)
	if got := m.pidOf(agent); got != pid {
		t.Fatalf("the agent reached daemon %d, the host chose %d", got, pid)
	}

	// The daemon dies. An agent's command brings it back, in that directory, over that store.
	if r := m.cliWith(agent, "daemon", "restart"); r.code != exitOK {
		t.Fatalf("restart = %+v", r)
	}
	if got := m.pidOf(host); got == pid || got != m.pidOf(agent) {
		t.Fatalf("after the restart the host reaches %d and the agent %d; the first daemon was %d", got, m.pidOf(agent), pid)
	}
	if _, err := os.Stat(filepath.Join(elsewhere, "vibesys")); !os.IsNotExist(err) {
		t.Fatalf("the agent's own state home was used: %v", err)
	}
	if r := m.cliWith(m.daemonAt("rt-elsewhere", true), "daemon", "status"); r.code != exitNoAnswer {
		t.Fatalf("the operator's own directory has a daemon: %+v", r)
	}
	m.cliWith(agent, "daemon", "stop")
}

// A directory that lost its record, as one under /tmp or /run does: an agent's command says so and
// exits 4, starting nothing, and the host's `daemon ensure` sets it up again.
func TestRealAnAgentStartsNoDaemonWhereTheDirectoryRecordsNothing(t *testing.T) {
	m := newMachine(t)
	host := append(m.daemonAt("rt-host", true), filestore.EnvStateHome+"="+filepath.Join(m.root, "state-host"))
	answer := ensureAnswer(t, m, host)
	if r := m.cliWith(host, "daemon", "stop"); r.code != exitOK {
		t.Fatalf("stop = %+v", r)
	}
	if err := os.Remove(filepath.Join(answer.GetRuntimeDirectory(), "daemon-places.json")); err != nil {
		t.Fatal(err)
	}
	told := placeVariable(t) + "=" + answer.GetRuntimeDirectory()
	agent := append(m.daemonAt("rt-elsewhere", true), told, filestore.EnvStateHome+"="+filepath.Join(m.root, "state-elsewhere"))
	r := m.cliWith(agent, "daemon", "restart")
	if r.code != exitNoAnswer || !strings.Contains(r.stderr, answer.GetRuntimeDirectory()) || !strings.Contains(r.stderr, "daemon ensure") {
		t.Fatalf("restart = %+v, want exit %d naming the directory and the way back", r, exitNoAnswer)
	}
	if s := m.cliWith(agent, "daemon", "status"); s.code != exitNoAnswer {
		t.Fatalf("a daemon runs: %+v", s)
	}
	back := ensureAnswer(t, m, append(host, told))
	if back.GetSocket() != answer.GetSocket() {
		t.Fatalf("the daemon came back at %q, not %q", back.GetSocket(), answer.GetSocket())
	}
	m.cliWith(host, "daemon", "stop")
}

// A daemon started by hand with the plain defaults is what a host's ensure then finds and answers
// with, and the person's own commands keep reaching it after the host told its agents about it.
func TestRealADaemonStartedByHandKeepsItsDefaults(t *testing.T) {
	m := newMachine(t)
	person := m.daemonAt("rt-person", true)
	if r := m.cliWith(person, "daemon", "ensure"); r.code != exitOK {
		t.Fatalf("ensure = %+v", r)
	}
	pid := m.pidOf(person)
	answer := ensureAnswer(t, m, person)
	if m.pidOf(person) != pid {
		t.Fatalf("the host's ensure replaced the person's daemon")
	}
	agent := append(m.daemonAt("rt-elsewhere", true), placeVariable(t)+"="+answer.GetRuntimeDirectory())
	if got := m.pidOf(agent); got != pid {
		t.Fatalf("the agent reached %d, the person's daemon is %d", got, pid)
	}
	m.cliWith(person, "daemon", "stop")
}
