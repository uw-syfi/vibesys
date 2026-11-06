package store_test

import (
	"fmt"
	"math/rand/v2"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/localfs"
	"github.com/uw-syfi/vibesys/orchestrator/internal/localfs/localfstest"
	"github.com/uw-syfi/vibesys/orchestrator/internal/store"
)

// TestMeasureScale measures the file-backed store with generated data. It asserts nothing about
// speed, since a result must never depend on timing: it reports numbers, and it runs only when
// ORCHESTRATOR_STORE_SCALE names how many items to generate:
//
//	ORCHESTRATOR_STORE_SCALE=1000000 go test ./internal/store -run TestMeasureScale -v -timeout 30m
//
// The data is 100 agents in each of 10 runs, items of about 700 bytes, and messages in every run.
func TestMeasureScale(t *testing.T) {
	v := os.Getenv("ORCHESTRATOR_STORE_SCALE")
	if v == "" {
		t.Skip("set ORCHESTRATOR_STORE_SCALE to the number of items to generate")
	}
	total, err := strconv.Atoi(v)
	if err != nil {
		t.Fatal(err)
	}
	dirPath := t.TempDir()
	dir, err := localfs.Resolve(dirPath, localfstest.Fake{})
	if err != nil {
		t.Fatal(err)
	}
	s := openSQLite(t, dir, store.SQLiteOptions{})
	r := rand.New(rand.NewPCG(1, 2))
	at := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	const runs, agentsPerRun = 10, 100
	agents := runs * agentsPerRun
	perAgent := total / agents

	report := func(format string, args ...any) { t.Logf(format, args...) }
	timed := func(f func()) time.Duration {
		start := time.Now()
		f()
		return time.Since(start)
	}
	size := func() string {
		var db, wal int64
		if fi, err := os.Stat(filepath.Join(dirPath, store.DatabaseFile)); err == nil {
			db = fi.Size()
		}
		if fi, err := os.Stat(filepath.Join(dirPath, store.DatabaseFile+"-wal")); err == nil {
			wal = fi.Size()
		}
		return fmt.Sprintf("db %.0f MB, wal %.0f MB", float64(db)/1e6, float64(wal)/1e6)
	}
	commit := func(events ...core.Event) {
		if _, err := s.Commit(store.Result{Events: events}); err != nil {
			t.Fatal(err)
		}
	}
	body := func() string { return strings.Repeat("o", 500+r.IntN(400)) }
	agentID := func(a int) core.AgentID { return core.AgentID(fmt.Sprintf("agent-%04d", a)) }
	runID := func(a int) core.RunID { return core.RunID(fmt.Sprintf("run-%02d", a/agentsPerRun)) }

	// Structure: runs, agents, messages.
	var batch []core.Event
	flush := func() {
		if len(batch) > 0 {
			commit(batch...)
			batch = batch[:0]
		}
	}
	for i := range runs {
		batch = append(batch, core.Event{Run: core.RunID(fmt.Sprintf("run-%02d", i)), At: at, Body: core.Run{
			ID: core.RunID(fmt.Sprintf("run-%02d", i)), Repository: "r", Task: "t", Status: core.RunRunning, Lead: agentID(i * agentsPerRun), CreatedAt: at,
		}})
	}
	for a := range agents {
		batch = append(batch, core.Event{Run: runID(a), At: at, Body: core.Agent{
			ID: agentID(a), Run: runID(a), Role: core.RoleWorker, Spec: core.AgentSpec{Harness: core.ClaudeCode},
			Status: core.AgentIdle, Stop: core.StopNone, StartedAt: at, Session: 1,
		}})
	}
	flush()
	for i := range 2000 * runs {
		run := core.RunID(fmt.Sprintf("run-%02d", i%runs))
		batch = append(batch, core.Event{Run: run, At: at, Body: core.Message{
			ID: core.MessageID(fmt.Sprintf("message-%d", i)), Run: run, To: agentID((i % runs) * agentsPerRun), Delivery: core.DeliveryConsumed,
			At: at, Body: core.Text{Text: "steer"},
		}})
		if len(batch) == 500 {
			flush()
		}
	}
	flush()

	// Items: 20 turns per agent, items spread over them.
	turnsPer := 20
	generate := timed(func() {
		for a := range agents {
			turn := 0
			for i := 1; i <= perAgent; i++ {
				if (i-1)%(perAgent/turnsPer+1) == 0 {
					turn++
					batch = append(batch, core.Event{Run: runID(a), At: at, Body: core.Turn{
						ID: core.TurnID(fmt.Sprintf("turn-%d-%d", a, turn)), Agent: agentID(a), Ordinal: uint32(turn), Trigger: core.TriggerMessage,
						Message: "m", Status: core.TurnCompleted, StartedAt: at.Add(time.Duration(turn) * time.Minute), EndedAt: at.Add(time.Duration(turn)*time.Minute + 50*time.Second),
					}})
				}
				batch = append(batch, core.Event{Run: runID(a), At: at, Body: core.Item{
					ID: core.ItemID(fmt.Sprintf("item-%d-%d", a, i)), Turn: core.TurnID(fmt.Sprintf("turn-%d-%d", a, turn)), Agent: agentID(a),
					Ordinal: uint32(i), Status: core.ItemFinished, At: at, Body: core.ToolCall{Use: core.ShellUse{Command: "ls"}, Output: body()},
				}})
				if len(batch) >= 500 {
					flush()
				}
			}
		}
		flush()
	})
	report("generated %d items in %v (batches of 500 events): %.0f items/s; %s", agents*perAgent, generate.Round(time.Millisecond), float64(agents*perAgent)/generate.Seconds(), size())

	// Single-event commits, as a sidecar fact commits them.
	const singles = 5000
	var lat []time.Duration
	for i := range singles {
		a := i % agents
		e := core.Event{Run: runID(a), At: at, Body: core.Item{
			ID: core.ItemID(fmt.Sprintf("late-%d", i)), Turn: "turn-late", Agent: agentID(a), Ordinal: uint32(perAgent + 1 + i/agents), Status: core.ItemFinished, At: at,
			Body: core.ToolCall{Use: core.ShellUse{Command: "ls"}, Output: body()},
		}}
		lat = append(lat, timed(func() { commit(e) }))
	}
	report("single-event commit: p50 %v p99 %v max %v", pct(lat, 50), pct(lat, 99), pct(lat, 100))

	sample := func(name string, f func(i int)) {
		var d []time.Duration
		for i := range 300 {
			d = append(d, timed(func() { f(i) }))
		}
		report("%-34s p50 %v p99 %v", name, pct(d, 50), pct(d, 99))
	}
	reads := func(tag string) {
		sample(tag+" newest 75 items", func(i int) {
			if p, err := s.Items(agentID(r.IntN(agents)), 0, 0); err != nil || len(p.Items) == 0 {
				t.Fatal(err)
			}
		})
		sample(tag+" older page (before 400)", func(i int) {
			if p, err := s.Items(agentID(r.IntN(agents)), 400, 0); err != nil || len(p.Items) == 0 {
				t.Fatal(err)
			}
		})
		end, err := s.Snapshot()
		if err != nil {
			t.Fatal(err)
		}
		sample(tag+" events of a run after tail", func(i int) {
			if _, err := s.EventsAfter(runID(r.IntN(agents)), end.Sequence, 100); err != nil {
				t.Fatal(err)
			}
		})
		sample(tag+" events of a run, mid-log", func(i int) {
			if p, err := s.EventsAfter(runID(r.IntN(agents)), uint64(total/2), 100); err != nil || len(p) == 0 {
				t.Fatal(err)
			}
		})
		sample(tag+" messages page", func(i int) {
			if _, err := s.Messages(core.RunID(fmt.Sprintf("run-%02d", r.IntN(runs))), uint64(r.IntN(900)), 200); err != nil {
				t.Fatal(err)
			}
		})
		sample(tag+" turns window", func(i int) {
			if _, err := s.Turns(core.RunID(fmt.Sprintf("run-%02d", r.IntN(runs))), at.Add(5*time.Minute), at.Add(8*time.Minute), 0); err != nil {
				t.Fatal(err)
			}
		})
		sample(tag+" snapshot", func(i int) {
			if _, err := s.Snapshot(); err != nil {
				t.Fatal(err)
			}
		})
	}
	reads("warm")

	reopen := func() {
		if err := s.Close(); err != nil {
			t.Fatal(err)
		}
		var d time.Duration
		d = timed(func() { s = openSQLite(t, dir, store.SQLiteOptions{}) })
		report("open: %v", d.Round(time.Millisecond))
	}
	reopen()
	var state core.State
	d := timed(func() {
		var err error
		if state, err = s.Recover(); err != nil {
			t.Fatal(err)
		}
	})
	report("recover from the whole log (no checkpoint): %v", d.Round(time.Millisecond))
	compaction := timed(func() {
		if _, err := s.Compact(store.Retention{}, at.Add(1000*time.Hour)); err != nil {
			t.Fatal(err)
		}
	})
	report("compact (checkpoint of %d events): %v; %s", len(state.Checkpoint()), compaction.Round(time.Millisecond), size())
	reopen()
	d = timed(func() {
		if _, err := s.Recover(); err != nil {
			t.Fatal(err)
		}
	})
	report("recover from a checkpoint with an empty tail: %v", d.Round(time.Millisecond))
	for i := range 10_000 {
		commit(core.Event{Run: runID(i % agents), At: at, Body: core.Item{
			ID: core.ItemID(fmt.Sprintf("tail-%d", i)), Turn: "turn-tail", Agent: agentID(i % agents), Ordinal: uint32(perAgent + 100 + i/agents), Status: core.ItemFinished, At: at,
			Body: core.ToolCall{Use: core.ShellUse{Command: "ls"}, Output: body()},
		}})
	}
	reopen()
	d = timed(func() {
		if _, err := s.Recover(); err != nil {
			t.Fatal(err)
		}
	})
	report("recover from a checkpoint and a tail of 10000 events: %v", d.Round(time.Millisecond))
	reads("cold")
	// Commits go on beside a Compact and a Verify; the slowest of them is the pause a client sees.
	stall := func(name string, f func()) {
		stop, done := make(chan struct{}), make(chan [3]time.Duration)
		go func() {
			var worst, total time.Duration
			n := 0
			for {
				select {
				case <-stop:
					done <- [3]time.Duration{worst, total / time.Duration(max(n, 1)), time.Duration(n)}
					return
				default:
				}
				start := time.Now()
				_, err := s.Commit(store.Result{Events: []core.Event{{Run: "run-00", At: at, Body: core.Item{
					ID: core.ItemID(fmt.Sprintf("stall-%s-%d", name, n)), Turn: "turn-stall", Agent: "agent-stall-" + core.AgentID(name),
					Ordinal: uint32(n + 1), Status: core.ItemFinished, At: at, Body: core.Assistant{Text: "x"},
				}}}})
				if err != nil {
					panic(err) // A scale measurement has no test goroutine to fail from here.
				}
				took := time.Since(start)
				worst, total, n = max(worst, took), total+took, n+1
			}
		}()
		elapsed := timed(f)
		close(stop)
		got := <-done
		report("%s took %v; %d commits ran beside it, mean %v, slowest %v", name, elapsed.Round(time.Millisecond), got[2],
			got[1].Round(time.Microsecond), got[0].Round(time.Microsecond))
	}
	stall("compact", func() {
		if _, err := s.Compact(store.Retention{}, at.Add(1000*time.Hour)); err != nil {
			t.Fatal(err)
		}
	})
	stall("verify", func() {
		if err := s.Verify(); err != nil {
			t.Fatal(err)
		}
	})
}

func pct(d []time.Duration, p int) time.Duration {
	s := slices.Clone(d)
	slices.Sort(s)
	return s[min(len(s)-1, len(s)*p/100)].Round(time.Microsecond)
}
