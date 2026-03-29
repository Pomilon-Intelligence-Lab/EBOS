from typing import List, Optional, Tuple, Dict
from ..models import Task, CPU, TaskState, TaskCategory
import math

class ServerRunQueue:
    def __init__(self):
        self.active_array: List[List[Task]] = [[] for _ in range(64)]
        self.expired_array: List[List[Task]] = [[] for _ in range(64)]
        self.warm_array: List[Task] = []
        self.panic_queue: List[Task] = []
        
        self.active_bitmap: int = 0
        self.expired_bitmap: int = 0
        self.active_count: int = 0
        self.warm_count: int = 0
        
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

    def add_to_warm(self, task: Task):
        if task not in self.warm_array:
            self.warm_array.append(task)
            self.warm_count += 1

    def pop_highest(self) -> Optional[Task]:
        # 1. Panic
        if self.panic_queue:
            self.panic_queue.sort(key=lambda t: t.deadline)
            return self.panic_queue.pop(0)
            
        # 2. Active
        if self.active_bitmap > 0:
            bucket_idx = (self.active_bitmap & -self.active_bitmap).bit_length() - 1
            bucket = self.active_array[bucket_idx]
            task = bucket.pop(0)
            self.active_count -= 1
            if not bucket:
                self.active_bitmap &= ~(1 << bucket_idx)
            return task
            
        # 3. Warm
        if self.warm_array:
            self.warm_count -= 1
            return self.warm_array.pop(0)
            
        return None

    def swap_if_needed(self, current_time: float, threshold: float):
        if self.active_count == 0 and self.warm_count == 0:
            if self.expired_bitmap > 0:
                self.active_array, self.expired_array = self.expired_array, self.active_array
                self.active_bitmap, self.expired_bitmap = self.expired_bitmap, self.active_bitmap
                self.active_count = sum(len(b) for b in self.active_array)
                self.oldest_expired_time = -1.0

class EBOSServerScheduler:
    def __init__(self, num_cores: int):
        self.num_cores = num_cores
        self.runqueues = {i: ServerRunQueue() for i in range(num_cores)}
        self.starvation_threshold = 15.0 
        self.warm_threshold = 600 # Principled threshold: 60th percentile
        
        self.category_boosts = {
            TaskCategory.INTERACTIVE: 500,
            TaskCategory.KERNEL: 400,
            TaskCategory.MULTIMEDIA: 300,
            TaskCategory.NETWORK: 200,
            TaskCategory.STORAGE: 100,
        }

    def _update_bucket(self, task: Task):
        clamped_score = max(0, min(1000, task.interactive_score))
        task.priority_bucket = 63 - (clamped_score * 63 // 1000)

    def _quantum_for_bucket(self, bucket: int) -> float:
        return 10.0 + (bucket / 63.0) * 90.0

    def pick_next_task(self, cpu: CPU, cpus: List[CPU], current_time: float = 0) -> Optional[Task]:
        rq = self.runqueues[cpu.core_id]
        rq.swap_if_needed(current_time, self.starvation_threshold)
        
        task = rq.pop_highest()
        if task: return task
        
        # Balanced Stealing: Steal if sibling has more than 2 tasks (O(1) check)
        for sibling in cpus:
            if sibling.core_id == cpu.core_id: continue
            s_rq = self.runqueues[sibling.core_id]
            if s_rq.active_count > 2:
                return s_rq.pop_highest()
        return None

    def on_task_ready(self, task: Task, category: Optional[TaskCategory] = None, current_time: float = 0):
        # Remove from any existing queue
        self._remove_from_queues(task)
        
        if task.sleep_start_time > 0:
            sleep_dur = current_time - task.sleep_start_time
            task.energy_credits = min(task.max_energy, task.energy_credits + sleep_dur * task.charge_rate)
            if category in self.category_boosts:
                task.interactive_score = min(1000, task.interactive_score + self.category_boosts[category])
            task.sleep_start_time = 0

        self._update_bucket(task)
        if task.home_core == -1: task.home_core = hash(task.id) % self.num_cores
        
        if task.quantum_remaining <= 0:
            task.quantum_remaining = self._quantum_for_bucket(task.priority_bucket) + min(10.0, task.energy_credits)
            task.energy_credits = max(0, task.energy_credits - 10.0)
            
        rq = self.runqueues[task.home_core]
        if task.interactive_score > self.warm_threshold:
            rq.add_to_warm(task)
        else:
            rq.add_to_active(task)

    def on_task_evicted(self, task: Task, current_time: float = 0):
        self._remove_from_queues(task)
        if task.category != TaskCategory.REALTIME:
            task.interactive_score >>= 1
        self._update_bucket(task)
        task.quantum_remaining = self._quantum_for_bucket(task.priority_bucket)
        self.runqueues[task.home_core].add_to_expired(task, current_time)

    def on_task_wait(self, task: Task):
        self._remove_from_queues(task)

    def on_task_terminate(self, task: Task):
        self._remove_from_queues(task)

    def _remove_from_queues(self, task: Task):
        if task.home_core == -1: return
        rq = self.runqueues[task.home_core]
        for arr in [rq.active_array, rq.expired_array]:
            for bucket in arr:
                if task in bucket:
                    bucket.remove(task)
                    if arr == rq.active_array: rq.active_count -= 1
        if task in rq.warm_array:
            rq.warm_array.remove(task)
            rq.warm_count -= 1
        if task in rq.panic_queue:
            rq.panic_queue.remove(task)
