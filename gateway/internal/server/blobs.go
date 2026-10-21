package server

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"path/filepath"
	"regexp"

	apiv1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/api/v1"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

var blobHash = regexp.MustCompile(`^[0-9a-f]{64}$`)

func (h *host) GetBlobs(ctx context.Context, request *apiv1.GetBlobsRequest) (*apiv1.GetBlobsResponse, error) {
	dir, err := h.contentDirectory(request.GetRunId(), request.GetSessionId())
	if err != nil {
		return nil, err
	}
	if len(request.GetSha256()) > 256 {
		return nil, status.Error(codes.InvalidArgument, "at most 256 hashes per GetBlobs request")
	}
	response := &apiv1.GetBlobsResponse{}
	remaining := blobBytes
	for _, hash := range request.GetSha256() {
		if err := ctx.Err(); err != nil {
			return nil, rpcError(err)
		}
		result := &apiv1.BlobResult{Sha256: hash}
		var data []byte
		var err error
		if !blobHash.MatchString(hash) {
			err = status.Error(codes.InvalidArgument, "sha256 must be 64 lowercase hexadecimal characters")
		} else {
			data, err = readFile(dir, filepath.Join("blobs", hash), remaining)
			if err == nil {
				sum := sha256.Sum256(data)
				if hex.EncodeToString(sum[:]) != hash {
					err = status.Error(codes.DataLoss, "blob sha256 mismatch")
				}
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
