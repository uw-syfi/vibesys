package clientcli

import (
	"fmt"
	"slices"
	"strings"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
)

// resolve picks the run a verb means. A given RUN is a full id or a unique prefix of one. An
// omitted RUN is the one live run whose repository is repo, or else the most recent ended one there.
// Every verb means the same run by it, so the rule lives here and nowhere else.
// Anything else is an error that lists the candidates with their ids, so the caller can pass one.
func resolve(arg, repo string, runs []*pb.Run, canonical func(string) string) (*pb.Run, error) {
	if arg == "" {
		return resolveOmitted(repo, runs, canonical)
	}
	var matches []*pb.Run
	for _, r := range runs {
		if r.GetId() == arg {
			return r, nil
		}
		if strings.HasPrefix(r.GetId(), arg) {
			matches = append(matches, r)
		}
	}
	switch len(matches) {
	case 1:
		return matches[0], nil
	case 0:
		return nil, failed("no run has the id or id prefix %q.%s", arg, candidates(runs))
	}
	return nil, failed("%q is the start of %d runs; pass more of the id.%s", arg, len(matches), candidates(matches))
}

func resolveOmitted(repo string, runs []*pb.Run, canonical func(string) string) (*pb.Run, error) {
	var here, elsewhere, ended []*pb.Run
	repo = canonical(repo)
	for _, r := range runs {
		switch {
		case !live(r) && canonical(r.GetRepository()) == repo:
			ended = append(ended, r)
		case !live(r):
		case canonical(r.GetRepository()) == repo:
			here = append(here, r)
		default:
			elsewhere = append(elsewhere, r)
		}
	}
	switch len(here) {
	case 1:
		return here[0], nil
	case 0:
		if len(ended) > 0 {
			sortNewestFirst(ended)
			return ended[0], nil
		}
		return nil, failed("no run in %s has been started; start one with `run start`, or pass a RUN id.%s", repo, candidates(elsewhere))
	}
	return nil, failed("%d live runs in %s: pass a RUN id.%s", len(here), repo, candidates(here))
}

// candidates lists runs one per line, newest first, for an error message.
func candidates(runs []*pb.Run) string {
	if len(runs) == 0 {
		return ""
	}
	sorted := slices.Clone(runs)
	sortNewestFirst(sorted)
	var b strings.Builder
	b.WriteString("\ncandidates:")
	for _, r := range sorted {
		fmt.Fprintf(&b, "\n  %s  %s  %s  %s", r.GetId(), statusWord(r.GetStatus().String(), "RUN_STATUS_"), r.GetTask(), r.GetRepository())
	}
	return b.String()
}

func sortNewestFirst(runs []*pb.Run) {
	slices.SortStableFunc(runs, func(a, b *pb.Run) int {
		return b.GetCreatedAt().AsTime().Compare(a.GetCreatedAt().AsTime())
	})
}
