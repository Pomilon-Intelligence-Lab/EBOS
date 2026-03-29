from typing import List, Optional, Tuple, Dict
from ..models import Task, CPU, TaskState, TaskCategory
from .ebos_server import EBOSServerScheduler, ServerRunQueue
import math

class DesktopRunQueue(ServerRunQueue):
    def __init__(self):
        super().__init__()
        self.fast_path: List[Task] = []
        
    def add_to_fast_path(self, task: Task):
        if task not in self.fast_path:
            self.fast_path.append(task)
            
    def pop_highest(self) -> Optional[Task]:
        # 1. Fast-Path (Absolute Priority for UI/RT)
        if self.fast_path:
            return self.fast_path.pop(0)
        return super().pop_highest()

class EBOSDesktopScheduler(EBOSServerScheduler):
    def __init__(self, num_cores: int):
        super().__init__(num_cores)
        self.runqueues = {i: DesktopRunQueue() for i in range(num_cores)}
        self.starvation_threshold = 10.0 # Faster swap for Desktop
        self.warm_threshold = 600

    def pick_next_task(self, cpu: CPU, cpus: List[CPU], current_time: float = 0) -> Optional[Task]:
        rq = self.runqueues[cpu.core_id]
        if rq.active_count == 0 and rq.warm_count == 0 and not rq.fast_path:
            rq.swap_if_needed(current_time, self.starvation_threshold)
            
        task = rq.pop_highest()
        if task: return task
        
        # Fixed Stealing: Must check fast_path specifically for v1.0.0
        for sibling in cpus:
            if sibling.core_id == cpu.core_id: continue
            s_rq = self.runqueues[sibling.core_id]
            # Steal if sibling has fast_path tasks or more than 1 active task
            if s_rq.fast_path or s_rq.active_count > 1:
                return s_rq.pop_highest()
        return None

    def on_task_ready(self, task: Task, category: Optional[TaskCategory] = None, current_time: float = 0):
        self._remove_from_queues(task)
        
        if task.sleep_start_time > 0:
            sleep_dur = current_time - task.sleep_start_time
            task.energy_credits = min(task.max_energy, task.energy_credits + sleep_dur * task.charge_rate)
            if category in self.category_boosts:
                task.interactive_score = min(1000, task.interactive_score + self.category_boosts[category] + 100)
            task.sleep_start_time = 0

        self._update_bucket(task)
        if task.home_core == -1: task.home_core = hash(task.id) % self.num_cores
        
        if task.quantum_remaining <= 0:
            task.quantum_remaining = self._quantum_for_bucket(task.priority_bucket) + min(15.0, task.energy_credits)
            task.energy_credits = max(0, task.energy_credits - 15.0)
            
        rq = self.runqueues[task.home_core]
        if task.category in [TaskCategory.REALTIME, TaskCategory.INTERACTIVE] and task.priority_bucket < 16:
            rq.add_to_fast_path(task)
        else:
            rq.add_to_active(task)

    def _remove_from_queues(self, task: Task):
        super()._remove_from_queues(task)
        if task.home_core != -1:
            rq = self.runqueues[task.home_core]
            if task in rq.fast_path:
                rq.fast_path.remove(task)
