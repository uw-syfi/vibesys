package main

import (
	"encoding/binary"
	"errors"
	"fmt"
	"os"
	"runtime"
	"sync/atomic"
	"syscall"
	"unsafe"
)

const (
	protocolVersion = uint32(1)
	headerSize      = 4096
	laneSize        = 4096
	ringSlots       = 64
	maxLaneCount    = 256

	offsetVersion   = 8
	offsetLaneCount = 12
	offsetCapacity  = 16
	offsetScenario  = 24
	offsetRingSlots = 28
	offsetReady     = 32
	offsetStop      = 40

	requestPublishedOffset  = 0
	requestConsumedOffset   = 64
	responsePublishedOffset = 128
	responseConsumedOffset  = 192
	requestSlotsOffset      = 256
	responseSlotsOffset     = requestSlotsOffset + ringSlots*16
	requestOperationOffset  = 0
	requestValueOffset      = 8
	responseStatusOffset    = 0
	responseValueOffset     = 8
)

var protocolMagic = [8]byte{'V', 'S', 'Q', 'U', 'E', 'U', 'E', '1'}

type scenario uint32

const (
	scenarioSPSC scenario = iota + 1
	scenarioMPSC
	scenarioMPMC
)

func parseScenario(value string) (scenario, error) {
	switch value {
	case "spsc":
		return scenarioSPSC, nil
	case "mpsc":
		return scenarioMPSC, nil
	case "mpmc":
		return scenarioMPMC, nil
	default:
		return 0, fmt.Errorf("unsupported scenario %q", value)
	}
}

func (s scenario) String() string {
	switch s {
	case scenarioSPSC:
		return "spsc"
	case scenarioMPSC:
		return "mpsc"
	case scenarioMPMC:
		return "mpmc"
	default:
		return fmt.Sprintf("scenario(%d)", s)
	}
}

type operation uint32

const (
	operationEnqueue operation = iota + 1
	operationDequeue
)

type responseStatus uint32

const (
	statusEnqueued responseStatus = iota + 1
	statusFull
	statusValue
	statusEmpty
	statusError
)

type request struct {
	operation operation
	value     uint64
}

type response struct {
	status responseStatus
	value  uint64
}

type mappedRegion struct {
	path      string
	file      *os.File
	data      []byte
	laneCount int
	capacity  uint64
	scenario  scenario
	owner     bool
}

func createRegion(s scenario, capacity uint64, laneCount int) (*mappedRegion, error) {
	if capacity == 0 {
		return nil, errors.New("capacity must be greater than zero")
	}
	if laneCount <= 0 {
		return nil, errors.New("lane count must be greater than zero")
	}
	if laneCount > maxLaneCount {
		return nil, fmt.Errorf("lane count must not exceed %d", maxLaneCount)
	}

	file, err := os.CreateTemp("", "vibeserve-queue-*.shm")
	if err != nil {
		return nil, fmt.Errorf("create shared-memory file: %w", err)
	}
	cleanup := func() {
		_ = file.Close()
		_ = os.Remove(file.Name())
	}

	size := headerSize + laneCount*laneSize
	if err := file.Truncate(int64(size)); err != nil {
		cleanup()
		return nil, fmt.Errorf("size shared-memory file: %w", err)
	}
	data, err := syscall.Mmap(int(file.Fd()), 0, size, syscall.PROT_READ|syscall.PROT_WRITE, syscall.MAP_SHARED)
	if err != nil {
		cleanup()
		return nil, fmt.Errorf("map shared-memory file: %w", err)
	}

	copy(data[:len(protocolMagic)], protocolMagic[:])
	binary.LittleEndian.PutUint32(data[offsetVersion:], protocolVersion)
	binary.LittleEndian.PutUint32(data[offsetLaneCount:], uint32(laneCount))
	binary.LittleEndian.PutUint64(data[offsetCapacity:], capacity)
	binary.LittleEndian.PutUint32(data[offsetScenario:], uint32(s))
	binary.LittleEndian.PutUint32(data[offsetRingSlots:], ringSlots)

	return &mappedRegion{
		path:      file.Name(),
		file:      file,
		data:      data,
		laneCount: laneCount,
		capacity:  capacity,
		scenario:  s,
		owner:     true,
	}, nil
}

func openRegion(path string) (*mappedRegion, error) {
	file, err := os.OpenFile(path, os.O_RDWR, 0)
	if err != nil {
		return nil, fmt.Errorf("open shared-memory file: %w", err)
	}
	stat, err := file.Stat()
	if err != nil {
		_ = file.Close()
		return nil, fmt.Errorf("stat shared-memory file: %w", err)
	}
	if stat.Size() < headerSize+laneSize {
		_ = file.Close()
		return nil, errors.New("shared-memory file is too small")
	}
	data, err := syscall.Mmap(
		int(file.Fd()),
		0,
		int(stat.Size()),
		syscall.PROT_READ|syscall.PROT_WRITE,
		syscall.MAP_SHARED,
	)
	if err != nil {
		_ = file.Close()
		return nil, fmt.Errorf("map shared-memory file: %w", err)
	}

	closeOnError := func(err error) (*mappedRegion, error) {
		_ = syscall.Munmap(data)
		_ = file.Close()
		return nil, err
	}
	if string(data[:len(protocolMagic)]) != string(protocolMagic[:]) {
		return closeOnError(errors.New("invalid shared-memory protocol magic"))
	}
	if got := binary.LittleEndian.Uint32(data[offsetVersion:]); got != protocolVersion {
		return closeOnError(fmt.Errorf("unsupported shared-memory protocol version %d", got))
	}
	laneCount := int(binary.LittleEndian.Uint32(data[offsetLaneCount:]))
	if laneCount <= 0 || laneCount > maxLaneCount || headerSize+laneCount*laneSize != len(data) {
		return closeOnError(errors.New("invalid shared-memory lane count"))
	}
	capacity := binary.LittleEndian.Uint64(data[offsetCapacity:])
	if capacity == 0 {
		return closeOnError(errors.New("invalid zero queue capacity"))
	}
	s := scenario(binary.LittleEndian.Uint32(data[offsetScenario:]))
	if _, err := parseScenario(s.String()); err != nil {
		return closeOnError(err)
	}
	if got := binary.LittleEndian.Uint32(data[offsetRingSlots:]); got != ringSlots {
		return closeOnError(fmt.Errorf("unsupported shared-memory ring size %d", got))
	}

	return &mappedRegion{
		path:      path,
		file:      file,
		data:      data,
		laneCount: laneCount,
		capacity:  capacity,
		scenario:  s,
	}, nil
}

func (r *mappedRegion) close() error {
	var errs []error
	if r.data != nil {
		if err := syscall.Munmap(r.data); err != nil {
			errs = append(errs, err)
		}
		r.data = nil
	}
	if r.file != nil {
		if err := r.file.Close(); err != nil {
			errs = append(errs, err)
		}
		r.file = nil
	}
	if r.owner {
		if err := os.Remove(r.path); err != nil && !errors.Is(err, os.ErrNotExist) {
			errs = append(errs, err)
		}
	}
	return errors.Join(errs...)
}

func (r *mappedRegion) laneOffset(lane int) int {
	return headerSize + lane*laneSize
}

func atomicUint64At(data []byte, offset int) *uint64 {
	return (*uint64)(unsafe.Pointer(&data[offset]))
}

func (r *mappedRegion) ready() bool {
	return atomic.LoadUint64(atomicUint64At(r.data, offsetReady)) == 1
}

func (r *mappedRegion) markReady() {
	atomic.StoreUint64(atomicUint64At(r.data, offsetReady), 1)
}

func (r *mappedRegion) stopped() bool {
	return atomic.LoadUint64(atomicUint64At(r.data, offsetStop)) == 1
}

func (r *mappedRegion) stop() {
	atomic.StoreUint64(atomicUint64At(r.data, offsetStop), 1)
}

func (r *mappedRegion) publish(lane int, sequence uint64, req request) error {
	if lane < 0 || lane >= r.laneCount {
		return fmt.Errorf("lane %d is outside [0, %d)", lane, r.laneCount)
	}
	offset := r.laneOffset(lane)
	consumed := atomic.LoadUint64(atomicUint64At(r.data, offset+requestConsumedOffset))
	if sequence-consumed > ringSlots {
		return errors.New("request ring is full")
	}
	slot := offset + requestSlotsOffset + int((sequence-1)%ringSlots)*16
	binary.LittleEndian.PutUint32(r.data[slot+requestOperationOffset:], uint32(req.operation))
	binary.LittleEndian.PutUint64(r.data[slot+requestValueOffset:], req.value)
	atomic.StoreUint64(atomicUint64At(r.data, offset+requestPublishedOffset), sequence)
	return nil
}

func (r *mappedRegion) response(lane int, sequence uint64) (response, bool) {
	offset := r.laneOffset(lane)
	if atomic.LoadUint64(atomicUint64At(r.data, offset+responsePublishedOffset)) < sequence {
		return response{}, false
	}
	slot := offset + responseSlotsOffset + int((sequence-1)%ringSlots)*16
	return response{
		status: responseStatus(binary.LittleEndian.Uint32(r.data[slot+responseStatusOffset:])),
		value:  binary.LittleEndian.Uint64(r.data[slot+responseValueOffset:]),
	}, true
}

func (r *mappedRegion) consumeResponse(lane int, sequence uint64) {
	offset := r.laneOffset(lane)
	atomic.StoreUint64(atomicUint64At(r.data, offset+responseConsumedOffset), sequence)
}

func (r *mappedRegion) waitForRequest(lane int, previous uint64) (uint64, request, bool) {
	offset := r.laneOffset(lane)
	for {
		published := atomic.LoadUint64(atomicUint64At(r.data, offset+requestPublishedOffset))
		if published > previous {
			sequence := previous + 1
			slot := offset + requestSlotsOffset + int((sequence-1)%ringSlots)*16
			return sequence, request{
				operation: operation(binary.LittleEndian.Uint32(r.data[slot+requestOperationOffset:])),
				value:     binary.LittleEndian.Uint64(r.data[slot+requestValueOffset:]),
			}, true
		}
		if r.stopped() {
			return 0, request{}, false
		}
		runtime.Gosched()
	}
}

func (r *mappedRegion) respond(lane int, sequence uint64, resp response) {
	offset := r.laneOffset(lane)
	slot := offset + responseSlotsOffset + int((sequence-1)%ringSlots)*16
	binary.LittleEndian.PutUint32(r.data[slot+responseStatusOffset:], uint32(resp.status))
	binary.LittleEndian.PutUint64(r.data[slot+responseValueOffset:], resp.value)
	atomic.StoreUint64(atomicUint64At(r.data, offset+requestConsumedOffset), sequence)
	atomic.StoreUint64(atomicUint64At(r.data, offset+responsePublishedOffset), sequence)
}
