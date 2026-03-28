from typing import List, Optional, Tuple
from ..models import Task, CPU, Mutex, TaskState, TaskCategory
import abc
import math

class Scheduler(abc.ABC):
    @abc.abstractmethod
    def pick_next_task(self, cpu: CPU, cpus: List[CPU]) -> Optional[Task]:
        pass
    
    @abc.abstractmethod
    def on_task_ready(self, task: Task, category: Optional[TaskCategory] = None):
        pass
    
    @abc.abstractmethod
    def on_task_wait(self, task: Task):
        pass
    
    @abc.abstractmethod
    def on_task_terminate(self, task: Task):
        pass
        
    @abc.abstractmethod
    def on_mutex_acquire_failed(self, task: Task, mutex: Mutex):
        pass
        
    @abc.abstractmethod
    def on_mutex_release(self, task: Task, mutex: Mutex):
        pass

class RunQueue:
    def __init__(self):
        self.active_array: List[List[Task]] = [[] for _ in range(64)]
        self.expired_array: List[List[Task]] = [[] for _ in range(64)]
        self.active_bitmap: int = 0
        self.expired_bitmap: int = 0
        self.active_count: int = 0
        
        # 1. Starvation Threshold Tracking
        self.oldest_expired_time: float = -1.0
        
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

    def pop_highest_active(self) -> Optional[Task]:
        if self.active_bitmap == 0:
            return None
        bucket_idx = (self.active_bitmap & -self.active_bitmap).bit_length() - 1
        task = self.active_array[bucket_idx].pop(0)
        self.active_count -= 1
        if not self.active_array[bucket_idx]:
            self.active_bitmap &= ~(1 << bucket_idx)
        return task

    def swap_arrays(self):
        self.active_array, self.expired_array = self.expired_array, self.active_array
        self.active_bitmap, self.expired_bitmap = self.expired_bitmap, self.active_bitmap
        self.active_count = sum(len(b) for b in self.active_array)
        self.oldest_expired_time = -1.0

class EBOSv42Scheduler(Scheduler):
    def __init__(self, num_cores: int):
        self.num_cores = num_cores
        self.runqueues = {i: RunQueue() for i in range(num_cores)}
        
        # Target P99 latency limit for starvation swaps
        self.starvation_threshold = 20.0 
        
        self.category_boosts = {
            TaskCategory.INTERACTIVE: 500,
            TaskCategory.KERNEL: 400,
            TaskCategory.MULTIMEDIA: 300,
            TaskCategory.NETWORK: 200,
            TaskCategory.STORAGE: 100,
            TaskCategory.NONE: 0,
        }

    def _update_bucket(self, task: Task):
        if task.category == TaskCategory.REALTIME:
            task.priority_bucket = 0
            return
        if task.category == TaskCategory.IDLE:
            task.priority_bucket = 63
            return
            
        clamped_score = max(0, min(1000, task.interactive_score))
        task.priority_bucket = 63 - (clamped_score * 63 // 1000)

    def _quantum_for_bucket(self, bucket: int) -> float:
        return 10.0 + (bucket / 63.0) * 90.0

    def pick_next_task(self, cpu: CPU, cpus: List[CPU], current_time: float = 0) -> Optional[Task]:
        rq = self.runqueues[cpu.core_id]
        
        # 1. Time-Triggered Epoch Swap (The "Starvation Threshold")
        force_swap = False
        if rq.oldest_expired_time > 0:
            if current_time - rq.oldest_expired_time >= self.starvation_threshold:
                force_swap = True
                
        if rq.active_bitmap == 0 or force_swap:
            rq.swap_arrays()
            
        if rq.active_bitmap > 0:
            return rq.pop_highest_active()
            
        # 4. Depth-Aware Work Stealing
        # Cache misses take nanoseconds; waiting in queue takes milliseconds.
        for sibling in cpus:
            if sibling.core_id == cpu.core_id: continue
            sibling_rq = self.runqueues[sibling.core_id]
            
            # Steal from Active if sibling is swamped
            if sibling_rq.active_count > 8:
                # Pop from highest priority bucket
                task = sibling_rq.pop_highest_active()
                if task: return task
                
            # Fallback to stealing from sibling's Expired (Cache-Aware)
            if sibling.node_id == cpu.node_id and sibling_rq.expired_bitmap > 0:
                bucket_idx = (sibling_rq.expired_bitmap & -sibling_rq.expired_bitmap).bit_length() - 1
                task = sibling_rq.expired_array[bucket_idx].pop(0)
                if not sibling_rq.expired_array[bucket_idx]:
                    sibling_rq.expired_bitmap &= ~(1 << bucket_idx)
                return task
                
        return None

    def on_task_ready(self, task: Task, category: Optional[TaskCategory] = None, current_time: float = 0):
        task.state = TaskState.READY
        
        if task.sleep_start_time > 0:
            sleep_duration = current_time - task.sleep_start_time
            energy_gain = sleep_duration * task.charge_rate
            task.energy_credits = min(task.max_energy, task.energy_credits + energy_gain)
            task.sleep_start_time = 0
            
            if category and category in self.category_boosts:
                boost = self.category_boosts[category]
                scale = min(1.0, sleep_duration / 50.0)
                task.interactive_score = min(1000, task.interactive_score + int(boost * scale))
        
        self._update_bucket(task)
        
        # 3. Preemptible Energy Bursts (Bounding the UI)
        # Instead of 100ms straight, cap at 20ms slices
        burst_quantum = 0
        if task.category in [TaskCategory.INTERACTIVE, TaskCategory.REALTIME]:
            burst_quantum = min(20.0, task.energy_credits)
            task.energy_credits -= burst_quantum

        base_q = self._quantum_for_bucket(task.priority_bucket)
        if task.quantum_remaining <= 0:
            task.quantum_remaining = base_q + burst_quantum
        
        target_core = hash(task.id) % self.num_cores
        self.runqueues[target_core].add_to_active(task)

    def on_task_evicted(self, task: Task, current_time: float = 0):
        # 3. Preemptible Energy Bursts (Re-queue to Active if energy remains)
        if task.energy_credits > 10.0 and task.category == TaskCategory.INTERACTIVE:
            # Task still has energy, give it another 20ms burst in ACTIVE
            # This allows it to stay high-priority but be preempted by Realtime
            burst = min(20.0, task.energy_credits)
            task.energy_credits -= burst
            task.quantum_remaining = self._quantum_for_bucket(task.priority_bucket) + burst
            task.state = TaskState.READY
            target_core = hash(task.id) % self.num_cores
            self.runqueues[target_core].add_to_active(task)
            return

        # Normal Epoch-Based Decay
        if task.category != TaskCategory.REALTIME:
            task.interactive_score >>= 1
        
        self._update_bucket(task)
        task.quantum_remaining = self._quantum_for_bucket(task.priority_bucket)
        task.state = TaskState.READY
        target_core = hash(task.id) % self.num_cores
        self.runqueues[target_core].add_to_expired(task, current_time)

    def on_task_wait(self, task: Task):
        task.state = TaskState.WAITING

    def on_task_terminate(self, task: Task):
        task.state = TaskState.TERMINATED

    def _promote_task(self, task: Task, new_bucket: int, new_score: int):
        if task.priority_bucket == new_bucket:
            return
        # Store original values for Soft Landing
        if not task.shattered_tax_pending:
            task.base_score = task.interactive_score
            task.base_bucket = task.priority_bucket
            
        target_core = hash(task.id) % self.num_cores
        rq = self.runqueues[target_core]
        
        found = False
        for arr_name in ['active_array', 'expired_array']:
            arr = getattr(rq, arr_name)
            bitmap_name = arr_name.replace('_array', '_bitmap')
            b = task.priority_bucket
            if task in arr[b]:
                arr[b].remove(task)
                if arr_name == 'active_array': rq.active_count -= 1
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
        # 2. Priority Soft Landing
        if not task.held_mutexes and task.shattered_tax_pending:
            # Restore to original priority instead of 0
            task.interactive_score = task.base_score
            task.priority_bucket = task.base_bucket
            # Give tiny 2ms grace quantum
            task.quantum_remaining += 2.0 
            task.shattered_tax_pending = False
            # Ensure bucket is consistent
            self._update_bucket(task)
