package hostlock_test

import (
	"math/rand/v2"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/orchestrator/hostlock"
)

func TestARecordReadsBackAsWritten(t *testing.T) {
	rng := rand.New(rand.NewPCG(1, 2))
	for range 200 {
		pid := 1 + rng.IntN(1<<22)
		release := []string{"dev", "1.0.0", "0.2.0-rc.1"}[rng.IntN(3)]
		data, err := hostlock.Marshal(pid, release, "/a/root", "host-a")
		if err != nil {
			t.Fatal(err)
		}
		got, err := hostlock.Parse(data)
		if err != nil || int(got.GetPid()) != pid || got.GetRelease() != release || got.GetRoot() != "/a/root" || got.GetMachine() != "host-a" {
			t.Fatalf("pid %d release %s: read back %v, %v", pid, release, got, err)
		}
	}
}

func TestARecordThatIsNotWholeIsRefused(t *testing.T) {
	if _, err := hostlock.Marshal(0, "1.0.0", "/a/root", ""); err == nil {
		t.Error("a record without a process was written")
	}
	if _, err := hostlock.Marshal(5, "", "/a/root", ""); err == nil {
		t.Error("a record without a release was written")
	}
	for name, text := range map[string]string{
		"empty":         "",
		"old text form": "pid 7 version 1.0.0\n",
		"unknown field": `{"pid":7,"release":"1","colour":"red"}`,
		"no pid":        `{"release":"1"}`,
		"no release":    `{"pid":7}`,
	} {
		if _, err := hostlock.Parse([]byte(text)); err == nil {
			t.Errorf("%s was accepted", name)
		} else if strings.TrimSpace(err.Error()) == "" {
			t.Errorf("%s: empty error", name)
		}
	}
}
