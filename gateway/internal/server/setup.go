package server

import (
	"context"
	"errors"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"

	apiv1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/api/v1"
	hostv1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/host/v1"
	"github.com/uw-syfi/vibesys/gateway/internal/hostcommand"
	"github.com/uw-syfi/vibesys/gateway/internal/hostoperations"
	"github.com/uw-syfi/vibesys/gateway/schemahash"
	"google.golang.org/protobuf/proto"
)

const (
	pathScanCap     = 10000
	pathResponseCap = 200
)

// completePaths lists one directory on this machine. Product hints come from
// bounded InspectCandidate calls, not from parsing project files here.
func (h *host) completePaths(ctx context.Context, request *apiv1.ReadRequest, deadline time.Duration) (*apiv1.ReadResponse, error) {
	query := request.GetCompletePaths()
	if query == nil {
		return readFailure(hostv1.FailureCategory_FAILURE_CATEGORY_INVALID_INPUT, "HostRead.method is required"), nil
	}
	callCtx, cancel := context.WithTimeout(ctx, deadline)
	defer cancel()
	directory, partial, err := splitPrefix(query.GetPrefix())
	if err != nil {
		return readFailure(hostv1.FailureCategory_FAILURE_CATEGORY_INVALID_INPUT, err.Error()), nil
	}
	info, err := os.Stat(directory)
	if err != nil {
		return pathFailure(err), nil
	}
	if !info.IsDir() {
		return readFailure(hostv1.FailureCategory_FAILURE_CATEGORY_INVALID_INPUT, directory+" is not a directory"), nil
	}
	names, incomplete, err := scanDirectories(directory, pathScanCap)
	if err != nil {
		return pathFailure(err), nil
	}
	matched := make([]string, 0, len(names))
	for _, name := range names {
		if strings.HasPrefix(name, partial) {
			matched = append(matched, name)
		}
	}
	sort.Strings(matched)
	truncated := incomplete || len(matched) > pathResponseCap
	if len(matched) > pathResponseCap {
		matched = matched[:pathResponseCap]
	}
	response := &hostv1.CompletePathsResponse{
		RequestId: query.GetRequestId(),
		Directory: directory,
		Truncated: truncated,
	}
	for _, name := range matched {
		if callCtx.Err() != nil {
			response.Truncated = true
			break
		}
		path := filepath.Join(directory, name)
		checkout, project, git := h.candidateHints(callCtx, path)
		response.Entries = append(response.Entries, &hostv1.CompletedPath{
			Name: name, Path: path, VibesysCheckout: checkout, VibesysProject: project, GitRepository: git,
		})
	}
	return &apiv1.ReadResponse{Result: &apiv1.ReadResponse_CompletePaths{CompletePaths: response}}, nil
}

func pathFailure(err error) *apiv1.ReadResponse {
	switch {
	case os.IsNotExist(err):
		return readFailure(hostv1.FailureCategory_FAILURE_CATEGORY_NOT_FOUND, err.Error())
	case os.IsPermission(err):
		return readFailure(hostv1.FailureCategory_FAILURE_CATEGORY_PERMISSION_DENIED, err.Error())
	default:
		return readFailure(hostv1.FailureCategory_FAILURE_CATEGORY_INVALID_INPUT, err.Error())
	}
}

func splitPrefix(prefix string) (string, string, error) {
	expanded, err := expandUserPath(prefix)
	if err != nil {
		return "", "", err
	}
	// Decide before Clean drops a trailing slash: "" , "~" and ".../" list that directory.
	if prefix == "" || prefix == "~" || strings.HasSuffix(prefix, "/") {
		return expanded, "", nil
	}
	return filepath.Dir(expanded), filepath.Base(expanded), nil
}

func expandUserPath(path string) (string, error) {
	if strings.ContainsAny(path, "\x00\n\r") || len(path) > 4096 {
		return "", errors.New("path is not usable")
	}
	if path == "~" || strings.HasPrefix(path, "~/") {
		home, err := os.UserHomeDir()
		if err != nil {
			return "", err
		}
		rest := strings.TrimPrefix(strings.TrimPrefix(path, "~"), "/")
		return filepath.Clean(filepath.Join(home, rest)), nil
	}
	if strings.HasPrefix(path, "~") {
		return "", errors.New("path is not usable")
	}
	if filepath.IsAbs(path) {
		return filepath.Clean(path), nil
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return "", err
	}
	return filepath.Clean(filepath.Join(home, path)), nil
}

func scanDirectories(directory string, limit int) ([]string, bool, error) {
	handle, err := os.Open(directory)
	if err != nil {
		return nil, false, err
	}
	defer handle.Close()
	infos, err := handle.Readdir(limit)
	incomplete := len(infos) == limit
	if err != nil && !errors.Is(err, io.EOF) && len(infos) == 0 {
		return nil, false, err
	}
	names := make([]string, 0, len(infos))
	for _, info := range infos {
		path := filepath.Join(directory, info.Name())
		if isDirectory(path, info) {
			names = append(names, info.Name())
		}
	}
	return names, incomplete, nil
}

func isDirectory(path string, info os.FileInfo) bool {
	if info.IsDir() {
		return true
	}
	if info.Mode()&os.ModeSymlink == 0 {
		return false
	}
	followed, err := os.Stat(path)
	return err == nil && followed.IsDir()
}

func (h *host) inspectCandidate(ctx context.Context, request *apiv1.ReadRequest, deadline time.Duration) (*apiv1.ReadResponse, error) {
	query := request.GetInspectCandidate()
	if query == nil {
		return readFailure(hostv1.FailureCategory_FAILURE_CATEGORY_INVALID_INPUT, "HostRead.method is required"), nil
	}
	candidate, err := expandUserPath(query.GetPath())
	if err != nil {
		return readFailure(hostv1.FailureCategory_FAILURE_CATEGORY_INVALID_INPUT, err.Error()), nil
	}
	info, err := os.Stat(candidate)
	if err != nil {
		if os.IsNotExist(err) {
			return notFoundCandidate(query), nil
		}
		return pathFailure(err), nil
	}
	if !info.IsDir() {
		return readFailure(hostv1.FailureCategory_FAILURE_CATEGORY_INVALID_INPUT, candidate+" is not a directory"), nil
	}
	executable := filepath.Join(candidate, ".venv", "bin", "vibesys")
	exe, err := os.Stat(executable)
	if err != nil || exe.IsDir() || exe.Mode()&0o111 == 0 {
		if err != nil && os.IsPermission(err) {
			return pathFailure(err), nil
		}
		return readFailure(hostv1.FailureCategory_FAILURE_CATEGORY_NOT_FOUND, "candidate executable is missing"), nil
	}
	input, err := proto.Marshal(request)
	if err != nil {
		return readFailure(hostv1.FailureCategory_FAILURE_CATEGORY_INVALID_INPUT, err.Error()), nil
	}
	callCtx, cancel := context.WithTimeout(ctx, deadline)
	defer cancel()
	result, err := h.config.HostRunner.Run(callCtx, hostcommand.Command{
		Executable: executable,
		Args:       []string{"host", "inspect-candidate"},
		Input:      input,
		StateRoot:  h.config.Root,
	})
	if errors.Is(err, context.DeadlineExceeded) {
		return readFailure(hostv1.FailureCategory_FAILURE_CATEGORY_DEADLINE_EXCEEDED, "host read deadline exceeded"), nil
	}
	if errors.Is(err, context.Canceled) {
		return readFailure(hostv1.FailureCategory_FAILURE_CATEGORY_CANCELLED, "host read cancelled"), nil
	}
	if err != nil || result.ExitCode != 0 {
		return readFailure(hostv1.FailureCategory_FAILURE_CATEGORY_INTERNAL, "vibesys host inspect-candidate failed"), nil
	}
	response := &apiv1.ReadResponse{}
	if err := proto.Unmarshal(result.Output, response); err != nil || response.GetInspectCandidate() == nil && response.GetFailure() == nil {
		return readFailure(hostv1.FailureCategory_FAILURE_CATEGORY_INVALID_INPUT, "inspect-candidate returned invalid protobuf"), nil
	}
	if failure := response.GetFailure(); failure != nil {
		return response, nil
	}
	if response.GetInspectCandidate().GetSchemaHash() != schemahash.APISchemaHash {
		return readFailure(hostv1.FailureCategory_FAILURE_CATEGORY_CONFIGURATION, "incompatible schema hash"), nil
	}
	if !hostoperations.HostReadResponseValid(request, response) {
		return readFailure(hostv1.FailureCategory_FAILURE_CATEGORY_INVALID_INPUT, "inspect-candidate returned a different identity"), nil
	}
	return response, nil
}

func (h *host) candidateHints(ctx context.Context, path string) (bool, bool, bool) {
	if h.config.HostRunner == nil || ctx.Err() != nil {
		return false, false, false
	}
	executable := filepath.Join(path, ".venv", "bin", "vibesys")
	exe, err := os.Stat(executable)
	if err != nil || exe.IsDir() || exe.Mode()&0o111 == 0 {
		return false, false, false
	}
	response, err := h.inspectCandidate(ctx, &apiv1.ReadRequest{
		Checkout: path,
		Method: &apiv1.ReadRequest_InspectCandidate{InspectCandidate: &hostv1.InspectCandidateRequest{
			RequestId: "path-hint", Path: path, SelectionRevision: 1,
		}},
	}, time.Second)
	got := response.GetInspectCandidate()
	if err != nil || got == nil {
		return false, false, false
	}
	return got.GetVibesysCheckout(), got.GetVibesysProject(), got.GetGitRepository()
}

func notFoundCandidate(query *hostv1.InspectCandidateRequest) *apiv1.ReadResponse {
	return &apiv1.ReadResponse{Result: &apiv1.ReadResponse_InspectCandidate{InspectCandidate: &hostv1.InspectCandidateResponse{
		RequestId:         query.GetRequestId(),
		SelectionRevision: query.GetSelectionRevision(),
		SchemaHash:        schemahash.APISchemaHash,
		State:             hostv1.CandidateState_CANDIDATE_STATE_NOT_FOUND,
		MinimumVersion:    hostoperations.MinCheckoutVersion,
	}}}
}
