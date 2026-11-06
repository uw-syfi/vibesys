package clientcli

import (
	"fmt"
	"slices"
	"strings"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
)

// live says a run has not reached a final status.
func live(r *pb.Run) bool {
	return r.GetStatus() != pb.RunStatus_RUN_STATUS_ENDED && r.GetStatus() != pb.RunStatus_RUN_STATUS_FAILED
}

// resolve picks the run a verb means. A given RUN is a full id or a unique prefix of one. An
// omitted RUN is the one live run whose repository is repo. Anything else is an error that lists
// the candidates with their ids, so the caller can pass one.
func resolve(arg, repo string, runs []*pb.Run) (*pb.Run, error) {
	if arg == "" {
		return resolveOmitted(repo, runs)
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

func resolveOmitted(repo string, runs []*pb.Run) (*pb.Run, error) {
	var here, elsewhere []*pb.Run
	for _, r := range runs {
		switch {
		case !live(r):
		case r.GetRepository() == repo:
			here = append(here, r)
		default:
			elsewhere = append(elsewhere, r)
		}
	}
	switch len(here) {
	case 1:
		return here[0], nil
	case 0:
		return nil, failed("no live run in %s: pass a RUN id.%s", repo, candidates(elsewhere))
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
