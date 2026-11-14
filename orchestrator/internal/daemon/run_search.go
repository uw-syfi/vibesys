package daemon

import (
	"context"
	"slices"
	"strings"
	"unicode"

	"golang.org/x/text/unicode/norm"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"

	cli "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/wire"
)

const (
	// MaxRunSearchResults is the contract's count bound. The byte bound below can make an answer
	// smaller when a run has a large (but still bounded) summary report.
	MaxRunSearchResults = 25
	// MaxRunSearchBytes leaves a full MiB below the orchestrator link's receive limit. Search is a
	// unary read, so unlike WatchRuns it has no follow-up frames in which to carry overflow.
	MaxRunSearchBytes = 3 << 20
)

type runSearchEntry struct {
	value  string
	weight int
}

type runSearchMatch struct {
	run     core.Run
	summary *pb.RunSummary
	score   int
}

// searchWords is the wire contract's definition of a word: case-folded letters and numbers, with
// compatibility decomposition so an operator need not type accents exactly as a record did.
func searchWords(value string) []string {
	value = norm.NFKD.String(strings.ToLower(value))
	return strings.FieldsFunc(value, func(r rune) bool {
		return !unicode.IsLetter(r) && !unicode.IsNumber(r)
	})
}

func entryScore(entry runSearchEntry, token string) int {
	words := searchWords(entry.value)
	if len(words) == 0 {
		return 0
	}
	if len(words) == 1 && words[0] == token {
		return entry.weight * 4
	}
	if slices.Contains(words, token) {
		return entry.weight * 3
	}
	if slices.ContainsFunc(words, func(word string) bool { return strings.HasPrefix(word, token) }) {
		return entry.weight * 2
	}
	if strings.Contains(strings.Join(words, " "), token) {
		return entry.weight
	}
	return 0
}

// runSearchScore is conjunctive across query words. A word may match any registered semantic field,
// and the highest-weight occurrence is the one that contributes to relevance.
func runSearchScore(entries []runSearchEntry, query []string) (int, bool) {
	total := 0
	for _, token := range query {
		best := 0
		for _, entry := range entries {
			best = max(best, entryScore(entry, token))
		}
		if best == 0 {
			return 0, false
		}
		total += best
	}
	return total, true
}

func harnessWords(h core.Harness) string {
	switch h {
	case core.ClaudeCode:
		return "claude code claude claude_code"
	case core.Codex:
		return "codex openai"
	default:
		return string(h)
	}
}

func reportEntries(summary *pb.RunSummary) []runSearchEntry {
	report := summary.GetReport().GetReport()
	if report == nil {
		return nil
	}
	return []runSearchEntry{
		{value: report.GetHeadline(), weight: 10},
		{value: report.GetChanged(), weight: 10},
		{value: report.GetInFlight(), weight: 10},
		{value: report.GetDecisions(), weight: 10},
	}
}

func factEntries(facts []*cli.TaskFact) []runSearchEntry {
	var entries []runSearchEntry
	for _, fact := range facts {
		switch body := fact.GetFact().(type) {
		case *cli.TaskFact_Plan:
			entries = append(entries,
				runSearchEntry{value: body.Plan.GetTitle(), weight: 10},
				runSearchEntry{value: body.Plan.GetBody(), weight: 10},
			)
		case *cli.TaskFact_Workstream:
			entries = append(entries,
				runSearchEntry{value: body.Workstream.GetTitle(), weight: 10},
				runSearchEntry{value: body.Workstream.GetNote(), weight: 10},
			)
		}
	}
	return entries
}

// runSearchEntries is the one backend registry of meanings SearchRuns accepts. It reads structured
// fields only; narration, logs and paths inside agent messages are deliberately not parsed.
func (e *Engine) runSearchEntries(run core.Run, summary *pb.RunSummary) []runSearchEntry {
	entries := []runSearchEntry{
		{value: run.Task, weight: 12},
		{value: run.SetupTask, weight: 12},
		{value: run.Repository, weight: 5},
		{value: string(run.ID), weight: 3},
	}
	if card := run.TaskCard; card != nil {
		entries = append(entries,
			runSearchEntry{value: card.Heading, weight: 12},
			runSearchEntry{value: card.WorkloadDescription, weight: 8},
			runSearchEntry{value: card.WorkloadName, weight: 8},
			runSearchEntry{value: card.HardwareName, weight: 8},
			runSearchEntry{value: card.ModelName, weight: 11},
		)
		for _, objective := range card.Objectives {
			entries = append(entries,
				runSearchEntry{value: objective.Name, weight: 7},
				runSearchEntry{value: objective.Unit, weight: 7},
			)
		}
	}
	for _, allowed := range run.Allowed {
		entries = append(entries,
			runSearchEntry{value: harnessWords(allowed.Harness), weight: 9},
			runSearchEntry{value: string(allowed.Model), weight: 11},
		)
	}
	entries = append(entries, reportEntries(summary)...)
	if e.Search != nil {
		view, _ := e.Search.Get(run.ID)
		entries = append(entries, factEntries(view.Facts)...)
	}
	return entries
}

// SearchRuns returns the Run and RunSummary pairs from one engine cut. Plan and workstream fields
// used only for matching are the latest independently synchronized derived search views, so the
// response does not claim they are current to its sequence. The response is bounded twice: by the
// contract's result count and by encoded bytes. A matching row that crosses either limit, and every
// matching row after it, is represented by Truncated rather than a partial result.
func (s *Service) SearchRuns(ctx context.Context, req *pb.SearchRunsRequest) (*pb.SearchRunsResponse, error) {
	if err := wire.Validate(req); err != nil {
		return nil, status.Error(codes.InvalidArgument, err.Error())
	}
	caller, err := s.caller(ctx)
	if err != nil {
		return nil, err
	}
	if err := operatorOnly(caller, "SearchRuns"); err != nil {
		return nil, err
	}
	query := searchWords(req.GetQuery())
	if len(query) == 0 {
		return nil, status.Error(codes.InvalidArgument, "query must contain a letter or number")
	}

	resp := new(pb.SearchRunsResponse)
	err = s.e.Cut(func(state core.State, last uint64) error {
		resp.Sequence = last
		agents := state.AgentsByRun()
		var matches []runSearchMatch
		for _, run := range state.Runs() {
			if run.NeverOpened {
				continue
			}
			summary := s.e.runSummary(run, agents[run.ID])
			score, found := runSearchScore(s.e.runSearchEntries(run, summary), query)
			if found {
				matches = append(matches, runSearchMatch{run: run, summary: summary, score: score})
			}
		}
		slices.SortFunc(matches, func(a, b runSearchMatch) int {
			if a.score != b.score {
				return b.score - a.score
			}
			if c := a.run.CreatedAt.Compare(b.run.CreatedAt); c != 0 {
				return -c
			}
			return strings.Compare(string(a.run.ID), string(b.run.ID))
		})
		for i, match := range matches {
			if i >= MaxRunSearchResults {
				resp.Truncated = true
				break
			}
			result := &pb.RunSearchResult{Run: wire.RunMessage(match.run), Summary: match.summary}
			resp.Results = append(resp.Results, result)
			if proto.Size(resp) > MaxRunSearchBytes {
				resp.Results = resp.Results[:len(resp.Results)-1]
				resp.Truncated = true
				break
			}
		}
		return nil
	})
	if err != nil {
		return nil, status.Error(codes.Unavailable, err.Error())
	}
	if err := wire.Validate(resp); err != nil {
		return nil, status.Error(codes.Internal, err.Error())
	}
	return resp, nil
}
