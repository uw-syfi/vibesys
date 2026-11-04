package server

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"path/filepath"

	apiv1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/api/v1"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

func (h *host) GetBlobs(ctx context.Context, request *apiv1.GetBlobsRequest) (*apiv1.GetBlobsResponse, error) {
	dir, err := h.contentDirectory(request.GetRunId(), request.GetSessionId())
	if err != nil {
		return nil, err
	}
	response := &apiv1.GetBlobsResponse{}
	remaining := blobBytes
	for _, hash := range request.GetSha256() {
		if err := ctx.Err(); err != nil {
			return nil, rpcError(err)
		}
		result := &apiv1.BlobResult{Sha256: hash}
		data, err := readFile(dir, filepath.Join("blobs", hash), remaining)
		if err == nil {
			sum := sha256.Sum256(data)
			if hex.EncodeToString(sum[:]) != hash {
				err = status.Error(codes.DataLoss, "blob sha256 mismatch")
			}
		}
		if err != nil {
			failure := status.Convert(rpcError(err))
			result.ErrorCode = int32(failure.Code())
			result.ErrorMessage = failure.Message()
		} else {
			result.Data = data
			remaining -= len(data)
		}
		response.Blobs = append(response.Blobs, result)
	}
	return response, nil
}
