from typing import List, Optional
import math
from ..models import Task, CPU, TaskState, TaskCategory

class RoundRobinScheduler:
    def __init__(self, quantum: float = 20.0):
        self.ready_queue: List[Task] = []
        self.quantum = quantum
        
    def pick_next_task(self, cpu: CPU, cpus: List[CPU]) -> Optional[Task]:
        if not self.ready_queue: return None
        task = self.ready_queue.pop(0)
        task.quantum_remaining = self.quantum
        return task
        
    def on_task_ready(self, task: Task, category: Optional[TaskCategory] = None, current_time: float = 0):
        if task not in self.ready_queue:
            self.ready_queue.append(task)
        
    def on_task_wait(self, task: Task):
        pass
        
    def on_task_terminate(self, task: Task):
        pass

class CFSScheduler:
    def __init__(self, sched_latency: float = 24.0, min_granularity: float = 3.0):
        self.ready_queue: List[Task] = []
        self.sched_latency = sched_latency
        self.min_granularity = min_granularity
        self.weights = {
            TaskCategory.REALTIME: 10240,
            TaskCategory.INTERACTIVE: 5120,
            TaskCategory.NONE: 1024,
            TaskCategory.IDLE: 15,
        }
        
    def pick_next_task(self, cpu: CPU, cpus: List[CPU]) -> Optional[Task]:
        if not self.ready_queue: return None
        
        # Pick task with min vruntime
        # vruntime = total_cpu_time * (1024 / weight)
        self.ready_queue.sort(key=lambda t: t.total_cpu_time * (1024 / self.weights.get(t.category, 1024)))
        task = self.ready_queue.pop(0)
        
        # Calculate slice
        total_weight = sum(self.weights.get(t.category, 1024) for t in self.ready_queue) + self.weights.get(task.category, 1024)
        slice_ms = (self.weights.get(task.category, 1024) / total_weight) * self.sched_latency
        task.quantum_remaining = max(self.min_granularity, slice_ms)
        return task
        
    def on_task_ready(self, task: Task, category: Optional[TaskCategory] = None, current_time: float = 0):
        if task not in self.ready_queue:
            self.ready_queue.append(task)
        
    def on_task_wait(self, task: Task):
        pass
        
    def on_task_terminate(self, task: Task):
        pass

class MLFQScheduler:
    def __init__(self, num_levels: int = 4, base_quantum: float = 10.0, boost_interval: float = 100.0):
        self.queues: List[List[Task]] = [[] for _ in range(num_levels)]
        self.base_quantum = base_quantum
        self.boost_interval = boost_interval
        self.last_boost = 0.0
        
    def pick_next_task(self, cpu: CPU, cpus: List[CPU], current_time: float = 0) -> Optional[Task]:
        # Priority Boost
        if current_time - self.last_boost >= self.boost_interval:
            for i in range(1, len(self.queues)):
                while self.queues[i]:
                    t = self.queues[i].pop(0)
                    t.mlfq_level = 0
                    self.queues[0].append(t)
            self.last_boost = current_time

        for i, q in enumerate(self.queues):
            if q:
                task = q.pop(0)
                task.mlfq_level = i
                task.quantum_remaining = self.base_quantum * (2 ** i)
                return task
        return None
        
    def on_task_ready(self, task: Task, category: Optional[TaskCategory] = None, current_time: float = 0):
        level = getattr(task, 'mlfq_level', 0)
        if task not in self.queues[level]:
            self.queues[level].append(task)
        
    def on_task_evicted(self, task: Task, current_time: float = 0):
        level = min(getattr(task, 'mlfq_level', 0) + 1, len(self.queues) - 1)
        task.mlfq_level = level
        self.on_task_ready(task, task.category, current_time)

    def on_task_wait(self, task: Task):
        # Demote slightly if blocked? No, standard MLFQ keeps level or promotes.
        # We'll promote to top level on wake to favor interactivity (Rules of MLFQ)
        task.mlfq_level = 0

    def on_task_terminate(self, task: Task):
        pass
