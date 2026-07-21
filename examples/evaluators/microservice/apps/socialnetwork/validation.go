package socialnetwork

import (
	"encoding/json"
	"fmt"
	"math"
	"net/http"
	"reflect"
	"sort"
	"strconv"
	"strings"
	"time"

	"vibesys/microservice-evaluator/api"
)

const composeSuccessBody = "Successfully upload post"

var postFields = []string{
	"creator", "media", "post_id", "post_type", "req_id", "text", "timestamp", "urls", "user_mentions",
}

func (a *Application) ValidateOperation(operation api.Operation, plan api.OperationPlan, results []api.ProtocolResult) api.ValidationResult {
	state, ok := plan.State.(*operationState)
	if !ok || state == nil || state.user == nil {
		return invalid("invalid_plan", fmt.Sprintf("unexpected Social Network plan state %T", plan.State))
	}
	if state.kind != operation.Name {
		return invalid("invalid_plan", fmt.Sprintf("plan kind %q does not match operation %q", state.kind, operation.Name))
	}
	wantResults := 1
	if state.kind == composeUserTimeline {
		wantResults = 2
	}
	if len(results) != wantResults {
		return invalid("result_count", fmt.Sprintf("got %d protocol results, want %d", len(results), wantResults))
	}
	if state.kind == composeUserTimeline || state.kind == legacyComposePost {
		if validation := validateCompose(results[0]); !validation.Success {
			return validation
		}
	}
	if state.kind != legacyComposePost {
		resultIndex := 0
		if state.kind == composeUserTimeline {
			resultIndex = 1
		}
		validation := validateTimeline(results[resultIndex], state.user, state.marker, state.timelineSize)
		if !validation.Success {
			if state.kind == composeUserTimeline {
				validation.ErrorCategory = "read_your_write"
				validation.ErrorMessage = "step 2: " + validation.ErrorMessage
			}
			return validation
		}
	}
	custom, validation := captureTimings(operation, results)
	if !validation.Success {
		return validation
	}
	return api.ValidationResult{Success: true, CustomTimings: custom}
}

func validateCompose(result api.ProtocolResult) api.ValidationResult {
	response, validation := validateHTTP(result)
	if !validation.Success {
		return validation
	}
	if body := strings.TrimSpace(string(response.Body)); body != composeSuccessBody {
		return invalid("response_value", fmt.Sprintf("compose response = %q, want %q", body, composeSuccessBody))
	}
	return api.ValidationResult{Success: true}
}

func validateTimeline(result api.ProtocolResult, user *benchmarkUser, marker string, limit int) api.ValidationResult {
	response, validation := validateHTTP(result)
	if !validation.Success {
		return validation
	}
	var posts []map[string]any
	if err := json.Unmarshal(response.Body, &posts); err != nil {
		return invalid("response_json", fmt.Sprintf("timeline must be a JSON array: %v", err))
	}
	if len(posts) == 0 {
		return invalid("response_value", "timeline must contain at least one seeded post")
	}
	if len(posts) > limit {
		return invalid("response_value", fmt.Sprintf("timeline contains %d posts, requested at most %d", len(posts), limit))
	}
	seenPostIDs := make(map[string]struct{}, len(posts))
	previousTimestamp := int64(math.MaxInt64)
	markerFound := marker == ""
	for index, post := range posts {
		keys := make([]string, 0, len(post))
		for key := range post {
			keys = append(keys, key)
		}
		sort.Strings(keys)
		if !reflect.DeepEqual(keys, postFields) {
			return invalid("response_schema", fmt.Sprintf("post %d fields %v, want %v", index, keys, postFields))
		}
		postID, ok := nonEmptyString(post["post_id"])
		if !ok {
			return invalid("response_schema", fmt.Sprintf("post %d post_id must be a non-empty string", index))
		}
		if _, duplicate := seenPostIDs[postID]; duplicate {
			return invalid("response_value", fmt.Sprintf("timeline repeats post_id %q", postID))
		}
		seenPostIDs[postID] = struct{}{}
		if _, ok := nonEmptyString(post["req_id"]); !ok {
			return invalid("response_schema", fmt.Sprintf("post %d req_id must be a non-empty string", index))
		}
		text, ok := nonEmptyString(post["text"])
		if !ok {
			return invalid("response_schema", fmt.Sprintf("post %d text must be a non-empty string", index))
		}
		if text == marker {
			markerFound = true
		}
		timestampString, ok := nonEmptyString(post["timestamp"])
		if !ok {
			return invalid("response_schema", fmt.Sprintf("post %d timestamp must be a non-empty integer string", index))
		}
		timestamp, err := strconv.ParseInt(timestampString, 10, 64)
		if err != nil || timestamp <= 0 {
			return invalid("response_schema", fmt.Sprintf("post %d timestamp %q must be a positive integer", index, timestampString))
		}
		if timestamp > previousTimestamp {
			return invalid("response_order", fmt.Sprintf("post %d timestamp %d follows older timestamp %d", index, timestamp, previousTimestamp))
		}
		previousTimestamp = timestamp
		creator, ok := post["creator"].(map[string]any)
		if !ok || len(creator) != 2 {
			return invalid("response_schema", fmt.Sprintf("post %d creator must contain exactly user_id and username", index))
		}
		creatorID, idOK := creator["user_id"].(string)
		creatorName, nameOK := creator["username"].(string)
		if !idOK || !nameOK || creatorID != strconv.Itoa(user.id) || creatorName != user.username {
			return invalid("response_value", fmt.Sprintf("post %d creator = %v, want user_id=%d username=%q", index, creator, user.id, user.username))
		}
		if postType, ok := post["post_type"].(float64); !ok || math.Trunc(postType) != postType {
			return invalid("response_schema", fmt.Sprintf("post %d post_type must be an integer", index))
		}
		for _, field := range []string{"user_mentions", "media", "urls"} {
			if err := validateCollection(field, post[field]); err != nil {
				return invalid("response_schema", fmt.Sprintf("post %d: %v", index, err))
			}
		}
	}
	if !markerFound {
		return invalid("response_value", fmt.Sprintf("composed marker %q is absent from the user timeline", marker))
	}
	return api.ValidationResult{Success: true}
}

func validateHTTP(result api.ProtocolResult) (api.HTTPResponse, api.ValidationResult) {
	if !result.TransportSuccess {
		return api.HTTPResponse{}, invalid(result.ErrorCategory, result.ErrorMessage)
	}
	response, ok := result.Payload.(api.HTTPResponse)
	if !ok {
		return api.HTTPResponse{}, invalid("invalid_response", fmt.Sprintf("expected HTTP response, got %T", result.Payload))
	}
	if response.StatusCode != http.StatusOK {
		return api.HTTPResponse{}, invalid("http_status", fmt.Sprintf("HTTP %d, want %d", response.StatusCode, http.StatusOK))
	}
	return response, api.ValidationResult{Success: true}
}

func captureTimings(operation api.Operation, results []api.ProtocolResult) (map[string]time.Duration, api.ValidationResult) {
	custom := make(map[string]time.Duration)
	for _, capture := range operation.CaptureHeaders {
		var values []string
		for _, result := range results {
			values = append(values, result.Metadata[http.CanonicalHeaderKey(capture.Header)]...)
		}
		if len(values) == 0 {
			continue
		}
		if len(values) != 1 {
			return nil, invalid("timing_header", fmt.Sprintf("header %s has %d values across the operation, want one", capture.Header, len(values)))
		}
		milliseconds, err := strconv.ParseFloat(values[0], 64)
		maximum := float64(math.MaxInt64) / float64(time.Millisecond)
		if err != nil || math.IsNaN(milliseconds) || math.IsInf(milliseconds, 0) || milliseconds < 0 || milliseconds > maximum {
			return nil, invalid("timing_header", fmt.Sprintf("header %s is not a non-negative finite duration: %q", capture.Header, values[0]))
		}
		custom[capture.Name] = time.Duration(milliseconds * float64(time.Millisecond))
	}
	return custom, api.ValidationResult{Success: true}
}

func nonEmptyString(value any) (string, bool) {
	text, ok := value.(string)
	return text, ok && text != ""
}

func validateCollection(field string, value any) error {
	switch typed := value.(type) {
	case []any:
		fields := map[string][]string{
			"user_mentions": {"user_id", "username"},
			"media":         {"media_id", "media_type"},
			"urls":          {"expanded_url", "shortened_url"},
		}[field]
		for index, item := range typed {
			object, ok := item.(map[string]any)
			if !ok || len(object) != len(fields) {
				return fmt.Errorf("%s item %d must contain exactly %v", field, index, fields)
			}
			for _, name := range fields {
				if _, ok := nonEmptyString(object[name]); !ok {
					return fmt.Errorf("%s item %d field %s must be a non-empty string", field, index, name)
				}
			}
		}
		return nil
	case map[string]any:
		if len(typed) == 0 {
			return nil
		}
		return fmt.Errorf("%s must be an array or empty object", field)
	default:
		return fmt.Errorf("%s must be an array or empty object", field)
	}
}

func invalid(category, message string) api.ValidationResult {
	return api.ValidationResult{ErrorCategory: category, ErrorMessage: message}
}
