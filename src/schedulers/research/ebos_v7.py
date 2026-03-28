from typing import List, Optional, Tuple, Dict
from ..models import Task, CPU, Mutex, TaskState, TaskCategory
from .ebos_v6 import EBOSv6Scheduler, ExtremeRunQueue
import abc
import math

class ThunderRunQueue(ExtremeRunQueue):
    def __init__(self):
        super().__init__()
        self.fast_path: List[Task] = []
        self.last_thunder_strike = -10.0
        
    def add_to_fast_path(self, task: Task):
        if task not in self.fast_path:
            self.fast_path.append(task)
            
    def pop_highest_thunder(self) -> Optional[Task]:
        if self.fast_path:
            return self.fast_path.pop(0)
        return self.pop_highest_extreme()

class EBOSv7Scheduler(EBOSv6Scheduler):
    def __init__(self, num_cores: int):
        self.num_cores = num_cores
        self.runqueues = {i: ThunderRunQueue() for i in range(num_cores)}
        self.starvation_threshold = 10.0 
        self.category_boosts = {
            TaskCategory.INTERACTIVE: 600,
            TaskCategory.KERNEL: 400,
            TaskCategory.MULTIMEDIA: 350,
            TaskCategory.NETWORK: 200,
            TaskCategory.STORAGE: 100,
            TaskCategory.NONE: 0,
        }
        self.deadlines = {
            TaskCategory.REALTIME: 0.5,
            TaskCategory.INTERACTIVE: 8.0,
            TaskCategory.MULTIMEDIA: 6.9,   
        }
        self.global_last_input_time = 0.0

    def pick_next_task(self, cpu: CPU, cpus: List[CPU], current_time: float = 0) -> Optional[Task]:
        rq = self.runqueues[cpu.core_id]
        
        if rq.active_bitmap == 0 and not rq.fast_path and not rq.panic_queue:
            if rq.oldest_expired_time > 0 and current_time - rq.oldest_expired_time >= self.starvation_threshold:
                rq.swap_arrays()
        
        task = rq.pop_highest_thunder()
        if task:
            task.actual_wake_time = current_time
            return task
            
        # 2. Strict Sticky-Cache Affinity (Only steal if sibling is truly desperate)
        for sibling in cpus:
            if sibling.core_id == cpu.core_id: continue
            sibling_rq = self.runqueues[sibling.core_id]
            if sibling_rq.active_count > 8: # Higher threshold to preserve affinity
                task = sibling_rq.pop_highest_thunder()
                if task and (task.home_core != sibling.core_id or task.category == TaskCategory.NONE):
                    task.actual_wake_time = current_time
                    return task
        return None

    def on_task_ready(self, task: Task, category: Optional[TaskCategory] = None, current_time: float = 0):
        task.state = TaskState.READY
        
        if task.home_core == -1:
            # Home core assignment
            best_core = 0
            min_load = 9999
            for i in range(self.num_cores):
                load = self.runqueues[i].active_count + len(self.runqueues[i].fast_path)
                if load < min_load:
                    min_load = load
                    best_core = i
            task.home_core = best_core
        
        target_core = task.home_core
        
        if task.category == TaskCategory.INTERACTIVE:
            self.global_last_input_time = current_time
            
        if task.category in self.deadlines:
            task.deadline = current_time + self.deadlines[task.category]
            
        if task.sleep_start_time > 0:
            if task.quantum_bank > 0:
                task.quantum_remaining += task.quantum_bank
                task.quantum_bank = 0
            task.sleep_start_time = 0
            
        self._update_bucket(task, current_time)
        
        # High-Alert Aware Quantum assignment
        if task.quantum_remaining <= 0:
            active_count = self.runqueues[target_core].active_count
            base_q = self._quantum_for_bucket(task.priority_bucket, active_count)
            if (current_time - self.global_last_input_time) < 50.0:
                if task.category in [TaskCategory.NONE, TaskCategory.IDLE]:
                    base_q = min(4.0, base_q)
            task.quantum_remaining = base_q + min(10.0, task.energy_credits)
            task.energy_credits = max(0, task.energy_credits - 10.0)

        # Fast-Path logic
        if task.category == TaskCategory.REALTIME:
            self.runqueues[target_core].add_to_fast_path(task)
        elif task.category == TaskCategory.INTERACTIVE and task.priority_bucket < 4:
            self.runqueues[target_core].add_to_fast_path(task)
        elif task.deadline > 0 and (task.deadline - current_time) < 1.0:
            self.runqueues[target_core].add_to_panic(task)
        else:
            self.runqueues[target_core].add_to_active(task)

    def on_task_evicted(self, task: Task, current_time: float = 0):
        # Pulse Slicing Hysteresis
        if task.category == TaskCategory.REALTIME and task.remaining_cpu > 0:
            task.state = TaskState.READY
            self.runqueues[task.home_core].add_to_fast_path(task)
            return

        self._update_vruntime_residual(task, 2.0)
        
        if task.interactive_score > 600: # Higher threshold for Warm
            task.state = TaskState.READY
            self.runqueues[task.home_core].add_to_warm(task)
            return

        if task.category != TaskCategory.REALTIME:
            task.interactive_score >>= 1
        
        self._update_bucket(task, current_time)
        task.state = TaskState.READY
        self.runqueues[task.home_core].add_to_expired(task, current_time)
