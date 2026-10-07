// Public example: a cooperative pipeline with two producers and one consumer.
// Exit 0 means exact payload checks and stable-softmax checks passed; 1 is failure.
#include "protocols.cuh"

#include <cooperative_groups.h>
#include <cstdio>
#include <stdexcept>
#include <vector>

using namespace megakernel_examples;
constexpr int THREADS = 256;
constexpr int PRODUCERS = 2;
constexpr int TILES = 5;

void check(cudaError_t status) {
    if (status != cudaSuccess) throw std::runtime_error(cudaGetErrorString(status));
}

template <typename T>
class DeviceBuffer {
public:
    explicit DeviceBuffer(size_t count) { check(cudaMalloc(&data, count * sizeof(T))); }
    ~DeviceBuffer() { cudaFree(data); }
    DeviceBuffer(const DeviceBuffer&) = delete;
    DeviceBuffer& operator=(const DeviceBuffer&) = delete;
    T* data = nullptr;
};

__global__ void pipeline(const float* input, float* output, Channel* channels,
                         float* payload, int epochs) {
    __shared__ alignas(16) float tiles[2][THREADS];
    __shared__ BulkBarrier barriers[2];
    int t = threadIdx.x;
    if (blockIdx.x == 0 && t < PRODUCERS) {
        channels[t].ready = 0;
        channels[t].consumed = 0;
    }
    cooperative_groups::this_grid().sync();  // Initialize inside this one launch.
    if (blockIdx.x < PRODUCERS) {
        bulk_init_cta(barriers[0]);
        bulk_init_cta(barriers[1]);
    }
    for (int epoch = 1; epoch <= epochs; ++epoch) {
        if (blockIdx.x < PRODUCERS) {
            int producer = blockIdx.x;
            await_cta(&channels[producer].consumed, epoch - 1);
            const float* source = input +
                ((epoch - 1) * PRODUCERS + producer) * TILES * THREADS;
            bulk_begin_cta(barriers[0], tiles[0], source, sizeof(tiles[0]));
            float sum = 0.0f;
            for (int tile = 0; tile < TILES; ++tile) {
                int current = tile % 2;
                bulk_wait_cta(barriers[current]);
                if (tile + 1 < TILES) {
                    int next = 1 - current;
                    bulk_begin_cta(barriers[next], tiles[next],
                                   source + (tile + 1) * THREADS, sizeof(tiles[0]));
                }
                sum += tiles[current][t];  // Overlap this work with the next copy.
                __syncthreads();  // No reader survives destination-buffer reuse.
            }
            payload[producer * THREADS + t] = sum;
            publish_cta(&channels[producer].ready, epoch);
        } else {
            for (int producer = 0; producer < PRODUCERS; ++producer) {
                await_cta(&channels[producer].ready, epoch);
            }
            output[(epoch - 1) * THREADS + t] = payload[t] + payload[THREADS + t];
            for (int producer = 0; producer < PRODUCERS; ++producer) {
                publish_cta(&channels[producer].consumed, epoch);
            }
        }
    }
    if (blockIdx.x < PRODUCERS) {
        bulk_finish_cta(barriers[0]);
        bulk_finish_cta(barriers[1]);
    }
}

// Stress finite scores far beyond the range where naive exp(score) is safe.
// Split boundaries include empty partitions and all splits of the same input.
__global__ void softmax_splits(float* output) {
    int split = threadIdx.x;
    if (split > 13) return;
    SoftmaxPartial<1> partial[2] = {{-INFINITY, 0.0f, {0.0f}},
                                   {-INFINITY, 0.0f, {0.0f}}};
    for (int i = 0; i < 13; ++i) {
        float score = 1000.0f + 0.125f * i;
        SoftmaxPartial<1> single = {score, 1.0f, {static_cast<float>(i - 6)}};
        int part = i < split ? 0 : 1;
        partial[part] = merge_softmax(partial[part], single);
    }
    auto merged = merge_softmax(partial[0], partial[1]);
    output[split] = merged.numerator[0] / merged.denominator;
}

void test_pipeline(int epochs, int seed) {
    std::vector<float> input(epochs * PRODUCERS * TILES * THREADS);
    for (size_t i = 0; i < input.size(); ++i) {
        input[i] = static_cast<float>(static_cast<int>((i * 17 + seed) % 101) - 50);
    }
    std::vector<float> actual(epochs * THREADS);
    DeviceBuffer<float> device_input(input.size()), device_output(actual.size());
    DeviceBuffer<float> payload(PRODUCERS * THREADS);
    DeviceBuffer<Channel> channels(PRODUCERS);
    check(cudaMemcpy(device_input.data, input.data(), input.size() * sizeof(float),
                     cudaMemcpyHostToDevice));
    // Deliberately dirty workspace, then initialize flags inside the kernel.
    check(cudaMemset(channels.data, 0xa5, PRODUCERS * sizeof(Channel)));
    check(cudaMemset(payload.data, 0xa5, PRODUCERS * THREADS * sizeof(float)));
    check(check_residency(pipeline, PRODUCERS + 1, THREADS, 0));
    void* arguments[] = {&device_input.data, &device_output.data, &channels.data,
                         &payload.data, &epochs};
    check(cudaLaunchCooperativeKernel(reinterpret_cast<void*>(pipeline),
                                     PRODUCERS + 1, THREADS, arguments));
    check(cudaDeviceSynchronize());
    check(cudaMemcpy(actual.data(), device_output.data, actual.size() * sizeof(float),
                     cudaMemcpyDeviceToHost));
    for (int epoch = 0; epoch < epochs; ++epoch) {
        for (int t = 0; t < THREADS; ++t) {
            float expected = 0.0f;
            for (int p = 0; p < PRODUCERS; ++p) {
                for (int tile = 0; tile < TILES; ++tile) {
                    expected += input[((epoch * PRODUCERS + p) * TILES + tile) *
                                      THREADS + t];
                }
            }
            if (actual[epoch * THREADS + t] != expected) {
                throw std::runtime_error("reused-channel payload mismatch");
            }
        }
    }
}

void test_softmax() {
    DeviceBuffer<float> device_output(14);
    softmax_splits<<<1, 32>>>(device_output.data);
    check(cudaGetLastError());
    check(cudaDeviceSynchronize());
    float actual[14];
    check(cudaMemcpy(actual, device_output.data, sizeof(actual), cudaMemcpyDeviceToHost));
    double numerator = 0.0, denominator = 0.0;
    for (int i = 0; i < 13; ++i) {
        double weight = std::exp(0.125 * (i - 12));
        numerator += weight * (i - 6);
        denominator += weight;
    }
    for (float value : actual) {
        if (!std::isfinite(value) || std::abs(value - numerator / denominator) > 2e-6) {
            throw std::runtime_error("split-softmax mismatch");
        }
    }
}

int main() {
    try {
        int device = 0, major = 0;
        check(cudaGetDevice(&device));
        check(cudaDeviceGetAttribute(&major, cudaDevAttrComputeCapabilityMajor, device));
        if (major < 9) throw std::runtime_error("bulk-copy example requires SM90+");
        for (int epochs : {1, 2, 17, 257}) {
            for (int seed : {0, 1, 997}) test_pipeline(epochs, seed);
        }
        test_softmax();
        std::puts("PASS: 12 reused-channel cases, 14 softmax partitions");
    } catch (const std::exception& error) {
        std::fprintf(stderr, "FAIL: %s\n", error.what());
        return 1;
    }
}
