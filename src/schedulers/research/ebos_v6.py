from typing import List, Optional, Tuple, Dict
from ..models import Task, CPU, Mutex, TaskState, TaskCategory
from .ebos_v5 import EBOSv5Scheduler, FluidRunQueue
import abc
import math

class ExtremeRunQueue(FluidRunQueue):
    def __init__(self):
        super().__init__()
        # EBOS v6.0: Panic Queue for Multimedia/Realtime tasks nearing deadlines
        self.panic_queue: List[Task] = []
        
    def add_to_panic(self, task: Task):
        if task not in self.panic_queue:
            self.panic_queue.append(task)
            
    def pop_highest_extreme(self) -> Optional[Task]:
        # 1. Panic Tasks (Absolute Priority)
        if self.panic_queue:
            # Sort panic queue by deadline (very small list, effectively O(1))
            self.panic_queue.sort(key=lambda t: t.deadline)
            return self.panic_queue.pop(0)
            
        # 2. Intra-Bucket Micro-vRuntime Selection
        # If Active array has multiple tasks in the highest bucket, pick by vruntime_residual
        if self.active_bitmap > 0:
            bucket_idx = (self.active_bitmap & -self.active_bitmap).bit_length() - 1
            bucket = self.active_array[bucket_idx]
            if len(bucket) > 1:
                # Sort small bucket by vruntime_residual (Intra-Bucket Fairness)
                bucket.sort(key=lambda t: t.vruntime_residual)
            
            task = bucket.pop(0)
            self.active_count -= 1
            if not self.active_array[bucket_idx]:
                self.active_bitmap &= ~(1 << bucket_idx)
            return task
            
        # 3. Warm Array
        return self.pop_highest_fluid()

class EBOSv6Scheduler(EBOSv5Scheduler):
    def __init__(self, num_cores: int):
        self.num_cores = num_cores
        self.runqueues = {i: ExtremeRunQueue() for i in range(num_cores)}
        self.starvation_threshold = 10.0 # Extreme responsiveness (10ms)
        
        self.category_boosts = {
            TaskCategory.INTERACTIVE: 500,
            TaskCategory.KERNEL: 400,
            TaskCategory.MULTIMEDIA: 300,
            TaskCategory.NETWORK: 200,
            TaskCategory.STORAGE: 100,
            TaskCategory.NONE: 0,
        }
        
        # Deadlines based on frequency (ms)
        self.deadlines = {
            TaskCategory.REALTIME: 1.0,     # Audio/DRM (1ms)
            TaskCategory.INTERACTIVE: 16.6, # 60Hz (16.6ms)
            TaskCategory.MULTIMEDIA: 6.9,   # 144Hz (6.9ms)
        }

    def _update_vruntime_residual(self, task: Task, execution_time: float):
        # Scale execution by priority (similar to CFS weight)
        # Higher score (Bucket 0) = slower vruntime growth
        weight = max(1, 1000 - task.priority_bucket * 15)
        task.vruntime_residual += execution_time * (1000 / weight)

    def pick_next_task(self, cpu: CPU, cpus: List[CPU], current_time: float = 0) -> Optional[Task]:
        rq = self.runqueues[cpu.core_id]
        
        # Check Starvation swap
        if rq.active_bitmap == 0:
            force_swap = False
            if rq.oldest_expired_time > 0 and current_time - rq.oldest_expired_time >= self.starvation_threshold:
                force_swap = True
            if rq.active_bitmap == 0 or force_swap:
                rq.swap_arrays()
        
        # Check for Panic injections (Tasks nearing deadlines)
        # We also check other cores' panic tasks to see if we should "help"
        task = rq.pop_highest_extreme()
        if task:
            task.actual_wake_time = current_time
            return task
            
        # Proactive Work Stealing (Extreme focus)
        for sibling in cpus:
            if sibling.core_id == cpu.core_id: continue
            sibling_rq = self.runqueues[sibling.core_id]
            if sibling_rq.active_count > 1: # Steal if sibling has even 2 tasks!
                task = sibling_rq.pop_highest_extreme()
                if task: 
                    task.actual_wake_time = current_time
                    return task
        return None

    def on_task_ready(self, task: Task, category: Optional[TaskCategory] = None, current_time: float = 0):
        task.state = TaskState.READY
        
        # Initialize Deadline
        if task.category in self.deadlines:
            # Set hard deadline from NOW
            task.deadline = current_time + self.deadlines[task.category]
            
        if task.sleep_start_time > 0:
            sleep_duration = current_time - task.sleep_start_time
            energy_gain = sleep_duration * task.charge_rate
            task.energy_credits = min(task.max_energy, task.energy_credits + energy_gain)
            task.sleep_start_time = 0
            # Quantum Residual Banking
            if task.quantum_bank > 0:
                task.quantum_remaining += task.quantum_bank
                task.quantum_bank = 0

            if category and category in self.category_boosts:
                boost = self.category_boosts[category]
                scale = min(1.0, sleep_duration / 50.0)
                task.interactive_score = min(1000, task.interactive_score + int(boost * scale))
        
        self._update_bucket(task, current_time)
        
        # Proactive Pushing (Extreme Load-Aware)
        best_core = 0
        min_load = 9999
        for i in range(self.num_cores):
            rq = self.runqueues[i]
            # Load includes panic queue weight
            load = rq.active_count + rq.warm_count + (len(rq.panic_queue) * 10)
            if load < min_load:
                min_load = load
                best_core = i
        
        if task.quantum_remaining <= 0:
            task.base_quantum = self._quantum_for_bucket(task.priority_bucket, self.runqueues[best_core].active_count)
            burst = min(10.0, task.energy_credits)
            task.energy_credits -= burst
            task.quantum_remaining = task.base_quantum + burst
            
        # Place in Panic queue if deadline is very close (e.g., < 2ms)
        if task.deadline > 0 and (task.deadline - current_time) < 2.0:
            self.runqueues[best_core].add_to_panic(task)
        else:
            self.runqueues[best_core].add_to_active(task)

    def on_task_evicted(self, task: Task, current_time: float = 0):
        # 3. Quantum Residual Banking
        # If task was preempted (not quantum exhausted), bank the remainder
        # In this simulation, on_task_ready is called for preempted tasks.
        # So we handle bank in on_task_ready.
        
        self._update_vruntime_residual(task, task.base_quantum)
        
        # Preemptible Energy Burst
        if task.energy_credits > 5.0 and task.category in [TaskCategory.INTERACTIVE, TaskCategory.MULTIMEDIA]:
            burst = min(10.0, task.energy_credits)
            task.energy_credits -= burst
            task.quantum_remaining = self._quantum_for_bucket(task.priority_bucket, 1) + burst
            task.state = TaskState.READY
            self.runqueues[hash(task.id)%self.num_cores].add_to_active(task)
            return

        # Fluid Re-insertion
        if task.interactive_score > 300: 
            task.state = TaskState.READY
            task.quantum_remaining = self._quantum_for_bucket(task.priority_bucket, 1)
            self.runqueues[hash(task.id)%self.num_cores].add_to_warm(task)
            return

        # Normal Decay
        if task.category != TaskCategory.REALTIME:
            task.interactive_score >>= 1
        
        self._update_bucket(task, current_time)
        task.quantum_remaining = self._quantum_for_bucket(task.priority_bucket, 1)
        task.state = TaskState.READY
        self.runqueues[hash(task.id)%self.num_cores].add_to_expired(task, current_time)

    def on_task_wait(self, task: Task):
        # Bank remaining quantum when blocking voluntarily (I/O)
        if task.quantum_remaining > 0:
            task.quantum_bank = task.quantum_remaining
            task.quantum_remaining = 0
        task.state = TaskState.WAITING
        task.target_wake_time = 0
        task.deadline = 0

    def on_task_terminate(self, task: Task):
        task.state = TaskState.TERMINATED
