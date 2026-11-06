package main

import (
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/evaluator"
	pb "github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/gen/vibesys/evaluator/v1"
)

func init() {
	register("hist", "summary", typed(histSummary))
}

// histSummary merges the histograms and reads the percentiles from the sum.
func histSummary(req *pb.HistSummaryRequest) (*pb.HistSummaryResponse, error) {
	merged, err := evaluator.MergeHistograms(req.GetHistograms()...)
	if err != nil {
		return nil, fail("histogram", "%v", err)
	}
	resp := &pb.HistSummaryResponse{Count: merged.Total(), Max: merged.Max()}
	for _, p := range req.GetPercentiles() {
		resp.Values = append(resp.Values, merged.Percentile(p))
	}
	return resp, nil
}
