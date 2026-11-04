package server

import (
	"context"
	"errors"
	"google.golang.org/protobuf/types/known/timestamppb"
	"io"
	"os"
	"sort"

	apiv1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/api/v1"
	storev1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/store/v1"
	"github.com/uw-syfi/vibesys/gateway/internal/runlog"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

// ListChatSessions reads the immutable creation record of every committed
// session. It never launches an agent or treats an unpublished record as a thread.
func (h *host) ListChatSessions(ctx context.Context, request *apiv1.ListChatSessionsRequest) (*apiv1.ListChatSessionsResponse, error) {
	dir, err := h.directory(request.GetRunId())
	if err != nil {
		return nil, err
	}
	ids, err := sessions(dir)
	if err != nil {
		return nil, rpcError(err)
	}
	if len(ids) > 4096 {
		return nil, status.Error(codes.ResourceExhausted, "too many chat sessions")
	}
	response := &apiv1.ListChatSessionsResponse{}
	var listed []*chatSummary
	for _, id := range ids {
		if err := ctx.Err(); err != nil {
			return nil, rpcError(err)
		}
		sessionDir, err := h.contentDirectory(request.GetRunId(), id)
		if err != nil {
			return nil, err
		}
		metadata, err := chatMetadata(sessionDir, id)
		if err != nil {
			return nil, rpcError(err)
		}
		if metadata != nil {
			response.Sessions = append(response.Sessions, metadata.created)
			listed = append(listed, metadata)
		}
		if proto.Size(response) > skeletonBytes {
			return nil, status.Error(codes.ResourceExhausted, "chat listing exceeds response budget")
		}
	}
	sort.SliceStable(listed, func(i, j int) bool {
		if listed[i].at.GetSeconds() != listed[j].at.GetSeconds() {
			return listed[i].at.GetSeconds() < listed[j].at.GetSeconds()
		}
		return listed[i].at.GetNanos() < listed[j].at.GetNanos()
	})
	for index, entry := range listed {
		response.Sessions[index] = entry.created
	}
	return response, nil
}

type chatSummary struct {
	created *storev1.ChatSessionCreated
	at      *timestamppb.Timestamp
}

func chatMetadata(dir, id string) (*chatSummary, error) {
	cap, bounded, err := committed(dir)
	if err != nil {
		return nil, err
	}
	if !bounded || cap == 0 {
		return nil, nil
	}
	f, err := openRegular(dir, "session.log")
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	defer f.Close()
	reader := runlog.NewReader(f)
	var result *storev1.ChatSessionCreated
	var at *timestamppb.Timestamp
	// Creation is the first record; the first question, when committed, supplies
	// the display title. Only this prefix is needed, irrespective of transcript size.
	for i := uint64(1); i <= 2 && (!bounded || i <= cap); i++ {
		payload, err := reader.Next()
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			return nil, err
		}
		record := &storev1.Record{}
		if err := proto.Unmarshal(payload, record); err != nil {
			return nil, status.Error(codes.DataLoss, "invalid chat record")
		}
		chat := record.GetChat()
		if i == 1 {
			result = chat.GetCreated()
			at = record.At
			if result == nil || result.Id != id {
				return nil, status.Error(codes.DataLoss, "chat creation identity mismatch")
			}
		} else if message := chat.GetMessage(); message != nil && message.Role == storev1.ChatRole_CHAT_ROLE_USER && result.Title == "" {
			title := []rune(message.GetText())
			if len(title) > 120 {
				title = title[:120]
			}
			result.Title = string(title)
		}
	}
	if result == nil {
		return nil, nil
	}
	return &chatSummary{created: result, at: at}, nil
}
