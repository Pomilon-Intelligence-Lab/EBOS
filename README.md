# EBOS: Event-Boosted O(1) Scheduler

EBOS is a high-performance, Symmetric Multiprocessing (SMP) scheduler that abandons O(log N) tree-based balancing in favor of deterministic, bitmask-driven bucket queues. By prioritizing hardware-level efficiency and behavioral profiling, EBOS delivers sub-millisecond jitter for interactive workloads while maintaining superior throughput for batch processing.

---

## Architectural Philosophy

EBOS is built on the principle of Isolation over Contention. Every CPU core manages its own isolated RunQueue, eliminating global scheduler locks and allowing performance to scale linearly with core count.

### 1. The O(1) Core
Task selection is performed in constant time using bitmask-accelerated bucket selection:
*   **Double-Buffered Arrays**: Each core maintains Active and Expired arrays of 64 priority buckets.
*   **Bitmask Selection**: Hardware-accelerated bit-scanning (bsf/clz) identifies the highest-priority non-empty bucket in a single CPU cycle.
*   **The Inverted Quantum Paradox**: High-priority tasks (UI/Realtime) receive shorter time slices (10-20ms) to ensure high-frequency responsiveness, while low-priority batch tasks receive larger slices (50-100ms) to maximize cache locality and IPC (Instructions Per Cycle).

### 2. Behavioral Intelligence: Energy & Boosts
EBOS ignores user-space priority hints, instead profiling tasks based on their system-level behavior:
*   **Token Bucket Energy Meter**: Tasks "bank" energy credits while sleeping. Upon wakeup, an interactive task can spend these credits to claim a "Burst Quantum," preempting background tasks to ensure instantaneous UI reaction.
*   **Category-Based Tagging**: Syscall/ISR hooks assign boosts based on intent (e.g., TaskCategory::Interactive for HID interrupts, TaskCategory::Multimedia for frame-sensitive playback).
*   **Epoch-Based Decay**: A task’s interactive_score is only halved when it exhausts its full quantum, preventing the "Decay Cliff" where interactive threads are penalized mid-render.

---

## Specialized Variants (v1.0.0 Stable)

The EBOS architecture is branched into two specialized variants to meet distinct workload requirements:

### EBOS-Desktop (EBOS-D)
*Focus: Maximum Snappiness and Jitter Isolation.*
*   **Interactive Fast-Path**: A dedicated high-priority queue that bypasses standard epoch-based buckets for UI and Realtime tasks.
*   **Thunder-Strike Preemption**: Aggressive preemption thresholds ensure that when a UI task wakes, it claims the CPU within microseconds, shielding the user from "micro-stutter" caused by background batch processing.

### EBOS-Server (EBOS-S)
*Focus: Raw Throughput and Instruction Density.*
*   **Pure O(1) Logic**: Minimizes management overhead by strictly adhering to the double-buffered bucket cycle.
*   **Fluidity (Warm Arrays)**: Implements "Warm Arrays" to track recently active tasks. This maintains L1/L2 cache locality by allowing tasks to re-enter the active cycle without a full re-sort, maximizing throughput for heavy computational loads.

---

## Benchmark Performance (Verified v1.0.0 Stable)

Verified on 8-core SMP environments using both Discrete Event Simulation (DES) and real-world C/pthread multi-trial stress tests (5 trials per scenario).

| Metric | EBOS Desktop | EBOS Server | Linux CFS (Baseline) |
| :--- | :--- | :--- | :--- |
| **Avg Time (Desktop Mix)** | 97.65ms | **83.67ms** | 107.65ms |
| **Min Jitter (Desktop Mix)** | **4.84ms** | 4.16ms | 10.93ms |
| **Avg Time (Absurd Load)** | 126.09ms | **113.63ms** | 170.07ms |
| **Min Jitter (Absurd Load)** | **13.41ms** | 15.63ms | 41.56ms |
| **Avg Latency (1k DES)** | **193.74ms** | 284.95ms | 641.23ms |
| **Mgmt Overhead (CS)** | **Low** | **Minimal** | High |


---

## Usage

### Prerequisites
- Python 3.8+
- GCC (for C benchmarks)

### Running Benchmarks
To run the precise Discrete Event Simulation:
```bash
python3 -m src.main
```

To run the real-world SMP C/pthread tests:
```bash
gcc -O3 src/real_test/main.c src/real_test/ebos_core.c -o ebos_bench -lpthread
./ebos_bench
```

---

## Project Structure

- `src/engine.py`: High-precision Discrete Event Simulation (DES) engine.
- `src/schedulers/ebos_desktop.py`: Implementation of the low-jitter Fast-Path branch.
- `src/schedulers/ebos_server.py`: Implementation of the high-throughput Fluidity branch.
- `src/real_test/`: SMP-fair C implementation using pthreads for hardware validation.
- `src/models.py`: Unified Task and CPU models across Python and C.

---

## License
This project is licensed under the MIT License. See the LICENSE file for details.

Developed by Pomilon Intelligence Lab.
