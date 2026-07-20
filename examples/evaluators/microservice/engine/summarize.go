package engine

import (
	"fmt"
	"sort"

	"vibesys/microservice-evaluator/api"
)

func summarizeTrial(
	index int,
	observations []api.Observation,
	generator GeneratorReport,
	workload api.Workload,
) TrialResult {
	result := TrialResult{
		Index:            index,
		Valid:            true,
		TotalOperations:  len(observations),
		ByOperation:      make(map[string]OperationResult),
		ErrorsByCategory: make(map[string]int),
		Generator:        generator,
	}
	var totalLatencies []float64
	var objectiveLatencies []float64
	var queueWaits []float64
	var protocolTimes []float64
	customTimings := make(map[string][]float64)
	byOperation := make(map[string][]api.Observation)
	var earliest, latest int64
	for observationIndex, observation := range observations {
		result.HTTPInvocations += observation.InvocationCount
		if observationIndex == 0 || observation.ScheduledAt.UnixNano() < earliest {
			earliest = observation.ScheduledAt.UnixNano()
		}
		if observation.CompletedAt.UnixNano() > latest {
			latest = observation.CompletedAt.UnixNano()
		}
		byOperation[observation.Operation] = append(byOperation[observation.Operation], observation)
		queueWaits = append(queueWaits, observation.QueueWaitMS)
		protocolTimes = append(protocolTimes, observation.ProtocolTimeMS)
		for name, value := range observation.CustomTimingsMS {
			customTimings[name] = append(customTimings[name], value)
		}
		if observation.ApplicationSuccess {
			result.SuccessfulOperations++
			totalLatencies = append(totalLatencies, observation.TotalLatencyMS)
			if hasAllTags(observation.Tags, workload.Objective.Tags) {
				objectiveLatencies = append(objectiveLatencies, observation.TotalLatencyMS)
			}
		} else {
			result.FailedOperations++
			category := observation.ErrorCategory
			if category == "" {
				category = "unknown"
			}
			result.ErrorsByCategory[category]++
		}
	}
	if earliest != 0 && latest >= earliest {
		result.ElapsedSeconds = float64(latest-earliest) / 1e9
	}
	if len(observations) > 0 {
		result.SuccessRate = float64(result.SuccessfulOperations) / float64(len(observations))
		result.ErrorRate = float64(result.FailedOperations) / float64(len(observations))
	}
	result.LatencyMS = distribution(totalLatencies)
	result.QueueWaitMS = distribution(queueWaits)
	result.ProtocolTimeMS = distribution(protocolTimes)
	result.CustomTimingsMS = summarizeCustomTimings(customTimings)

	operationNames := make([]string, 0, len(byOperation))
	for name := range byOperation {
		operationNames = append(operationNames, name)
	}
	sort.Strings(operationNames)
	for _, name := range operationNames {
		result.ByOperation[name] = summarizeOperation(byOperation[name])
	}

	switch workload.Objective.Metric {
	case "latency_ms.p50":
		if len(objectiveLatencies) == 0 {
			result.InvalidReasons = append(result.InvalidReasons, "objective has no successful matching latency samples")
		} else {
			sort.Float64s(objectiveLatencies)
			result.PrimaryValue = pointer(percentile(objectiveLatencies, 50))
		}
	case "operations_per_second", "requests_per_second":
		if result.ElapsedSeconds <= 0 {
			result.InvalidReasons = append(result.InvalidReasons, "measurement elapsed time is zero")
		} else {
			result.PrimaryValue = pointer(float64(result.SuccessfulOperations) / result.ElapsedSeconds)
		}
	}
	if !generator.Sustained {
		result.InvalidReasons = append(result.InvalidReasons, fmt.Sprintf(
			"offered rate %.2f is below required %.2f operations/s",
			generator.OfferedRate,
			generator.MinOfferedRate,
		))
	}
	if minimum := workload.Constraints.MinSuccessRate; minimum != nil && result.SuccessRate < *minimum {
		result.InvalidReasons = append(result.InvalidReasons, fmt.Sprintf(
			"success rate %.6f is below required %.6f",
			result.SuccessRate,
			*minimum,
		))
	}
	if maximum := workload.Constraints.MaxErrorRate; maximum != nil && result.ErrorRate > *maximum {
		result.InvalidReasons = append(result.InvalidReasons, fmt.Sprintf(
			"error rate %.6f exceeds allowed %.6f",
			result.ErrorRate,
			*maximum,
		))
	}
	result.Valid = len(result.InvalidReasons) == 0
	if !result.Valid {
		result.PrimaryValue = nil
	}
	return result
}

func summarizeOperation(observations []api.Observation) OperationResult {
	result := OperationResult{Operations: len(observations)}
	var latencies, waits, protocolTimes []float64
	customTimings := make(map[string][]float64)
	for _, observation := range observations {
		result.HTTPInvocations += observation.InvocationCount
		waits = append(waits, observation.QueueWaitMS)
		protocolTimes = append(protocolTimes, observation.ProtocolTimeMS)
		for name, value := range observation.CustomTimingsMS {
			customTimings[name] = append(customTimings[name], value)
		}
		if observation.ApplicationSuccess {
			result.Successes++
			latencies = append(latencies, observation.TotalLatencyMS)
		} else {
			result.Failures++
		}
	}
	result.LatencyMS = distribution(latencies)
	result.QueueWaitMS = distribution(waits)
	result.ProtocolTimeMS = distribution(protocolTimes)
	result.CustomTimingsMS = summarizeCustomTimings(customTimings)
	return result
}

func summarizeCustomTimings(values map[string][]float64) map[string]Distribution {
	if len(values) == 0 {
		return nil
	}
	result := make(map[string]Distribution, len(values))
	for name, samples := range values {
		result[name] = distribution(samples)
	}
	return result
}

func hasAllTags(actual []string, required []string) bool {
	for _, want := range required {
		found := false
		for _, tag := range actual {
			if tag == want {
				found = true
				break
			}
		}
		if !found {
			return false
		}
	}
	return true
}
