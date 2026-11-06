package evaluator

import (
	"bytes"
	"compress/zlib"
	"encoding/base64"
	"encoding/binary"
	"fmt"
	"io"
	"math"
	"math/bits"
)

// HDR histograms in the standard compressed V2 encoding, as loadgen writes them. The layout and
// the percentile rule are HdrHistogram's: a percentile is the highest value equivalent to the
// first bucket whose cumulative count reaches int(p/100*total + 0.5). The numbers are those of
// the Python implementation this replaces (fixtures/hdr.json pins them for both languages).

const (
	v2Cookie           = 0x1C849303
	v2CompressedCookie = 0x1C849304
	wordSizeBits       = 0x10
	cookieMask         = ^int32(0xF0)
	hdrHeaderSize      = 4 + 4 + 4 + 4 + 8 + 8 + 8
	maxFigures         = 5
	lastByte           = 8 // the ninth byte of a 64-bit LEB128 value carries 8 bits
)

// Histogram holds counts of values in HdrHistogram buckets.
type Histogram struct {
	lowest, highest int64
	figures         int
	counts          []int64

	unitMagnitude               int
	subBucketHalfCountMagnitude int
	subBucketHalfCount          int64
	subBucketMask               int64
}

// NewHistogram is an empty histogram for values lowest..highest with the given significant
// figures (1 to 5).
func NewHistogram(lowest, highest int64, figures int) (*Histogram, error) {
	if lowest < 1 || highest < 2*lowest || figures < 1 || figures > maxFigures {
		return nil, fmt.Errorf("invalid histogram range %d..%d (%d figures)", lowest, highest, figures)
	}
	h := &Histogram{lowest: lowest, highest: highest, figures: figures}
	largestSingleUnit := 2 * math.Pow10(figures)
	subBucketCountMagnitude := int(math.Ceil(math.Log2(largestSingleUnit)))
	h.subBucketHalfCountMagnitude = max(subBucketCountMagnitude, 1) - 1
	h.unitMagnitude = bits.Len64(uint64(lowest)) - 1
	subBucketCount := int64(1) << (h.subBucketHalfCountMagnitude + 1)
	h.subBucketHalfCount = subBucketCount / 2
	h.subBucketMask = (subBucketCount - 1) << h.unitMagnitude
	smallestUntrackable := subBucketCount << h.unitMagnitude
	bucketCount := 1
	for smallestUntrackable <= highest {
		if smallestUntrackable > 1<<62 {
			bucketCount++
			break
		}
		smallestUntrackable <<= 1
		bucketCount++
	}
	h.counts = make([]int64, (bucketCount+1)*int(h.subBucketHalfCount))
	return h, nil
}

// Total is the number of values recorded.
func (h *Histogram) Total() int64 {
	var total int64
	for _, c := range h.counts {
		total += c
	}
	return total
}

// Max is the highest value recorded, as the libraries report it (its bucket's highest); 0 if empty.
func (h *Histogram) Max() int64 {
	for i := len(h.counts) - 1; i >= 0; i-- {
		if h.counts[i] != 0 {
			return h.highestEquivalent(h.value(i))
		}
	}
	return 0
}

func (h *Histogram) index(value int64) int {
	pow2ceiling := bits.Len64(uint64(value | h.subBucketMask))
	bucket := pow2ceiling - h.unitMagnitude - (h.subBucketHalfCountMagnitude + 1)
	subBucket := value >> (bucket + h.unitMagnitude)
	return ((bucket + 1) << h.subBucketHalfCountMagnitude) + int(subBucket-h.subBucketHalfCount)
}

func (h *Histogram) value(index int) int64 {
	bucket := (index >> h.subBucketHalfCountMagnitude) - 1
	subBucket := int64(index&(int(h.subBucketHalfCount)-1)) + h.subBucketHalfCount
	if bucket < 0 {
		subBucket -= h.subBucketHalfCount
		bucket = 0
	}
	return subBucket << (bucket + h.unitMagnitude)
}

func (h *Histogram) highestEquivalent(value int64) int64 {
	pow2ceiling := bits.Len64(uint64(value | h.subBucketMask))
	bucket := pow2ceiling - h.unitMagnitude - (h.subBucketHalfCountMagnitude + 1)
	subBucket := value >> (bucket + h.unitMagnitude)
	adjusted := bucket
	if subBucket >= 2*h.subBucketHalfCount {
		adjusted = bucket + 1
	}
	lowest := subBucket << (bucket + h.unitMagnitude)
	return lowest + (int64(1) << (h.unitMagnitude + adjusted)) - 1
}

// Record records value (clamped to the trackable range) count times.
func (h *Histogram) Record(value, count int64) {
	value = min(max(value, 0), h.highest)
	h.counts[h.index(value)] += count
}

// Percentile is the value at percentile p (0 to 100); 0 for an empty histogram.
func (h *Histogram) Percentile(p float64) int64 {
	total := h.Total()
	if total == 0 {
		return 0
	}
	target := int64(min(max(p, 0.0), 100.0)/100.0*float64(total) + 0.5)
	var running int64
	for i, c := range h.counts {
		running += c
		if running >= target {
			return h.highestEquivalent(h.value(i))
		}
	}
	return 0
}

// Merge adds other's counts to h. Histograms of one layout add bucket by bucket; otherwise each
// of other's buckets is recorded at its lowest equivalent value.
func (h *Histogram) Merge(other *Histogram) {
	if h.lowest == other.lowest && h.highest == other.highest && h.figures == other.figures {
		for i, c := range other.counts {
			h.counts[i] += c
		}
		return
	}
	for i, c := range other.counts {
		if c != 0 {
			h.Record(other.value(i), c)
		}
	}
}

// DecodeHistogram decodes a compressed V2 histogram: the raw bytes loadgen writes, or the base64
// text the Go and Java libraries print.
func DecodeHistogram(data []byte) (*Histogram, error) {
	if bytes.HasPrefix(data, []byte("HISTF")) {
		raw, err := base64.StdEncoding.DecodeString(string(data))
		if err != nil {
			return nil, fmt.Errorf("histogram: bad base64 (%v)", err)
		}
		data = raw
	}
	if len(data) < 8 {
		return nil, fmt.Errorf("histogram: too short")
	}
	cookie := int32(binary.BigEndian.Uint32(data))
	length := int64(int32(binary.BigEndian.Uint32(data[4:])))
	if cookie&cookieMask != v2CompressedCookie {
		return nil, fmt.Errorf("histogram: cookie %#x is not the compressed V2 cookie", uint32(cookie))
	}
	end := min(8+max(length, 0), int64(len(data)))
	zr, err := zlib.NewReader(bytes.NewReader(data[8:end]))
	if err != nil {
		return nil, fmt.Errorf("histogram: bad compression (%v)", err)
	}
	payload, err := io.ReadAll(zr)
	if err != nil {
		return nil, fmt.Errorf("histogram: bad compression (%v)", err)
	}
	if len(payload) < hdrHeaderSize {
		return nil, fmt.Errorf("histogram: header too short")
	}
	inner := int32(binary.BigEndian.Uint32(payload))
	countsLength := int64(int32(binary.BigEndian.Uint32(payload[4:])))
	digits := int(int32(binary.BigEndian.Uint32(payload[12:])))
	lowest := int64(binary.BigEndian.Uint64(payload[16:]))
	highest := int64(binary.BigEndian.Uint64(payload[24:]))
	if inner&cookieMask != v2Cookie {
		return nil, fmt.Errorf("histogram: inner cookie %#x is not the V2 cookie", uint32(inner))
	}
	h, err := NewHistogram(lowest, highest, digits)
	if err != nil {
		return nil, fmt.Errorf("histogram: %v", err)
	}
	body := payload[hdrHeaderSize:min(int64(len(payload)), hdrHeaderSize+max(countsLength, 0))]
	values, err := zigzagVarints(body)
	if err != nil {
		return nil, err
	}
	index := 0
	for _, v := range values {
		if v < 0 {
			index -= int(v)
			continue
		}
		if index >= len(h.counts) {
			return nil, fmt.Errorf("histogram: more counts than its range holds")
		}
		h.counts[index] = v
		index++
	}
	return h, nil
}

// Encode is the histogram in the compressed V2 encoding (raw bytes, not base64).
func (h *Histogram) Encode() []byte {
	var body []byte
	zeros := int64(0)
	last := -1
	for i, c := range h.counts {
		if c != 0 {
			last = i
		}
	}
	for _, c := range h.counts[:last+1] {
		if c == 0 {
			zeros++
			continue
		}
		if zeros > 0 {
			body = appendZigzag(body, -zeros)
			zeros = 0
		}
		body = appendZigzag(body, c)
	}
	header := make([]byte, hdrHeaderSize)
	binary.BigEndian.PutUint32(header, uint32(v2Cookie|wordSizeBits))
	binary.BigEndian.PutUint32(header[4:], uint32(len(body)))
	binary.BigEndian.PutUint32(header[8:], 0)
	binary.BigEndian.PutUint32(header[12:], uint32(h.figures))
	binary.BigEndian.PutUint64(header[16:], uint64(h.lowest))
	binary.BigEndian.PutUint64(header[24:], uint64(h.highest))
	binary.BigEndian.PutUint64(header[32:], math.Float64bits(1.0))
	var buf bytes.Buffer
	zw := zlib.NewWriter(&buf)
	_, _ = zw.Write(append(header, body...))
	_ = zw.Close()
	out := make([]byte, 8, 8+buf.Len())
	binary.BigEndian.PutUint32(out, uint32(v2CompressedCookie|wordSizeBits))
	binary.BigEndian.PutUint32(out[4:], uint32(buf.Len()))
	return append(out, buf.Bytes()...)
}

func appendZigzag(out []byte, value int64) []byte {
	unsigned := uint64(value) << 1
	if value < 0 {
		unsigned = uint64((value << 1) ^ (value >> 63))
	}
	for range lastByte {
		if unsigned < 0x80 {
			return append(out, byte(unsigned))
		}
		out = append(out, byte(unsigned&0x7F)|0x80)
		unsigned >>= 7
	}
	return append(out, byte(unsigned&0xFF))
}

func zigzagVarints(data []byte) ([]int64, error) {
	var out []int64
	position := 0
	for position < len(data) {
		var value uint64
		shift := 0
		for n := 0; n <= lastByte; n++ {
			if position >= len(data) {
				return nil, fmt.Errorf("histogram: truncated count")
			}
			b := data[position]
			position++
			if n == lastByte {
				value |= uint64(b) << shift
				break
			}
			value |= uint64(b&0x7F) << shift
			shift += 7
			if b < 0x80 {
				break
			}
		}
		out = append(out, int64(value>>1)^-int64(value&1))
	}
	return out, nil
}

// MergeHistograms decodes the histograms (raw or base64) and adds them up; the first one's layout
// is the result's. With none, the result is an empty histogram of loadgen's layout (1 us to an
// hour, 3 figures).
func MergeHistograms(encoded ...[]byte) (*Histogram, error) {
	var merged *Histogram
	for i, data := range encoded {
		h, err := DecodeHistogram(data)
		if err != nil {
			return nil, fmt.Errorf("histogram %d: %w", i, err)
		}
		if merged == nil {
			merged = h
			continue
		}
		merged.Merge(h)
	}
	if merged == nil {
		return NewHistogram(1, 3_600_000_000, 3)
	}
	return merged, nil
}
