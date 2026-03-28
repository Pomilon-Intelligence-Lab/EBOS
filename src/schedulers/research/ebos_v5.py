from typing import List, Optional, Tuple, Dict
from ..models import Task, CPU, Mutex, TaskState, TaskCategory
from .ebos_v42 import Scheduler, RunQueue
import abc
import math

class FluidRunQueue(RunQueue):
    def __init__(self):
        super().__init__()
        # 1. The "Warm" Array (Fluid Re-insertion)
        self.warm_array: List[List[Task]] = [[] for _ in range(64)]
        self.warm_bitmap: int = 0
        self.warm_count: int = 0
        
    def add_to_warm(self, task: Task):
        bucket = task.priority_bucket
        if task not in self.warm_array[bucket]:
            self.warm_array[bucket].append(task)
            self.warm_bitmap |= (1 << bucket)
            self.warm_count += 1
            
    def pop_highest_fluid(self) -> Optional[Task]:
        # Priority: Active > Warm
        if self.active_bitmap > 0:
            return self.pop_highest_active()
            
        if self.warm_bitmap > 0:
            bucket_idx = (self.warm_bitmap & -self.warm_bitmap).bit_length() - 1
            task = self.warm_array[bucket_idx].pop(0)
            self.warm_count -= 1
            if not self.warm_array[bucket_idx]:
                self.warm_bitmap &= ~(1 << bucket_idx)
            return task
        return None

    def swap_arrays(self):
        # Move Warm to Active
        for b in range(64):
            while self.warm_array[b]:
                task = self.warm_array[b].pop(0)
                self.add_to_active(task)
        self.warm_bitmap = 0
        self.warm_count = 0
        
        self.active_array, self.expired_array = self.expired_array, self.active_array
        self.active_bitmap, self.expired_bitmap = self.expired_bitmap, self.active_bitmap
        self.active_count = sum(len(b) for b in self.active_array)
        self.oldest_expired_time = -1.0

class EBOSv5Scheduler(Scheduler):
    def __init__(self, num_cores: int):
        self.num_cores = num_cores
        self.runqueues = {i: FluidRunQueue() for i in range(num_cores)}
        self.starvation_threshold = 25.0 # More relaxed for throughput
        
        self.category_boosts = {
            TaskCategory.INTERACTIVE: 500,
            TaskCategory.KERNEL: 400,
            TaskCategory.MULTIMEDIA: 300,
            TaskCategory.NETWORK: 200,
            TaskCategory.STORAGE: 100,
            TaskCategory.NONE: 0,
        }

    def _update_bucket(self, task: Task, current_time: float = 0):
        if task.category == TaskCategory.REALTIME:
            task.priority_bucket = 0
            return
        if task.category == TaskCategory.IDLE:
            task.priority_bucket = 63
            return
            
        score = task.interactive_score
        
        # Jitter Compensation
        if task.target_wake_time > 0 and current_time > task.target_wake_time:
            jitter = current_time - task.target_wake_time
            if jitter > 1.0: 
                score += int(min(300, jitter * 50)) 
        
        clamped_score = max(0, min(1000, score))
        task.priority_bucket = 63 - (clamped_score * 63 // 1000)

    def _quantum_for_bucket(self, bucket: int, load: int) -> float:
        base_q = 10.0 + (bucket / 63.0) * 90.0
        # V-Quantum: Balance responsiveness and throughput
        scale = 1.0 / math.log2(load/4 + 2) if load > 4 else 1.0
        return max(5.0, base_q * scale) 

    def pick_next_task(self, cpu: CPU, cpus: List[CPU], current_time: float = 0) -> Optional[Task]:
        rq = self.runqueues[cpu.core_id]
        
        if rq.active_bitmap == 0:
            # Check for starvation swap
            force_swap = False
            if rq.oldest_expired_time > 0 and current_time - rq.oldest_expired_time >= self.starvation_threshold:
                force_swap = True
            
            if rq.active_bitmap == 0 or force_swap:
                rq.swap_arrays()
            
        task = rq.pop_highest_fluid()
        if task:
            task.actual_wake_time = current_time
            return task
            
        # Proactive Work Stealing (restricted to same NUMA node)
        for sibling in cpus:
            if sibling.core_id == cpu.core_id or sibling.node_id != cpu.node_id: continue
            sibling_rq = self.runqueues[sibling.core_id]
            if sibling_rq.active_count > 4: 
                task = sibling_rq.pop_highest_fluid()
                if task: 
                    task.actual_wake_time = current_time
                    return task
        return None

    def on_task_ready(self, task: Task, category: Optional[TaskCategory] = None, current_time: float = 0):
        task.state = TaskState.READY
        
        if task.target_wake_time <= 0:
            task.target_wake_time = current_time
            
        if task.sleep_start_time > 0:
            sleep_duration = current_time - task.sleep_start_time
            energy_gain = sleep_duration * task.charge_rate
            task.energy_credits = min(task.max_energy, task.energy_credits + energy_gain)
            task.sleep_start_time = 0
            if category and category in self.category_boosts:
                boost = self.category_boosts[category]
                scale = min(1.0, sleep_duration / 50.0)
                task.interactive_score = min(1000, task.interactive_score + int(boost * scale))
        
        self._update_bucket(task, current_time)
        
        # Proactive Load-Aware Pushing (NUMA-local focus)
        best_core = 0
        min_load = 999
        # In real OS, we'd check cores in same NUMA node first
        for i in range(self.num_cores):
            load = self.runqueues[i].active_count + self.runqueues[i].warm_count
            if load < min_load:
                min_load = load
                best_core = i
        
        if task.quantum_remaining <= 0:
            task.base_quantum = self._quantum_for_bucket(task.priority_bucket, self.runqueues[best_core].active_count)
            burst = min(10.0, task.energy_credits) # Balanced burst
            task.energy_credits -= burst
            task.quantum_remaining = task.base_quantum + burst
            
        self.runqueues[best_core].add_to_active(task)

    def on_task_evicted(self, task: Task, current_time: float = 0):
        # Reset jitter tracker
        task.target_wake_time = current_time 
        
        # Preemptible Energy Burst handling (Hysteresis)
        if task.energy_credits > 10.0 and task.category in [TaskCategory.INTERACTIVE, TaskCategory.MULTIMEDIA]:
            burst = min(10.0, task.energy_credits)
            task.energy_credits -= burst
            task.quantum_remaining = self._quantum_for_bucket(task.priority_bucket, 1) + burst
            task.state = TaskState.READY
            self.runqueues[hash(task.id)%self.num_cores].add_to_active(task)
            return

        # Fluid Re-insertion (Hysteresis-Based)
        # Only re-insert if score is high AND it hasn't been thrashed
        if task.interactive_score > 500: 
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
        task.state = TaskState.WAITING
        task.target_wake_time = 0

    def on_task_terminate(self, task: Task):
        task.state = TaskState.TERMINATED

    def _promote_task(self, task: Task, new_bucket: int, new_score: int):
        if not task.shattered_tax_pending:
            task.base_score = task.interactive_score
            task.base_bucket = task.priority_bucket
        
        target_core = hash(task.id) % self.num_cores
        rq = self.runqueues[target_core]
        
        found = False
        for arr_name in ['active_array', 'warm_array', 'expired_array']:
            arr = getattr(rq, arr_name)
            bitmap_name = arr_name.replace('_array', '_bitmap')
            b = task.priority_bucket
            if task in arr[b]:
                arr[b].remove(task)
                if arr_name == 'active_array': rq.active_count -= 1
                if arr_name == 'warm_array': rq.warm_count -= 1
                if not arr[b]:
                    setattr(rq, bitmap_name, getattr(rq, bitmap_name) & ~(1 << b))
                found = True
                break
        
        task.priority_bucket = new_bucket
        task.interactive_score = new_score
        if found:
            rq.add_to_active(task)

    def on_mutex_acquire_failed(self, task: Task, mutex: Mutex):
        owner = mutex.owner
        if not owner: return
        if task.priority_bucket < owner.priority_bucket:
            self._promote_task(owner, task.priority_bucket, task.interactive_score)
        if owner.waiting_on and owner.waiting_on.owner:
            owner2 = owner.waiting_on.owner
            if task.priority_bucket < owner2.priority_bucket:
                self._promote_task(owner2, task.priority_bucket, task.interactive_score)
            if owner2.waiting_on and owner2.waiting_on.owner:
                owner3 = owner2.waiting_on.owner
                if owner3.category != TaskCategory.REALTIME:
                    self._promote_task(owner3, 0, 1000)
                    owner3.shattered_tax_pending = True

    def on_mutex_release(self, task: Task, mutex: Mutex):
        if not task.held_mutexes and task.shattered_tax_pending:
            task.interactive_score = task.base_score
            task.priority_bucket = task.base_bucket
            task.quantum_remaining += 2.0 
            task.shattered_tax_pending = False
            self._update_bucket(task)
