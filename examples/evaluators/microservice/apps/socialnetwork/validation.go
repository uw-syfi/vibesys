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
	if state.kind == legacyComposePost || state.kind == composeUserTimeline {
		wantResults = 3
	}
	if len(results) != wantResults {
		return invalid("result_count", fmt.Sprintf("got %d protocol results, want %d", len(results), wantResults))
	}
	if state.kind == composeUserTimeline || state.kind == legacyComposePost {
		if validation := validateCompose(results[0]); !validation.Success {
			return validation
		}
		if !state.committed {
			state.user.expected = prependExpected(state.marker, state.user.expected)
			state.committed = true
		}
	}
	resultIndex := 0
	if state.kind == composeUserTimeline || state.kind == legacyComposePost {
		resultIndex = 1
	}
	learnIdentity := state.kind == composeUserTimeline || state.kind == legacyComposePost
	validation := validateTimeline(results[resultIndex], state.user, state.expected, state.timelineSize, learnIdentity)
	if !validation.Success {
		if state.kind == composeUserTimeline || state.kind == legacyComposePost {
			validation.ErrorCategory = "read_your_write"
			validation.ErrorMessage = "user timeline step: " + validation.ErrorMessage
		}
		return validation
	}
	if learnIdentity {
		state.user.expected = append([]expectedPost(nil), state.expected...)
	}
	if state.kind == composeUserTimeline || state.kind == legacyComposePost {
		validation = validateTimeline(results[2], state.user, state.expected, state.timelineSize, false)
		if !validation.Success {
			validation.ErrorCategory = "read_your_write"
			validation.ErrorMessage = "follower home timeline step: " + validation.ErrorMessage
			return validation
		}
	}
	custom, validation := captureTimings(operation, state.kind, results)
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

func validateTimeline(result api.ProtocolResult, user *benchmarkUser, expected []expectedPost, limit int, learnIdentity bool) api.ValidationResult {
	response, validation := validateHTTP(result)
	if !validation.Success {
		return validation
	}
	var posts []map[string]any
	if err := json.Unmarshal(response.Body, &posts); err != nil {
		return invalid("response_json", fmt.Sprintf("timeline must be a JSON array: %v", err))
	}
	wantPosts := len(expected)
	if wantPosts > limit {
		wantPosts = limit
	}
	if len(posts) != wantPosts {
		return invalid("response_value", fmt.Sprintf("timeline contains %d posts, want exact newest window of %d", len(posts), wantPosts))
	}
	seenPostIDs := make(map[string]struct{}, len(posts))
	previousTimestamp := int64(math.MaxInt64)
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
		if text != expected[index].text {
			return invalid("response_value", fmt.Sprintf("post %d text = %q, want %q", index, text, expected[index].text))
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
		if expected[index].postID == "" && learnIdentity {
			expected[index].postID = postID
			expected[index].requestID = post["req_id"].(string)
			expected[index].timestamp = timestampString
		} else if postID != expected[index].postID || post["req_id"] != expected[index].requestID || timestampString != expected[index].timestamp {
			return invalid("response_value", fmt.Sprintf(
				"post %d identity = (%q, %q, %q), want (%q, %q, %q)",
				index, postID, post["req_id"], timestampString,
				expected[index].postID, expected[index].requestID, expected[index].timestamp,
			))
		}
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

func captureTimings(operation api.Operation, kind string, results []api.ProtocolResult) (map[string]time.Duration, api.ValidationResult) {
	custom := make(map[string]time.Duration)
	for _, capture := range operation.CaptureHeaders {
		expectedIndex, ok := timingInvocation(kind, capture.Header)
		if !ok || expectedIndex >= len(results) {
			return nil, invalid("timing_header", fmt.Sprintf("header %s has no valid protocol step for operation %s", capture.Header, kind))
		}
		canonical := http.CanonicalHeaderKey(capture.Header)
		for index, result := range results {
			if index != expectedIndex && len(result.Metadata[canonical]) != 0 {
				return nil, invalid("timing_header", fmt.Sprintf("header %s appeared on protocol step %d, want step %d", capture.Header, index+1, expectedIndex+1))
			}
		}
		values := results[expectedIndex].Metadata[canonical]
		if len(values) != 1 {
			return nil, invalid("timing_header", fmt.Sprintf("header %s has %d values on protocol step %d, want one", capture.Header, len(values), expectedIndex+1))
		}
		milliseconds, err := strconv.ParseFloat(values[0], 64)
		duration, valid := checkedDuration(milliseconds, time.Millisecond)
		if err != nil || !valid {
			return nil, invalid("timing_header", fmt.Sprintf("header %s is not a non-negative finite duration: %q", capture.Header, values[0]))
		}
		custom[capture.Name] = duration
	}
	return custom, api.ValidationResult{Success: true}
}

func timingInvocation(operationName, header string) (int, bool) {
	switch http.CanonicalHeaderKey(header) {
	case "X-Compose-Thrift-Ms":
		return 0, operationName == composeUserTimeline || operationName == legacyComposePost
	case "X-Usertimeline-Thrift-Ms", "X-User-Timeline-Thrift-Ms":
		if operationName == userTimelineRead {
			return 0, true
		}
		return 1, operationName == composeUserTimeline || operationName == legacyComposePost
	case "X-Hometimeline-Thrift-Ms", "X-Home-Timeline-Thrift-Ms":
		if operationName == homeTimelineRead {
			return 0, true
		}
		return 2, operationName == composeUserTimeline || operationName == legacyComposePost
	default:
		return 0, false
	}
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
