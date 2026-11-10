package main

import (
	"strconv"

	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/evaluator"
	pb "github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/gen/vibesys/evaluator/v1"
)

func init() {
	register("sample", "start", typed(sampleStart))
	register("sample", "phase", typed(samplePhase))
	register("sample", "combine", typed(sampleCombine))
}

// sampleStart resolves the sample seed and artifacts directory and writes seeds.json.
func sampleStart(req *pb.SampleStartRequest) (*pb.SampleStartResponse, error) {
	mode := evaluator.Mode(req.GetMode())
	if mode != evaluator.Check && mode != evaluator.Benchmark && mode != evaluator.Profile && mode != evaluator.Trace {
		return nil, fail("invalid_argument", "mode is check, benchmark, profile or trace, got %q", req.GetMode())
	}
	seed, source, err := evaluator.SampleSeed(req.GetSeedFlag(), req.SeedFlag != nil, req.GetEnvSeed())
	if err != nil {
		return nil, fail("invalid_argument", "%v", err)
	}
	artifacts, err := evaluator.ArtifactsDir(req.GetEnvArtifactsDir(), req.GetScratchDir())
	if err != nil {
		return nil, fail("io", "%v", err)
	}
	if err := evaluator.WriteSeeds(artifacts, seed, source); err != nil {
		return nil, fail("io", "%v", err)
	}
	evaluator.Log("%s sample, seed %s (%s)", mode, strconv.FormatUint(seed, 10), source)
	return &pb.SampleStartResponse{Seed: seed, SeedSource: source, ArtifactsDir: artifacts}, nil
}

// samplePhase creates one phase's directory.
func samplePhase(req *pb.SamplePhaseRequest) (*pb.SamplePhaseResponse, error) {
	path, err := evaluator.PhaseDir(req.GetArtifactsDir(), req.GetName())
	if err != nil {
		return nil, fail("phase", "%v", err)
	}
	return &pb.SamplePhaseResponse{Path: path}, nil
}

var aggregations = map[pb.Aggregation]evaluator.Aggregation{
	pb.Aggregation_AGGREGATION_MEDIAN: evaluator.Median,
	pb.Aggregation_AGGREGATION_MEAN:   evaluator.Mean,
	pb.Aggregation_AGGREGATION_MIN:    evaluator.MinOf,
	pb.Aggregation_AGGREGATION_MAX:    evaluator.MaxOf,
	pb.Aggregation_AGGREGATION_SUM:    evaluator.Sum,
}

// sampleCombine turns each metric's trials into the sample's value.
func sampleCombine(req *pb.SampleCombineRequest) (*pb.SampleCombineResponse, error) {
	resp := &pb.SampleCombineResponse{}
	for _, s := range req.GetSeries() {
		how, ok := aggregations[s.GetAggregation()]
		if !ok {
			return nil, fail("invalid_argument", "series %q: choose an aggregation", s.GetName())
		}
		v, err := evaluator.Combine(how, s.GetValues())
		if err != nil {
			return nil, fail("invalid_argument", "series %q: %v", s.GetName(), err)
		}
		resp.Values = append(resp.Values, &pb.MetricValue{Name: s.GetName(), Value: v})
	}
	return resp, nil
}
