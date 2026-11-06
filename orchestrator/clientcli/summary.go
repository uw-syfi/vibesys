package clientcli

import (
	"fmt"
	"slices"
	"strings"
	"time"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
)

// summarize is the human account of a run: where it is, what its agents are doing, what it cost
// and what it has found. A truncated snapshot leaves out counts it cannot stand behind.
func summarize(v *View, now time.Time, zone *time.Location) string {
	if v.Run == nil {
		return fmt.Sprintf("run %s is not in the snapshot\n", v.RunID)
	}
	var b strings.Builder
	row := func(name, format string, args ...any) {
		fmt.Fprintf(&b, "  %-11s %s\n", name, fmt.Sprintf(format, args...))
	}
	fmt.Fprintf(&b, "run %s\n", v.RunID)
	row("status", "%s", statusWord(v.Run.GetStatus().String(), "RUN_STATUS_")+withPrefix(" by ", stopReason(v.Run)))
	row("task", "%s", v.Run.GetTask())
	row("repository", "%s", v.Run.GetRepository())
	row("started", "%s ago", age(now, v.Run.GetCreatedAt().AsTime()))
	if v.Run.GetEndedAt() != nil {
		row("ended", "%s ago", age(now, v.Run.GetEndedAt().AsTime()))
	}
	if v.Truncated {
		row("note", "the snapshot was cut to fit, so agents, turns, checks and measures are not counted")
	} else {
		row("agents", "%s", agentCounts(v))
		row("turns", "%s", turnTotals(v))
		checks, measures := jobTotals(v.Search)
		row("checks", "%s", checks)
		row("measures", "%s", measures)
	}
	row("best", "%s", best(v.Search))
	row("last report", "%s", lastReport(v, zone))
	return b.String()
}

func agentCounts(v *View) string {
	counts := map[pb.AgentStatus]int{}
	for _, a := range v.Agents {
		counts[a.GetStatus()]++
	}
	var parts []string
	for _, status := range sortedKeys(counts) {
		parts = append(parts, fmt.Sprintf("%d %s", counts[status], statusWord(status.String(), "AGENT_STATUS_")))
	}
	if len(parts) == 0 {
		return "none yet"
	}
	return strings.Join(parts, ", ")
}

func sortedKeys[K ~int32](m map[K]int) []K {
	keys := make([]K, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	slices.Sort(keys)
	return keys
}

func turnTotals(v *View) string {
	var total float64
	for _, t := range v.Turns {
		total += t.GetUsage().GetCostUsd()
	}
	return fmt.Sprintf("%d, cost $%.4f", len(v.Turns), total)
}

// jobTotals counts the finished and failed checks and measures of a search view.
func jobTotals(s *pb.SearchState) (checks, measures string) {
	if s == nil {
		return "not reported", "not reported"
	}
	if s.GetTruncated() {
		return "not counted, the search view was cut", "not counted, the search view was cut"
	}
	count := func(kind func(*cliv1.Job) string, name string) string {
		var finished, broken int
		for _, j := range s.GetJobs() {
			if kind(j) != name {
				continue
			}
			switch j.GetState() {
			case cliv1.JobState_JOB_STATE_SUCCEEDED:
				finished++
			case cliv1.JobState_JOB_STATE_FAILED, cliv1.JobState_JOB_STATE_LOST:
				broken++
			}
		}
		return fmt.Sprintf("%d finished, %d failed", finished, broken)
	}
	return count(jobKind, "check"), count(jobKind, "measure")
}

func best(s *pb.SearchState) string {
	current := s.GetHead().GetCurrent()
	if current == nil {
		return "none yet"
	}
	m := current.GetResult().GetMeasure()
	return fmt.Sprintf("%s = %g at %s", m.GetMetric().GetName(), m.GetValue(), shortCommit(current.GetHead()))
}

func lastReport(v *View, zone *time.Location) string {
	reports := v.Reports()
	if len(reports) == 0 {
		return "none yet"
	}
	last := reports[len(reports)-1]
	return fmt.Sprintf("%s, %s", clock(last.GetAt().AsTime(), zone), oneLine(last.GetReport().GetHeadline(), 200))
}
