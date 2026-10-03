package main

import (
	"encoding/json"
	"fmt"
	"os"
	"regexp"
)

var gitHubSHA = regexp.MustCompile(`^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$`)

type gitHubEventPayload struct {
	PullRequest *struct {
		Base *struct {
			SHA string `json:"sha"`
		} `json:"base"`
		Head *struct {
			SHA string `json:"sha"`
		} `json:"head"`
	} `json:"pull_request"`
	MergeGroup *struct {
		BaseSHA string `json:"base_sha"`
		HeadSHA string `json:"head_sha"`
	} `json:"merge_group"`
	Before string `json:"before"`
	After  string `json:"after"`
}

func gitHubEventRevisions() (base, head, event string, err error) {
	event = os.Getenv("GITHUB_EVENT_NAME")
	path := os.Getenv("GITHUB_EVENT_PATH")
	if event == "" || path == "" {
		return "", "", "", fmt.Errorf("--github-event requires GITHUB_EVENT_NAME and GITHUB_EVENT_PATH")
	}
	if event != "pull_request" && event != "merge_group" && event != "push" {
		return "", "", "", fmt.Errorf("unsupported GitHub event %q", event)
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		return "", "", "", fmt.Errorf("read GitHub event %q: %w", path, err)
	}
	var payload gitHubEventPayload
	if err := json.Unmarshal(raw, &payload); err != nil {
		return "", "", "", fmt.Errorf("malformed GitHub event %q: %w", path, err)
	}
	switch event {
	case "pull_request":
		if payload.PullRequest == nil || payload.PullRequest.Base == nil || payload.PullRequest.Head == nil {
			return "", "", "", fmt.Errorf("pull_request event requires pull_request.base.sha and pull_request.head.sha")
		}
		base, head = payload.PullRequest.Base.SHA, payload.PullRequest.Head.SHA
	case "merge_group":
		if payload.MergeGroup == nil {
			return "", "", "", fmt.Errorf("merge_group event requires merge_group.base_sha and merge_group.head_sha")
		}
		base, head = payload.MergeGroup.BaseSHA, payload.MergeGroup.HeadSHA
	case "push":
		base, head = payload.Before, payload.After
	}
	if !gitHubSHA.MatchString(base) || !gitHubSHA.MatchString(head) {
		return "", "", "", fmt.Errorf("%s event requires valid base and head SHAs", event)
	}
	return base, head, event, nil
}
