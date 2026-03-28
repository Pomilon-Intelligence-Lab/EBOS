from typing import List, Optional, Dict, Tuple
from ..models import Task, CPU, Mutex, TaskState, TaskCategory
import abc
import math

class EBOSRunQueue:
    def __init__(self):
        self.active_array: List[List[Task]] = [[] for _ in range(64)]
        self.expired_array: List[List[Task]] = [[] for _ in range(64)]
        self.warm_array: List[Task] = []
        self.panic_queue: List[Task] = []
        self.fast_path: List[Task] = []
        
        self.active_bitmap: int = 0
        self.expired_bitmap: int = 0
        self.active_count: int = 0
        self.warm_count: int = 0
        
        self.oldest_expired_time: float = -1.0
        self.last_thunder_strike: float = -10.0

    def add_to_active(self, task: Task):
        bucket = task.priority_bucket
        if task not in self.active_array[bucket]:
            self.active_array[bucket].append(task)
            self.active_bitmap |= (1 << bucket)
            self.active_count += 1
            
    def add_to_expired(self, task: Task, current_time: float):
        bucket = task.priority_bucket
        if task not in self.expired_array[bucket]:
            self.expired_array[bucket].append(task)
            self.expired_bitmap |= (1 << bucket)
            if self.oldest_expired_time < 0:
                self.oldest_expired_time = current_time

    def add_to_warm(self, task: Task):
        if task not in self.warm_array:
            self.warm_array.append(task)
            self.warm_count += 1

    def add_to_panic(self, task: Task):
        if task not in self.panic_queue:
            self.panic_queue.append(task)

    def add_to_fast_path(self, task: Task):
        if task not in self.fast_path:
            self.fast_path.append(task)

    def pop_highest_priority(self) -> Optional[Task]:
        # 1. Fast-Path (Absolute Priority, Jitter Isolation)
        if self.fast_path:
            return self.fast_path.pop(0)

        # 2. Panic Queue (Deadline Driven)
        if self.panic_queue:
            self.panic_queue.sort(key=lambda t: t.deadline)
            return self.panic_queue.pop(0)

        # 3. Active Array (O(1) Bitmap Selection)
        if self.active_bitmap > 0:
            bucket_idx = (self.active_bitmap & -self.active_bitmap).bit_length() - 1
            bucket = self.active_array[bucket_idx]
            if len(bucket) > 1:
                # Intra-Bucket Fairness (Micro-vRuntime)
                bucket.sort(key=lambda t: t.vruntime_residual)
            task = bucket.pop(0)
            self.active_count -= 1
            if not bucket:
                self.active_bitmap &= ~(1 << bucket_idx)
            return task

        # 4. Warm Array (Fluidity)
        if self.warm_array:
            task = self.warm_array.pop(0)
            self.warm_count -= 1
            return task
            
        return None

    def swap_arrays(self):
        self.active_array, self.expired_array = self.expired_array, self.active_array
        self.active_bitmap, self.expired_bitmap = self.expired_bitmap, self.active_bitmap
        self.active_count = sum(len(b) for b in self.active_array)
        self.oldest_expired_time = -1.0

class EBOSScheduler:
    def __init__(self, num_cores: int):
        self.num_cores = num_cores
        self.runqueues = {i: EBOSRunQueue() for i in range(num_cores)}
        self.starvation_threshold = 15.0 
        self.global_last_input_time = 0.0
        
        self.category_boosts = {
            TaskCategory.REALTIME: 800,
            TaskCategory.INTERACTIVE: 600,
            TaskCategory.KERNEL: 400,
            TaskCategory.MULTIMEDIA: 300,
            TaskCategory.NETWORK: 200,
            TaskCategory.STORAGE: 100,
        }
        
        self.deadlines = {
            TaskCategory.REALTIME: 0.5,
            TaskCategory.INTERACTIVE: 8.0,
            TaskCategory.MULTIMEDIA: 6.9,   
        }

    def _update_bucket(self, task: Task):
        clamped_score = max(0, min(1000, task.interactive_score))
        task.priority_bucket = 63 - (clamped_score * 63 // 1000)

    def _quantum_for_bucket(self, bucket: int, load: int) -> float:
        # Load-Aware Quantum Scaling
        base = 10.0 + (bucket / 63.0) * 90.0
        load_factor = 1.0 / (1.0 + math.log1p(load))
        return base * load_factor

    def pick_next_task(self, cpu: CPU, cpus: List[CPU], current_time: float = 0) -> Optional[Task]:
        rq = self.runqueues[cpu.core_id]
        
        # 1. Epoch Swap (Starvation Prevention)
        if rq.active_count == 0 and rq.warm_count == 0 and not rq.fast_path and not rq.panic_queue:
            if rq.oldest_expired_time > 0 and (current_time - rq.oldest_expired_time) >= self.starvation_threshold:
                rq.swap_arrays()
        
        # 2. Local Selection
        task = rq.pop_highest_priority()
        if task:
            task.actual_wake_time = current_time
            return task
            
        # 3. Proactive Work Stealing (Sticky-Cache Affinity)
        for sibling in cpus:
            if sibling.core_id == cpu.core_id: continue
            sibling_rq = self.runqueues[sibling.core_id]
            if sibling_rq.active_count > 4: # Steal threshold
                task = sibling_rq.pop_highest_priority()
                if task:
                    task.actual_wake_time = current_time
                    return task
        return None

    def on_task_ready(self, task: Task, category: Optional[TaskCategory] = None, current_time: float = 0):
        if task.state == TaskState.RUNNING:
            self._update_bucket(task)
            return # Don't re-queue if already running
            
        if task.state == TaskState.READY and task.quantum_remaining > 0:
            return # Already in a queue and has quantum
            
        task.state = TaskState.READY
        
        # Energy Recovery
        if task.sleep_start_time > 0:
            sleep_duration = current_time - task.sleep_start_time
            task.energy_credits = min(task.max_energy, task.energy_credits + sleep_duration * task.charge_rate)
            
            if category and category in self.category_boosts:
                boost = self.category_boosts[category]
                scale = min(1.0, sleep_duration / 50.0)
                task.interactive_score = min(1000, task.interactive_score + int(boost * scale))
            
            if task.quantum_bank > 0:
                task.quantum_remaining += task.quantum_bank
                task.quantum_bank = 0
            task.sleep_start_time = 0

        if task.category == TaskCategory.INTERACTIVE:
            self.global_last_input_time = current_time

        self._update_bucket(task)
        
        # Core Assignment
        if task.home_core == -1:
            best_core = hash(task.id) % self.num_cores
            task.home_core = best_core

        # Quantum Assignment (Only if empty)
        if task.quantum_remaining <= 0:
            base_q = self._quantum_for_bucket(task.priority_bucket, self.runqueues[task.home_core].active_count)
            burst = 0.0
            if task.category in [TaskCategory.REALTIME, TaskCategory.INTERACTIVE]:
                burst = min(15.0, task.energy_credits)
                task.energy_credits -= burst
            task.quantum_remaining = base_q + burst

        # Insertion
        rq = self.runqueues[task.home_core]
        if task.category == TaskCategory.REALTIME:
            rq.add_to_fast_path(task)
        elif task.category == TaskCategory.INTERACTIVE and task.priority_bucket < 8:
            rq.add_to_fast_path(task)
        else:
            rq.add_to_active(task)

    def on_task_evicted(self, task: Task, current_time: float = 0):
        # Micro-vRuntime Residual Tracking
        task.vruntime_residual += 2.0 # Penalty for eviction
        
        # Fluid Warm Array Check
        if task.interactive_score > 500:
            task.state = TaskState.READY
            self.runqueues[task.home_core].add_to_warm(task)
            return

        # Normal Decay & Expiration
        if task.category != TaskCategory.REALTIME:
            task.interactive_score >>= 1
        
        self._update_bucket(task)
        task.state = TaskState.READY
        self.runqueues[task.home_core].add_to_expired(task, current_time)

    def on_task_wait(self, task: Task):
        if task.quantum_remaining > 0:
            task.quantum_bank = task.quantum_remaining
            task.quantum_remaining = 0
        task.state = TaskState.WAITING
        task.deadline = 0

    def on_task_terminate(self, task: Task):
        task.state = TaskState.TERMINATED

    def on_mutex_acquire_failed(self, task: Task, mutex: Mutex):
        # Priority Inheritance
        owner = mutex.owner
        if owner and task.priority_bucket < owner.priority_bucket:
            owner.priority_bucket = task.priority_bucket
            owner.shattered_tax_pending = True

    def on_mutex_release(self, task: Task, mutex: Mutex):
        # Soft Landing after PI
        if not task.held_mutexes and task.shattered_tax_pending:
            self._update_bucket(task)
            task.shattered_tax_pending = False
