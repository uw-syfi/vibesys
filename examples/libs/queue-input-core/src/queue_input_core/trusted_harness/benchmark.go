package main

import (
	"crypto/rand"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"sync"
	"time"
)

type benchmarkConfig struct {
	candidateConfig
	producers int
	consumers int
	duration  time.Duration
	warmup    time.Duration
	seed      int64
}

type benchmarkResult struct {
	Scenario       string  `json:"scenario"`
	Enqueued       uint64  `json:"enqueued"`
	Dropped        uint64  `json:"dropped"`
	Dequeued       uint64  `json:"dequeued"`
	Empty          uint64  `json:"empty"`
	Attempts       uint64  `json:"attempts"`
	Duration       float64 `json:"duration"`
	TotalOpsPerSec float64 `json:"total_ops_per_sec"`
	Producers      int     `json:"producers"`
	Consumers      int     `json:"consumers"`
}

type benchmarkCounts struct {
	enqueued            uint64
	full                uint64
	dequeued            uint64
	empty               uint64
	enqueuedFingerprint [2]uint64
	dequeuedFingerprint [2]uint64
}

type fingerprintKeys [2]uint64

func newFingerprintKeys() (fingerprintKeys, error) {
	var data [16]byte
	if _, err := rand.Read(data[:]); err != nil {
		return fingerprintKeys{}, fmt.Errorf("generate benchmark fingerprint key: %w", err)
	}
	return fingerprintKeys{
		binary.LittleEndian.Uint64(data[:8]),
		binary.LittleEndian.Uint64(data[8:]),
	}, nil
}

func mixFingerprint(value, key uint64) uint64 {
	value += key + 0x9e3779b97f4a7c15
	value = (value ^ (value >> 30)) * 0xbf58476d1ce4e5b9
	value = (value ^ (value >> 27)) * 0x94d049bb133111eb
	return value ^ (value >> 31)
}

func addFingerprint(target *[2]uint64, keys fingerprintKeys, value uint64) {
	target[0] += mixFingerprint(value, keys[0])
	target[1] += mixFingerprint(value, keys[1])
}

func (counts *benchmarkCounts) add(other benchmarkCounts) {
	counts.enqueued += other.enqueued
	counts.full += other.full
	counts.dequeued += other.dequeued
	counts.empty += other.empty
	for index := range counts.enqueuedFingerprint {
		counts.enqueuedFingerprint[index] += other.enqueuedFingerprint[index]
		counts.dequeuedFingerprint[index] += other.dequeuedFingerprint[index]
	}
}

func drainAndValidate(
	session *candidateSession,
	lane int,
	capacity uint64,
	counts *benchmarkCounts,
	keys fingerprintKeys,
) error {
	drained := uint64(0)
	drainedFingerprint := [2]uint64{}
	for {
		requests := make([]request, ringSlots)
		for index := range requests {
			requests[index] = request{operation: operationDequeue}
		}
		responses, err := session.invokeBatch(lane, requests)
		if err != nil {
			return fmt.Errorf("drain queue: %w", err)
		}
		emptyObserved := false
		for _, resp := range responses {
			switch resp.status {
			case statusValue:
				if emptyObserved {
					return errors.New("dequeue returned a value after the queue became empty")
				}
				drained++
				if drained > capacity {
					return fmt.Errorf("drained more than queue capacity %d", capacity)
				}
				drainedFingerprint[0] += mixFingerprint(resp.value, keys[0])
				drainedFingerprint[1] += mixFingerprint(resp.value, keys[1])
			case statusEmpty:
				emptyObserved = true
			default:
				return fmt.Errorf("drain dequeue returned invalid status %d", resp.status)
			}
		}
		if emptyObserved {
			break
		}
	}

	dequeued := counts.dequeued + drained
	if dequeued != counts.enqueued {
		return fmt.Errorf(
			"enqueue/dequeue conservation failed: %d successful enqueues, %d returned values",
			counts.enqueued,
			dequeued,
		)
	}
	for index := range keys {
		got := counts.dequeuedFingerprint[index] + drainedFingerprint[index]
		want := counts.enqueuedFingerprint[index]
		if got != want {
			return errors.New("dequeued values do not match the successfully enqueued multiset")
		}
	}
	return nil
}

func runBenchmarkPhase(config benchmarkConfig, duration time.Duration) (benchmarkResult, error) {
	producers, consumers, err := workerCounts(config.scenario, config.producers, config.consumers)
	if err != nil {
		return benchmarkResult{}, err
	}
	sessionConfig := config.candidateConfig
	sessionConfig.laneCount = producers + consumers
	session, err := startCandidate(sessionConfig)
	if err != nil {
		return benchmarkResult{}, err
	}

	perLaneCounts := make([]benchmarkCounts, producers+consumers)
	keys, err := newFingerprintKeys()
	if err != nil {
		_ = session.close()
		return benchmarkResult{}, err
	}
	start := make(chan struct{})
	errCh := make(chan error, producers+consumers)
	var workers sync.WaitGroup
	workers.Add(producers + consumers)
	started := time.Now()
	deadline := started.Add(duration)

	for lane := 0; lane < producers+consumers; lane++ {
		go func(lane int) {
			defer workers.Done()
			localCounts := &perLaneCounts[lane]
			var nextValue uint64
			<-start
			for time.Now().Before(deadline) {
				requests := make([]request, ringSlots)
				for index := range requests {
					requests[index] = request{operation: operationDequeue}
					if lane < producers {
						nextValue++
						requests[index] = request{
							operation: operationEnqueue,
							value:     uint64(lane)<<56 | nextValue,
						}
					}
				}
				responses, err := session.invokeBatch(lane, requests)
				if err != nil {
					errCh <- fmt.Errorf("lane %d: %w", lane, err)
					return
				}
				for index, resp := range responses {
					switch resp.status {
					case statusEnqueued:
						if requests[index].operation != operationEnqueue {
							errCh <- fmt.Errorf("lane %d: dequeue returned enqueue status", lane)
							return
						}
						localCounts.enqueued++
						addFingerprint(
							&localCounts.enqueuedFingerprint,
							keys,
							requests[index].value,
						)
					case statusFull:
						if requests[index].operation != operationEnqueue {
							errCh <- fmt.Errorf("lane %d: dequeue returned full status", lane)
							return
						}
						localCounts.full++
					case statusValue:
						if requests[index].operation != operationDequeue {
							errCh <- fmt.Errorf("lane %d: enqueue returned value status", lane)
							return
						}
						localCounts.dequeued++
						addFingerprint(&localCounts.dequeuedFingerprint, keys, resp.value)
					case statusEmpty:
						if requests[index].operation != operationDequeue {
							errCh <- fmt.Errorf("lane %d: enqueue returned empty status", lane)
							return
						}
						localCounts.empty++
					default:
						errCh <- fmt.Errorf(
							"lane %d: invalid response status %d",
							lane,
							resp.status,
						)
						return
					}
				}
			}
		}(lane)
	}
	started = time.Now()
	deadline = started.Add(duration)
	close(start)
	workers.Wait()
	elapsed := time.Since(started)
	close(errCh)

	var operationErr error
	for err := range errCh {
		operationErr = errors.Join(operationErr, err)
	}
	var counts benchmarkCounts
	for _, laneCounts := range perLaneCounts {
		counts.add(laneCounts)
	}
	if operationErr == nil {
		operationErr = drainAndValidate(session, 0, config.capacity, &counts, keys)
	}
	closeErr := session.close()
	if operationErr != nil || closeErr != nil {
		return benchmarkResult{}, errors.Join(operationErr, closeErr)
	}

	enqueued := counts.enqueued
	full := counts.full
	dequeued := counts.dequeued
	empty := counts.empty
	attempts := enqueued + full + dequeued + empty
	successful := enqueued + dequeued
	return benchmarkResult{
		Scenario:       config.scenario.String(),
		Enqueued:       enqueued,
		Dropped:        full,
		Dequeued:       dequeued,
		Empty:          empty,
		Attempts:       attempts,
		Duration:       elapsed.Seconds(),
		TotalOpsPerSec: float64(successful) / elapsed.Seconds(),
		Producers:      producers,
		Consumers:      consumers,
	}, nil
}

func runBenchmark(config benchmarkConfig) (benchmarkResult, error) {
	if config.duration <= 0 {
		return benchmarkResult{}, errors.New("duration must be greater than zero")
	}
	if config.warmup < 0 {
		return benchmarkResult{}, errors.New("warmup must not be negative")
	}
	if _, _, err := workerCounts(config.scenario, config.producers, config.consumers); err != nil {
		return benchmarkResult{}, err
	}

	gate := accuracyConfig{
		candidateConfig: config.candidateConfig,
		operations:      24,
		trials:          1,
		producers:       config.producers,
		consumers:       config.consumers,
		seed:            config.seed,
	}
	if err := runAccuracy(gate); err != nil {
		return benchmarkResult{}, fmt.Errorf("correctness gate: %w", err)
	}

	if config.warmup > 0 {
		if _, err := runBenchmarkPhase(config, config.warmup); err != nil {
			return benchmarkResult{}, fmt.Errorf("warmup: %w", err)
		}
	}
	return runBenchmarkPhase(config, config.duration)
}

func writeBenchmarkResults(path string, results []benchmarkResult) error {
	if path == "" {
		return nil
	}
	data, err := json.MarshalIndent(results, "", "  ")
	if err != nil {
		return fmt.Errorf("encode benchmark result: %w", err)
	}
	if err := os.WriteFile(path, data, 0o600); err != nil {
		return fmt.Errorf("write benchmark result %q: %w", path, err)
	}
	return nil
}
