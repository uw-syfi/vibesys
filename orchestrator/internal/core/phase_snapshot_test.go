package core_test

import (
	"bufio"
	"bytes"
	"encoding/hex"
	"os"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/wire"
)

// A log recorded before phases existed is held to what it was: it replays to a state with no violation,
// every run in it reads as optimizing, completing setup on it is refused as not in setup, and the bytes
// of its events are what they were. The golden file is such a log, written as the encoder of the time
// wrote it (a run with no phase field); set UPDATE_PHASE_GOLDEN=1 to write it again.

const prePhaseGolden = "testdata/pre_phase_log.golden"

// prePhaseLog is a short life of two runs, written with no phase on any run.
func prePhaseLog(t *testing.T) []core.Event {
	t.Helper()
	w := newScript(t)
	run, leadID := w.startRunIn("repo-a", "task", 2, steers)
	worker := w.spawn(run, leadID, steers)
	w.command(operator, core.SendMessage{Agent: leadID, Text: "hello"})
	w.endTurn(leadID, core.TurnCompleted)
	w.fact(worker, core.SessionClosed{})
	other, _ := w.startRunIn("repo-b", "task", 1, noSteer)
	w.command(operator, core.StopRun{Run: other})
	var out []core.Event
	for _, e := range w.log {
		switch b := e.Body.(type) {
		case core.Run:
			b.Phase = ""
			e.Body = b
		case core.Receipt:
			// The script's digests are placeholders; the store holds a real one's hex.
			b.Digest = core.Digest(strings.Repeat("a", 64))
			e.Body = b
		}
		out = append(out, e)
	}
	return out
}

func encodeLog(t *testing.T, events []core.Event) []string {
	t.Helper()
	var lines []string
	for i, e := range events {
		data, err := wire.EncodeEvent(uint64(i+1), e)
		if err != nil {
			t.Fatal(err)
		}
		lines = append(lines, hex.EncodeToString(data))
	}
	return lines
}

func TestAnOldLogReplaysToAnOptimizeStateThatCompletingSetupRefuses(t *testing.T) {
	if os.Getenv("UPDATE_PHASE_GOLDEN") != "" {
		if err := os.MkdirAll("testdata", 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(prePhaseGolden, []byte(strings.Join(encodeLog(t, prePhaseLog(t)), "\n")+"\n"), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	data, err := os.ReadFile(prePhaseGolden)
	if err != nil {
		t.Fatalf("the golden log is missing (UPDATE_PHASE_GOLDEN=1 writes it): %v", err)
	}
	var events []core.Event
	var lines []string
	sc := bufio.NewScanner(bytes.NewReader(data))
	sc.Buffer(nil, 1<<20)
	for sc.Scan() {
		raw, err := hex.DecodeString(sc.Text())
		if err != nil {
			t.Fatal(err)
		}
		_, e, err := wire.DecodeEvent(raw)
		if err != nil {
			t.Fatal(err)
		}
		events = append(events, e)
		lines = append(lines, sc.Text())
	}
	if len(events) == 0 {
		t.Fatal("the golden log is empty")
	}
	// The same bytes, written again.
	for i, line := range encodeLog(t, events) {
		if line != lines[i] {
			t.Fatalf("event %d is written differently now: the golden log is no longer what the encoder writes", i+1)
		}
	}

	s := core.Replay(core.State{}, events)
	if v := s.Violations(); len(v) > 0 {
		t.Fatalf("the old log replays to a state with violations: %v", v)
	}
	runs := s.Runs()
	if len(runs) != 2 {
		t.Fatalf("%d runs in the old log", len(runs))
	}
	for _, run := range runs {
		if run.Phase != "" || !run.Phase.Optimizing() || !run.OptimizeStartedAt.IsZero() || run.SetupUsage != nil || run.SetupTask != "" || run.PendingLead != nil {
			t.Fatalf("run %s reads as %+v", run.ID, run)
		}
	}
	// Completing setup on any of them is refused as not in setup, whatever the verdict says.
	next := s
	for i, run := range runs {
		in := core.Input{At: epoch.Add(24 * 3600e9), Nonce: core.Nonce("old" + string(rune('a'+i))), Body: core.Command{
			ID: core.CommandID("complete-" + string(rune('a'+i))), Digest: core.Digest(strings.Repeat("b", 64)), Caller: operator,
			Body: completion(run.ID, "kv"),
		}}
		var evs []core.Event
		next, evs, _ = core.Decide(next, in)
		var receipt core.Receipt
		for _, e := range evs {
			if r, ok := e.Body.(core.Receipt); ok {
				receipt = r
			}
			if _, isRun := e.Body.(core.Run); isRun {
				t.Fatalf("completing setup on an old run wrote %T", e.Body)
			}
		}
		if receipt.Refusal != core.RefuseNotInSetup {
			t.Fatalf("run %s: receipt %+v", run.ID, receipt)
		}
	}
}
