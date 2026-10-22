package server_test

import (
	"context"
	"os"
	"path/filepath"
	"testing"

	apiv1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/api/v1"
	hostv1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/host/v1"
	"github.com/uw-syfi/vibesys/gateway/internal/hostcommand"
	"github.com/uw-syfi/vibesys/gateway/internal/server"
	"github.com/uw-syfi/vibesys/gateway/schemahash"
	"google.golang.org/protobuf/proto"
)

func TestCompletePathsListsDirectoriesAndKeepsHintsFalseWithoutAnExecutable(t *testing.T) {
	root := t.TempDir()
	if err := os.Mkdir(filepath.Join(root, "project"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "notes.txt"), []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	spawned := false
	runner := hostcommand.FakeRunner{Program: func(context.Context, hostcommand.Command) (hostcommand.Result, error) {
		spawned = true
		return hostcommand.Result{}, nil
	}}
	f := startWithConfig(t, server.Config{HostRunner: runner})
	response, err := client(t, f, false).Read(f.ctx, &apiv1.ReadRequest{
		Checkout: "~",
		Method: &apiv1.ReadRequest_CompletePaths{CompletePaths: &hostv1.CompletePathsRequest{
			RequestId: "request-a", Prefix: root + "/",
		}},
	})
	got := response.GetCompletePaths()
	if err != nil || spawned || got.GetDirectory() != root || got.GetTruncated() || len(got.GetEntries()) != 1 {
		t.Fatalf("listing: %v %v spawned=%v", response, err, spawned)
	}
	entry := got.GetEntries()[0]
	if entry.GetName() != "project" || entry.GetVibesysCheckout() || entry.GetVibesysProject() || entry.GetGitRepository() {
		t.Fatalf("entry: %+v", entry)
	}
}

func TestInspectCandidateReportsAMissingExecutableWithoutRunningIt(t *testing.T) {
	root := t.TempDir()
	spawned := false
	runner := hostcommand.FakeRunner{Program: func(context.Context, hostcommand.Command) (hostcommand.Result, error) {
		spawned = true
		return hostcommand.Result{}, nil
	}}
	f := startWithConfig(t, server.Config{HostRunner: runner})
	response, err := client(t, f, false).Read(f.ctx, &apiv1.ReadRequest{
		Checkout: root,
		Method: &apiv1.ReadRequest_InspectCandidate{InspectCandidate: &hostv1.InspectCandidateRequest{
			RequestId: "request-a", Path: root, SelectionRevision: 1,
		}},
	})
	failure := response.GetFailure()
	if err != nil || spawned || failure.GetCategory() != hostv1.FailureCategory_FAILURE_CATEGORY_NOT_FOUND || failure.GetMessage() != "candidate executable is missing" {
		t.Fatalf("missing executable: %v %v spawned=%v", response, err, spawned)
	}
}

func TestInspectCandidateRefusesADifferentSchemaHash(t *testing.T) {
	root := t.TempDir()
	executable := filepath.Join(root, ".venv", "bin", "vibesys")
	if err := os.MkdirAll(filepath.Dir(executable), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(executable, []byte("#!/bin/sh\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	runner := hostcommand.FakeRunner{Program: func(_ context.Context, command hostcommand.Command) (hostcommand.Result, error) {
		if command.Executable != executable || len(command.Args) != 2 || command.Args[0] != "host" || command.Args[1] != "inspect-candidate" {
			t.Errorf("command: %+v", command)
		}
		output, err := proto.Marshal(&apiv1.ReadResponse{Result: &apiv1.ReadResponse_InspectCandidate{InspectCandidate: &hostv1.InspectCandidateResponse{
			RequestId: "request-a", SelectionRevision: 1, SchemaHash: "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
			State: hostv1.CandidateState_CANDIDATE_STATE_NOT_FOUND, MinimumVersion: "0.1.1",
		}}})
		return hostcommand.Result{Output: output}, err
	}}
	f := startWithConfig(t, server.Config{HostRunner: runner})
	response, err := client(t, f, false).Read(f.ctx, &apiv1.ReadRequest{
		Checkout: root,
		Method: &apiv1.ReadRequest_InspectCandidate{InspectCandidate: &hostv1.InspectCandidateRequest{
			RequestId: "request-a", Path: root, SelectionRevision: 1,
		}},
	})
	if err != nil || response.GetFailure().GetCategory() != hostv1.FailureCategory_FAILURE_CATEGORY_UNSUPPORTED {
		t.Fatalf("hash: %v %v", response, err)
	}
}

func TestInspectCandidateEchoesTheSelectionAndFillsPathHints(t *testing.T) {
	root := t.TempDir()
	child := filepath.Join(root, "checkout")
	executable := filepath.Join(child, ".venv", "bin", "vibesys")
	if err := os.MkdirAll(filepath.Dir(executable), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(executable, []byte("#!/bin/sh\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	runner := hostcommand.FakeRunner{Program: func(_ context.Context, command hostcommand.Command) (hostcommand.Result, error) {
		request := &apiv1.ReadRequest{}
		if err := proto.Unmarshal(command.Input, request); err != nil {
			return hostcommand.Result{}, err
		}
		query := request.GetInspectCandidate()
		output, err := proto.Marshal(&apiv1.ReadResponse{Result: &apiv1.ReadResponse_InspectCandidate{InspectCandidate: &hostv1.InspectCandidateResponse{
			RequestId: query.GetRequestId(), SelectionRevision: query.GetSelectionRevision(), SchemaHash: schemahash.APISchemaHash,
			State: hostv1.CandidateState_CANDIDATE_STATE_FOUND, MinimumVersion: "0.1.1",
			VibesysCheckout: true, VibesysProject: true, GitRepository: false,
			Identity: &hostv1.CandidateIdentity{Root: child, Version: "0.1.1", Compatible: true},
		}}})
		return hostcommand.Result{Output: output}, err
	}}
	f := startWithConfig(t, server.Config{HostRunner: runner})
	inspected, err := client(t, f, false).Read(f.ctx, &apiv1.ReadRequest{
		Checkout: child,
		Method: &apiv1.ReadRequest_InspectCandidate{InspectCandidate: &hostv1.InspectCandidateRequest{
			RequestId: "request-a", Path: child, SelectionRevision: 4,
		}},
	})
	got := inspected.GetInspectCandidate()
	if err != nil || got.GetSelectionRevision() != 4 || !got.GetVibesysCheckout() {
		t.Fatalf("inspect: %v %v", inspected, err)
	}
	listed, err := client(t, f, false).Read(f.ctx, &apiv1.ReadRequest{
		Checkout: "~",
		Method: &apiv1.ReadRequest_CompletePaths{CompletePaths: &hostv1.CompletePathsRequest{
			RequestId: "request-b", Prefix: root + "/",
		}},
	})
	entries := listed.GetCompletePaths().GetEntries()
	if err != nil || len(entries) != 1 || !entries[0].GetVibesysCheckout() || !entries[0].GetVibesysProject() || entries[0].GetGitRepository() {
		t.Fatalf("hints: %v %v", listed, err)
	}
}
