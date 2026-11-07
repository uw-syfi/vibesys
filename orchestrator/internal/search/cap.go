package search

import (
	"cmp"
	"slices"

	cli "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
)

// Cap projects an owned view to the bounded wire shape. It keeps the newest jobs
// of the run named by s.RunId and only their results and troubles: the CLI's store holds the jobs of
// every run of the task, and a run that showed an earlier run's jobs would count and replay work it
// never did. A job no run claimed is left out and counted in UnattributedJobs. Health and identity
// belong to the caller.
func Cap(v View, s *pb.SearchState) *pb.SearchState {
	s.UnattributedJobs, s.Truncated = v.UnattributedJobs, v.Truncated || len(v.Detail) > len(Detail(v.Detail))
	for _, f := range v.Facts {
		switch b := f.Fact.(type) {
		case *cli.TaskFact_Job:
			if submittedBy(b.Job, s.RunId) {
				s.Jobs = append(s.Jobs, b.Job)
			}
		case *cli.TaskFact_Result:
			s.Results = append(s.Results, b.Result)
		case *cli.TaskFact_Judgment:
			s.Judgments = append(s.Judgments, b.Judgment)
		case *cli.TaskFact_Promotion:
			s.Promotions = append(s.Promotions, b.Promotion)
		case *cli.TaskFact_Plan:
			s.Plans = append(s.Plans, b.Plan)
		case *cli.TaskFact_Head:
			s.Head = b.Head
		case *cli.TaskFact_Trouble:
			s.Troubles = append(s.Troubles, b.Trouble)
		}
	}
	slices.SortFunc(s.Jobs, func(a, b *cli.Job) int {
		if c := a.GetSubmittedAt().AsTime().Compare(b.GetSubmittedAt().AsTime()); c != 0 {
			return -c
		}
		return cmp.Compare(a.Id, b.Id)
	})
	s.Jobs = capList(s.Jobs, 500, &s.Truncated)
	kept := map[string]bool{}
	for _, j := range s.Jobs {
		kept[j.Id] = true
	}
	s.Results = slices.DeleteFunc(s.Results, func(r *cli.Result) bool { return !kept[r.JobId] })
	s.Troubles = slices.DeleteFunc(s.Troubles, func(r *cli.JobTrouble) bool { return !kept[r.JobId] })
	slices.SortFunc(s.Results, func(a, b *cli.Result) int { return cmp.Compare(a.JobId, b.JobId) })
	slices.SortFunc(s.Troubles, func(a, b *cli.JobTrouble) int { return cmp.Compare(a.JobId, b.JobId) })
	_, commits := ages(v)
	slices.SortFunc(s.Judgments, func(a, b *cli.Judgment) int {
		if c := commits[a.Commit].Compare(commits[b.Commit]); c != 0 {
			return -c
		}
		return cmp.Compare(a.Commit, b.Commit)
	})
	slices.SortFunc(s.Promotions, func(a, b *cli.PromotionRecord) int { return cmp.Compare(b.Seq, a.Seq) })
	slices.SortFunc(s.Plans, func(a, b *cli.Plan) int { return cmp.Compare(b.Number, a.Number) })
	s.Judgments = capList(s.Judgments, 100, &s.Truncated)
	s.Promotions = capList(s.Promotions, 200, &s.Truncated)
	s.Plans = capList(s.Plans, 200, &s.Truncated)
	var leftOut bool
	s.Bounds, leftOut = Bounds(v.Facts, s.RunId, s.Head)
	s.Truncated = s.Truncated || leftOut
	boundBytes(s, v)
	return s
}

// submittedBy says the run submitted the job.
func submittedBy(j *cli.Job, run string) bool {
	return slices.ContainsFunc(j.GetSubmitters(), func(s *cli.Submitter) bool { return s.GetRunId() == run })
}

func capList[T any](xs []T, n int, truncated *bool) []T {
	if len(xs) > n {
		*truncated = true
	}
	return xs[:min(len(xs), n)]
}
