package engine

import (
	"math"
	"math/rand"
	"sort"
)

func distribution(values []float64) Distribution {
	result := Distribution{Count: len(values)}
	if len(values) == 0 {
		return result
	}
	ordered := append([]float64(nil), values...)
	sort.Float64s(ordered)
	var sum float64
	for _, value := range ordered {
		sum += value
	}
	result.Mean = pointer(sum / float64(len(ordered)))
	result.P50 = pointer(percentile(ordered, 50))
	result.P90 = pointer(percentile(ordered, 90))
	result.P95 = pointer(percentile(ordered, 95))
	result.P99 = pointer(percentile(ordered, 99))
	result.P999 = pointer(percentile(ordered, 99.9))
	result.Max = pointer(ordered[len(ordered)-1])
	return result
}

func percentile(ordered []float64, percent float64) float64 {
	if len(ordered) == 0 {
		return math.NaN()
	}
	if len(ordered) == 1 {
		return ordered[0]
	}
	position := percent / 100 * float64(len(ordered)-1)
	lower := int(math.Floor(position))
	upper := int(math.Ceil(position))
	if lower == upper {
		return ordered[lower]
	}
	return ordered[lower] + (ordered[upper]-ordered[lower])*(position-float64(lower))
}

func aggregate(values []float64) Aggregate {
	result := Aggregate{Trials: len(values)}
	if len(values) == 0 {
		return result
	}
	ordered := append([]float64(nil), values...)
	sort.Float64s(ordered)
	median := percentile(ordered, 50)
	deviations := make([]float64, len(ordered))
	for index, value := range ordered {
		deviations[index] = math.Abs(value - median)
	}
	sort.Float64s(deviations)
	result.Median = pointer(median)
	result.MAD = pointer(percentile(deviations, 50))
	result.IQR = pointer(percentile(ordered, 75) - percentile(ordered, 25))
	if len(ordered) >= 2 {
		result.CI95 = bootstrapMedianCI(ordered, 2000, 1)
	}
	return result
}

func bootstrapMedianCI(values []float64, repetitions int, seed int64) []float64 {
	rng := rand.New(rand.NewSource(seed))
	medians := make([]float64, repetitions)
	sample := make([]float64, len(values))
	for repetition := 0; repetition < repetitions; repetition++ {
		for index := range sample {
			sample[index] = values[rng.Intn(len(values))]
		}
		sort.Float64s(sample)
		medians[repetition] = percentile(sample, 50)
	}
	sort.Float64s(medians)
	return []float64{percentile(medians, 2.5), percentile(medians, 97.5)}
}

func pointer(value float64) *float64 {
	return &value
}
