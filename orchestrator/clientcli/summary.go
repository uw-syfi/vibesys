package clientcli

import (
	"fmt"
	"math"
	"slices"
	"strconv"
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
	row("status", "%s", statusWord(v.Run.GetStatus().String(), "RUN_STATUS_")+neverStarted(v.Run)+withPrefix(" by ", stopReason(v.Run))+stallWords(v, now))
	if v.Run.GetStatus() == pb.RunStatus_RUN_STATUS_FAILED {
		if detail := endDetail(v.Agents[v.Run.GetLeadAgentId()], 500); detail != "" {
			row("why", "%s", detail)
		}
	}
	row("task", "%s", v.Run.GetTask())
	row("may use", "%s", allowedWords(v.Run.GetAllowedAgents()))
	row("repository", "%s", v.Run.GetRepository())
	row("started", "%s ago", age(now, v.Run.GetCreatedAt().AsTime()))
	if v.Run.GetEndedAt() != nil {
		row("ended", "%s ago", age(now, v.Run.GetEndedAt().AsTime()))
	}
	if v.Truncated {
		row("note", "the snapshot was cut to fit, so agents, turns, checks and measures are not counted")
	} else {
		row("agents", "%s", agentCounts(v))
		row("turns", "%d", len(v.Turns))
		checks, measures := jobTotals(v.Search)
		row("checks", "%s", checks)
		row("measures", "%s", measures)
	}
	b.WriteString(usageRows(v))
	if words := limitWords(v, zone); words != "" {
		row("limits", "%s", words)
	}
	if kept := v.Run.GetKeptCheckouts(); len(kept) > 0 {
		row("kept", "%d working copies hold uncommitted changes to tracked files; commit or discard them, then `git worktree remove` each:", len(kept))
		for _, k := range kept {
			fmt.Fprintf(&b, "    %s  %s\n", k.GetAgentId(), k.GetLocation())
		}
	}
	row("head", "%s", head(v.Search))
	row("best", "%s", best(v.Search))
	row("last report", "%s", lastReport(v, zone))
	return b.String()
}

// neverStarted marks a run whose lead never ran anything.
func neverStarted(r *pb.Run) string {
	if r.GetNeverOpened() {
		return " (never started)"
	}
	return ""
}

// allowedWords lists the agents a run may use: each harness with its model, or "default model" for a
// harness the operator gave none.
func allowedWords(allowed []*pb.AllowedAgent) string {
	if len(allowed) == 0 {
		return "not recorded"
	}
	parts := make([]string, 0, len(allowed))
	for _, c := range allowed {
		model := orDefault(c.GetModel())
		parts = append(parts, statusWord(c.GetHarness().String(), "HARNESS_")+" "+model)
	}
	return strings.Join(parts, "; ")
}

func orDefault(model string) string {
	if model == "" {
		return "(default model)"
	}
	return model
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

// stallWords says how long a stalled run has stood still, or that its lead has given its final
// report, and is empty for a run that is neither.
func stallWords(v *View, now time.Time) string {
	if v.Run.GetFinalReported() {
		return ", its lead has given its final report"
	}
	if !v.Stalled() {
		return ""
	}
	return ", stalled for " + age(now, v.Run.GetStalledSince().AsTime())
}

// unmeasured returns, in id order, the agents that are over and ran a turn yet recorded no usage: a
// session killed or lost before its provider reported, so what it cost is missing from every total.
// It is empty for a cut snapshot, which has not the turns to say.
func unmeasured(v *View) []string {
	if v.Truncated {
		return nil
	}
	ran := map[string]bool{}
	for _, t := range v.Turns {
		ran[t.GetAgentId()] = true
	}
	var ids []string
	for id, a := range v.Agents {
		if over(a.GetStatus()) && ran[id] && !hasUsage(a.GetUsage()) {
			ids = append(ids, id)
		}
	}
	slices.Sort(ids)
	return ids
}

// over says an agent's session is finished for good.
func over(s pb.AgentStatus) bool {
	return s == pb.AgentStatus_AGENT_STATUS_ENDED || s == pb.AgentStatus_AGENT_STATUS_FAILED || s == pb.AgentStatus_AGENT_STATUS_LOST
}

// hasUsage says a usage reports anything at all.
func hasUsage(u *pb.Usage) bool {
	return u != nil && (u.InputTokens != nil || u.CacheReadTokens != nil || u.CacheCreationTokens != nil ||
		u.OutputTokens != nil || u.ReasoningTokens != nil || u.CostUsd != nil)
}

// usageRows is what the run has cost: the run's total, and each agent's, from the orchestrator's own
// tallies, which count every finished turn and so stay right when a snapshot was cut. An agent that
// has finished no turn that reported a cost has no row, except one that is over and reported none:
// its cost is unknown, so the total says it leaves those out.
func usageRows(v *View) string {
	var b strings.Builder
	total := usageWords(v.Run.GetUsage())
	if total == "" {
		total = "not reported yet"
	}
	missing := unmeasured(v)
	switch len(missing) {
	case 0:
	case 1:
		total += "; 1 agent ended without a measured cost, so this is not the whole cost"
	default:
		total += fmt.Sprintf("; %d agents ended without a measured cost, so this is not the whole cost", len(missing))
	}
	fmt.Fprintf(&b, "  %-11s %s\n", "usage", total)
	ids := make([]string, 0, len(v.Agents))
	for id := range v.Agents {
		ids = append(ids, id)
	}
	slices.Sort(ids)
	for _, id := range ids {
		a := v.Agents[id]
		words := usageWords(a.GetUsage())
		if words == "" && slices.Contains(missing, id) {
			words = "ended without a measured cost"
		}
		if words != "" {
			fmt.Fprintf(&b, "    %-9s %s  %s\n", statusWord(a.GetRole().String(), "AGENT_ROLE_"), id, words)
		}
	}
	return b.String()
}

// jobTotals counts the finished and failed checks and measures of a search view.
func jobTotals(s *pb.SearchState) (checks, measures string) {
	if s == nil {
		return "not reported", "not reported"
	}
	if s.GetTruncated() {
		return "not counted, the search view was cut", "not counted, the search view was cut"
	}
	// An unreadable view with no jobs is a host that has not read the job log yet (it has just
	// started, or cannot reach it): a count of zero would be a guess that goes up later.
	unread := s.GetHealth().GetUnreadable()
	if unread != nil && len(s.GetJobs()) == 0 {
		return notLoaded(unread), notLoaded(unread)
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
		counted := fmt.Sprintf("%d finished, %d failed", finished, broken)
		if unread != nil {
			counted += " (as last read; the job log cannot be read now)"
		}
		return counted
	}
	return count(jobKind, "check"), count(jobKind, "measure")
}

// notLoaded says why a view has no counts yet.
func notLoaded(u *pb.Unreadable) string {
	if u.GetReason() == pb.UnreadableReason_UNREADABLE_REASON_NO_DAEMON {
		return "loading, the host is still reading the job log"
	}
	return "not loaded, the job log cannot be read: " + statusWord(u.GetReason().String(), "UNREADABLE_REASON_")
}

// head is the candidate the run last promoted and the value it measured on the metric it scored, which
// for a task of several objectives is not the best the run measured on the others.
func head(s *pb.SearchState) string {
	current := s.GetHead().GetCurrent()
	if current == nil {
		return none(s)
	}
	m := current.GetResult().GetMeasure()
	return fmt.Sprintf("%s = %s at %s", m.GetMetric().GetName(), withUnit(m.GetValue(), m.GetMetric().GetUnit()), shortCommit(current.GetHead()))
}

// best is the best the run measured on each objective, as the search state serves it and never
// recomputed here: the bound's best value when the run has a bound for the objective, else the newest
// point of the series that set a best. A bound counted in another unit than the measurements says so.
func best(s *pb.SearchState) string {
	bounds := map[string]*pb.ObjectiveBounds{}
	for _, b := range s.GetBounds() {
		bounds[b.GetObjective()] = b
	}
	var parts []string
	for _, series := range s.GetSeries() {
		value, found := 0.0, false
		if b := bounds[series.GetObjective()]; b != nil && b.BestValue != nil {
			value, found = b.GetBestValue(), true
		} else {
			for _, p := range slices.Backward(series.GetPoints()) {
				if p.GetBestSoFar() {
					value, found = p.GetValue(), true
					break
				}
			}
		}
		if !found {
			continue
		}
		part := fmt.Sprintf("%s = %s", series.GetObjective(), withUnit(value, series.GetUnit()))
		if b := bounds[series.GetObjective()]; b != nil && b.GetUnitCheck() == pb.BoundUnitCheck_BOUND_UNIT_CHECK_MISMATCH {
			part += fmt.Sprintf(" (its bound is in %s, unverified)", b.GetVersions()[len(b.GetVersions())-1].GetUnit())
		}
		parts = append(parts, part)
	}
	if len(parts) == 0 {
		return none(s)
	}
	return strings.Join(parts, ", ")
}

func none(s *pb.SearchState) string {
	if s.GetHealth().GetUnreadable() != nil {
		return "not known yet"
	}
	return "none yet"
}

// withUnit is a value followed by the unit it is counted in, and the value alone when the unit is not
// known.
func withUnit(value float64, unit string) string {
	if unit == "" {
		return fmt.Sprintf("%g", value)
	}
	return fmt.Sprintf("%g %s", value, unit)
}

func lastReport(v *View, zone *time.Location) string {
	reports := v.Reports()
	if len(reports) == 0 {
		return "none yet"
	}
	last := reports[len(reports)-1]
	return fmt.Sprintf("%s, %s", clock(last.GetAt().AsTime(), zone), oneLine(last.GetReport().GetHeadline(), 200))
}

// limitWords says the limits a run has and, once one is reached, which and when. It is empty for a run
// with none.
func limitWords(v *View, zone *time.Location) string {
	r := v.Run
	var parts []string
	if r.GetDeadline() != nil {
		parts = append(parts, "time "+age(r.GetDeadline().AsTime(), r.GetCreatedAt().AsTime()))
	}
	if r.MaxCostUsd != nil {
		cost := "cost " + dollars(r.GetMaxCostUsd())
		switch {
		case r.GetUsage() != nil && r.GetUsage().CostUsd != nil:
			cost += " (reported so far " + dollars(r.GetUsage().GetCostUsd()) + ")"
		case !v.Truncated && anyTurnEnded(v.Turns):
			cost += " (no agent has reported a cost, so this limit cannot take effect)"
		}
		parts = append(parts, cost)
	}
	if len(parts) == 0 {
		return ""
	}
	words := strings.Join(parts, ", ")
	if r.GetLimitReached() != pb.RunLimit_RUN_LIMIT_UNSPECIFIED {
		words += fmt.Sprintf("; the %s was reached at %s and the lead was told to finish", limitWord(r.GetLimitReached()), clock(r.GetLimitReachedAt().AsTime(), zone))
	}
	return words
}

// dollars writes an amount of money to a hundredth of a dollar at least, and to a ten-thousandth when it
// has that much to say, so a limit of half a cent is not shown as a cent.
func dollars(usd float64) string {
	s := strconv.FormatFloat(math.Round(usd*1e4)/1e4, 'f', -1, 64)
	if i := strings.IndexByte(s, '.'); i < 0 {
		s += ".00"
	} else if places := len(s) - i - 1; places < 2 {
		s += strings.Repeat("0", 2-places)
	}
	return "$" + s
}

// anyTurnEnded: a cost is reported when a turn ends, so only an ended turn can show that none was.
func anyTurnEnded(turns map[string]*pb.Turn) bool {
	for _, t := range turns {
		if t.GetStatus() != pb.TurnStatus_TURN_STATUS_RUNNING {
			return true
		}
	}
	return false
}
