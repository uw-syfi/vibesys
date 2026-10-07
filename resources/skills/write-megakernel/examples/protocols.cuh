#pragma once

#include <cuda/atomic>
#include <cuda_runtime.h>
#include <cmath>

// Teaching interface. All *_cta calls require every thread of a 1-D CTA.
// Flags are device memory; their payload lifetimes are the caller's responsibility.
namespace megakernel_examples {

__device__ inline void publish_cta(int* flag, int epoch) {
    __syncthreads();  // Include writes from every producer thread.
    if (threadIdx.x == 0) {
        cuda::atomic_ref<int, cuda::thread_scope_device>(*flag)
            .store(epoch, cuda::memory_order_release);
    }
}

__device__ inline void await_cta(int* flag, int epoch) {
    if (threadIdx.x == 0) {
        while (cuda::atomic_ref<int, cuda::thread_scope_device>(*flag)
                   .load(cuda::memory_order_acquire) != epoch) {
            // Busy wait is safe only when all awaited producers can run.
        }
    }
    __syncthreads();  // Every consumer thread follows the successful acquire.
}

// One producer CTA, one consumer CTA, one reused payload per channel.
// Producer waits consumed == epoch-1 before writing; consumer publishes consumed
// only after every reader finishes. Epochs start at 1 and must not wrap.
struct Channel {
    int ready;
    int consumed;
};

// SM90+ global -> local shared bulk copy. Only thread 0 manipulates the barrier.
// Source/destination are 16-byte aligned; bytes is a positive multiple of 16.
// One outstanding transfer per barrier. The source remains immutable until wait.
// Before reusing the destination, the caller must join all its previous readers.
struct alignas(8) BulkBarrier {
    unsigned long long state;
    unsigned phase;
};

__device__ inline void bulk_init_cta(BulkBarrier& barrier) {
    if (threadIdx.x == 0) {
        unsigned b = static_cast<unsigned>(__cvta_generic_to_shared(&barrier.state));
        barrier.phase = 0;
        asm volatile("mbarrier.init.shared::cta.b64 [%0], 1;"
                     :: "r"(b) : "memory");
        asm volatile("fence.proxy.async.shared::cta;" ::: "memory");
    }
    __syncthreads();
}

__device__ inline void bulk_begin_cta(BulkBarrier& barrier, void* destination,
                                      const void* source, unsigned bytes) {
    if (threadIdx.x == 0) {
        unsigned b = static_cast<unsigned>(__cvta_generic_to_shared(&barrier.state));
        unsigned d = static_cast<unsigned>(__cvta_generic_to_shared(destination));
        // Prior destination readers must already have joined before buffer reuse.
        asm volatile("fence.proxy.async.shared::cta;" ::: "memory");
        asm volatile("mbarrier.arrive.expect_tx.shared::cta.b64 _, [%0], %1;"
                     :: "r"(b), "r"(bytes) : "memory");
        asm volatile(
            "cp.async.bulk.shared::cluster.global.mbarrier::complete_tx::bytes "
            "[%0], [%1], %2, [%3];"
            :: "r"(d), "l"(source), "r"(bytes), "r"(b) : "memory");
    }
}

__device__ inline void bulk_wait_cta(BulkBarrier& barrier) {
    if (threadIdx.x == 0) {
        unsigned b = static_cast<unsigned>(__cvta_generic_to_shared(&barrier.state));
        asm volatile(
            "{ .reg .pred p; wait_bulk: "
            "mbarrier.try_wait.parity.shared::cta.b64 p, [%0], %1; "
            "@!p bra wait_bulk; }" :: "r"(b), "r"(barrier.phase) : "memory");
        barrier.phase ^= 1;
    }
    __syncthreads();  // Completion precedes every shared-memory reader.
}

// Call only after every begun transfer has completed and all readers have joined.
__device__ inline void bulk_finish_cta(BulkBarrier& barrier) {
    __syncthreads();
    if (threadIdx.x == 0) {
        unsigned b = static_cast<unsigned>(__cvta_generic_to_shared(&barrier.state));
        asm volatile("mbarrier.inval.shared::cta.b64 [%0];"
                     :: "r"(b) : "memory");
    }
}

// A partition stores unnormalized weighted values, not already-divided output.
// Inputs: finite, unmasked scores; empty partitions use denominator == 0.
template <int D>
struct SoftmaxPartial {
    float maximum;
    float denominator;
    float numerator[D];
};

template <int D>
__host__ __device__ inline SoftmaxPartial<D> merge_softmax(
    const SoftmaxPartial<D>& a, const SoftmaxPartial<D>& b) {
    if (a.denominator == 0.0f) return b;
    if (b.denominator == 0.0f) return a;
    SoftmaxPartial<D> result;
    result.maximum = fmaxf(a.maximum, b.maximum);
    float sa = expf(a.maximum - result.maximum);
    float sb = expf(b.maximum - result.maximum);
    result.denominator = sa * a.denominator + sb * b.denominator;
    for (int d = 0; d < D; ++d) {
        result.numerator[d] = sa * a.numerator[d] + sb * b.numerator[d];
    }
    return result;
}

// Call after setting any dynamic shared-memory opt-in attribute. The occupancy
// query includes kernel static shared memory and registers. Never launch a larger
// spinning grid on the assumption that queued producer CTAs will eventually run.
template <typename Kernel>
inline cudaError_t check_residency(Kernel kernel, int blocks, int threads,
                                   size_t dynamic_shared_bytes) {
    int device = 0, cooperative = 0, sms = 0, blocks_per_sm = 0;
    cudaError_t status = cudaGetDevice(&device);
    if (status != cudaSuccess) return status;
    status = cudaDeviceGetAttribute(&cooperative, cudaDevAttrCooperativeLaunch, device);
    if (status != cudaSuccess) return status;
    if (!cooperative) return cudaErrorNotSupported;
    status = cudaDeviceGetAttribute(&sms, cudaDevAttrMultiProcessorCount, device);
    if (status != cudaSuccess) return status;
    status = cudaOccupancyMaxActiveBlocksPerMultiprocessor(
        &blocks_per_sm, kernel, threads, dynamic_shared_bytes);
    if (status != cudaSuccess) return status;
    if (blocks <= 0 || blocks > sms * blocks_per_sm) {
        return cudaErrorCooperativeLaunchTooLarge;
    }
    return cudaSuccess;
}

}  // namespace megakernel_examples
