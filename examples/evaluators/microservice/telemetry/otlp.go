package telemetry

import (
	"bufio"
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"sort"
	"strconv"
	"strings"
	"time"
)

type spanSample struct {
	durationMS float64
	failed     bool
}

// SummarizeOTLP normalizes OTLP JSON or newline-delimited OTLP JSON files.
func SummarizeOTLP(
	request CollectionRequest,
	paths []string,
	top int,
) (Report, error) {
	if err := ValidateRequest(request); err != nil {
		return Report{}, err
	}
	if len(paths) == 0 {
		return Report{}, fmt.Errorf("at least one OTLP JSON input is required")
	}
	if top <= 0 {
		return Report{}, fmt.Errorf("top must be positive")
	}
	services := make(map[string][]spanSample)
	spans := make(map[string][]spanSample)
	datastores := make(map[string][]spanSample)
	spanCount := 0
	errorCount := 0
	for _, path := range paths {
		documents, err := readJSONDocuments(path)
		if err != nil {
			return Report{}, err
		}
		for _, document := range documents {
			visitResourceSpans(document, func(service string, span map[string]any) {
				start, end, ok := spanTimes(span)
				if !ok || !inMeasurementWindows(start, end, request.Windows) {
					return
				}
				duration := float64(end-start) / 1e6
				attributes := otlpAttributes(asSlice(span["attributes"]))
				failed := spanFailed(span, attributes)
				operation := stringValue(span["name"])
				if operation == "" {
					operation = "unknown"
				}
				if service == "" {
					service = "unknown"
				}
				sample := spanSample{durationMS: duration, failed: failed}
				services[service] = append(services[service], sample)
				spanName := service + ":" + operation
				spans[spanName] = append(spans[spanName], sample)
				if isDatastoreSpan(service, operation, attributes) {
					datastores[spanName] = append(datastores[spanName], sample)
				}
				spanCount++
				if failed {
					errorCount++
				}
			})
		}
	}
	report := Report{
		SchemaVersion: ReportSchemaVersion,
		Source:        "otlp-json",
		CollectedAt:   time.Now().UTC(),
		WorkloadName:  request.WorkloadName,
		WorkloadHash:  request.WorkloadHash,
		Windows:       append([]MeasurementWindow(nil), request.Windows...),
		SpanCount:     spanCount,
		ErrorCount:    errorCount,
		Services:      rankLatencyRows(services, top),
		Spans:         rankLatencyRows(spans, top),
		Datastores:    rankLatencyRows(datastores, top),
	}
	if err := ValidateReport(report); err != nil {
		return Report{}, err
	}
	return report, nil
}

func readJSONDocuments(path string) ([]any, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read OTLP JSON %s: %w", path, err)
	}
	var document any
	if err := decodeJSON(data, &document); err == nil {
		return []any{document}, nil
	}
	var documents []any
	scanner := bufio.NewScanner(bytes.NewReader(data))
	buffer := make([]byte, 64*1024)
	scanner.Buffer(buffer, 16*1024*1024)
	for line := 1; scanner.Scan(); line++ {
		if len(bytes.TrimSpace(scanner.Bytes())) == 0 {
			continue
		}
		var item any
		if err := decodeJSON(scanner.Bytes(), &item); err != nil {
			return nil, fmt.Errorf("parse OTLP JSON %s line %d: %w", path, line, err)
		}
		documents = append(documents, item)
	}
	if err := scanner.Err(); err != nil {
		return nil, fmt.Errorf("scan OTLP JSON %s: %w", path, err)
	}
	if len(documents) == 0 {
		return nil, fmt.Errorf("OTLP JSON %s contains no documents", path)
	}
	return documents, nil
}

func decodeJSON(data []byte, target any) error {
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.UseNumber()
	return decoder.Decode(target)
}

func visitResourceSpans(value any, visit func(string, map[string]any)) {
	switch typed := value.(type) {
	case []any:
		for _, item := range typed {
			visitResourceSpans(item, visit)
		}
	case map[string]any:
		if resources, ok := typed["resourceSpans"].([]any); ok {
			for _, rawResource := range resources {
				resource, ok := rawResource.(map[string]any)
				if !ok {
					continue
				}
				metadata, _ := resource["resource"].(map[string]any)
				attributes := otlpAttributes(asSlice(metadata["attributes"]))
				service := stringValue(attributes["service.name"])
				scopes := asSlice(resource["scopeSpans"])
				if len(scopes) == 0 {
					scopes = asSlice(resource["instrumentationLibrarySpans"])
				}
				for _, rawScope := range scopes {
					scope, ok := rawScope.(map[string]any)
					if !ok {
						continue
					}
					for _, rawSpan := range asSlice(scope["spans"]) {
						if span, ok := rawSpan.(map[string]any); ok {
							visit(service, span)
						}
					}
				}
			}
			return
		}
		for _, item := range typed {
			visitResourceSpans(item, visit)
		}
	}
}

func spanTimes(span map[string]any) (int64, int64, bool) {
	start, err := strconv.ParseInt(stringValue(span["startTimeUnixNano"]), 10, 64)
	if err != nil {
		return 0, 0, false
	}
	end, err := strconv.ParseInt(stringValue(span["endTimeUnixNano"]), 10, 64)
	if err != nil || end < start {
		return 0, 0, false
	}
	return start, end, true
}

func inMeasurementWindows(startUnixNano, endUnixNano int64, windows []MeasurementWindow) bool {
	start := time.Unix(0, startUnixNano)
	end := time.Unix(0, endUnixNano)
	for _, window := range windows {
		if !start.Before(window.Start) && !end.After(window.End) {
			return true
		}
	}
	return false
}

func otlpAttributes(items []any) map[string]any {
	result := make(map[string]any, len(items))
	for _, rawItem := range items {
		item, ok := rawItem.(map[string]any)
		if !ok {
			continue
		}
		key := stringValue(item["key"])
		value, _ := item["value"].(map[string]any)
		for _, valueKey := range []string{
			"stringValue", "intValue", "doubleValue", "boolValue",
		} {
			if rawValue, exists := value[valueKey]; exists {
				result[key] = rawValue
				break
			}
		}
	}
	return result
}

func spanFailed(span map[string]any, attributes map[string]any) bool {
	if status, ok := span["status"].(map[string]any); ok {
		code := strings.ToUpper(stringValue(status["code"]))
		if code == "2" || code == "STATUS_CODE_ERROR" {
			return true
		}
	}
	for _, key := range []string{"error.type", "exception.type"} {
		if stringValue(attributes[key]) != "" {
			return true
		}
	}
	return attributes["error"] == true
}

func isDatastoreSpan(service, operation string, attributes map[string]any) bool {
	for _, key := range []string{"db.system", "db.system.name", "db.namespace", "db.operation.name"} {
		if stringValue(attributes[key]) != "" {
			return true
		}
	}
	text := strings.ToLower(service + " " + operation)
	for _, token := range []string{"mongo", "redis", "memcache", "mysql", "postgres", "database", " db."} {
		if strings.Contains(text, token) {
			return true
		}
	}
	return false
}

func rankLatencyRows(groups map[string][]spanSample, top int) []LatencyRow {
	rows := make([]LatencyRow, 0, len(groups))
	for name, samples := range groups {
		values := make([]float64, 0, len(samples))
		errors := 0
		for _, sample := range samples {
			values = append(values, sample.durationMS)
			if sample.failed {
				errors++
			}
		}
		sort.Float64s(values)
		mean := 0.0
		for _, value := range values {
			mean += value
		}
		mean /= float64(len(values))
		p50 := percentile(values, 50)
		p95 := percentile(values, 95)
		p99 := percentile(values, 99)
		maximum := values[len(values)-1]
		rows = append(rows, LatencyRow{
			Name: name, Count: len(values), ErrorCount: errors,
			MeanMS: &mean, P50MS: &p50, P95MS: &p95, P99MS: &p99, MaxMS: &maximum,
		})
	}
	sort.Slice(rows, func(left, right int) bool {
		if *rows[left].P95MS == *rows[right].P95MS {
			return rows[left].Name < rows[right].Name
		}
		return *rows[left].P95MS > *rows[right].P95MS
	})
	if len(rows) > top {
		rows = rows[:top]
	}
	return rows
}

func percentile(sorted []float64, percent float64) float64 {
	if len(sorted) == 1 {
		return sorted[0]
	}
	position := float64(len(sorted)-1) * percent / 100
	lower := int(position)
	upper := lower + 1
	if upper >= len(sorted) {
		return sorted[lower]
	}
	fraction := position - float64(lower)
	return sorted[lower]*(1-fraction) + sorted[upper]*fraction
}

func asSlice(value any) []any {
	items, _ := value.([]any)
	return items
}

func stringValue(value any) string {
	switch typed := value.(type) {
	case string:
		return typed
	case json.Number:
		return typed.String()
	case float64:
		return strconv.FormatInt(int64(typed), 10)
	default:
		return ""
	}
}
