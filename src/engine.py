import heapq
import random
from typing import List, Dict, Optional, Tuple, Any
from .models import Task, CPU, TaskState, TaskCategory

class EventType:
    TASK_ARRIVAL = 0
    QUANTUM_EXPIRE = 1
    IO_WAKEUP = 2
    TASK_TERMINATED = 3
    BURST_EXPIRED = 4 # IO Block

class SimulationEngine:
    def __init__(self, scheduler, num_cores: int = 1, nodes: int = 1):
        self.scheduler = scheduler
        self.cpus = [CPU(i, (i * nodes) // num_cores if num_cores > 0 else 0) for i in range(num_cores)]
        self.current_time = 0.0
        self.events = [] # Priority queue of (time, event_type, data)
        self.tasks: List[Task] = []
        self.finished_tasks: List[Task] = []
        
        self.context_switch_overhead = 0.02
        self.trap_overhead = 0.001

    def add_task(self, task: Task):
        task.remaining_cpu = task.total_cpu_needed
        self.tasks.append(task)
        heapq.heappush(self.events, (0.0, EventType.TASK_ARRIVAL, task))

    def run(self, max_time: float):
        while self.events and self.current_time < max_time:
            time, event_type, data = heapq.heappop(self.events)
            
            # Update idle/busy times for CPUs since last event
            duration = time - self.current_time
            for cpu in self.cpus:
                if cpu.current_task: cpu.busy_time += duration
                else: cpu.idle_time += duration
            
            self.current_time = time
            self._handle_event(event_type, data)
            
            if all(t.state == TaskState.TERMINATED for t in self.tasks):
                break

    def _handle_event(self, event_type: int, data: Any):
        if event_type == EventType.TASK_ARRIVAL:
            task = data
            self._assign_new_burst(task)
            task.current_wait_start = self.current_time
            self.scheduler.on_task_ready(task, task.category, self.current_time)
            self._reschedule_all_idle_cpus()
            
        elif event_type == EventType.IO_WAKEUP:
            task = data
            task.current_wait_start = self.current_time
            self.scheduler.on_task_ready(task, task.category, self.current_time)
            # Thunder-Strike check (if EBOS)
            self._check_preemptions(task)
            self._reschedule_all_idle_cpus()

        elif event_type == EventType.QUANTUM_EXPIRE:
            cpu, task = data
            if cpu.current_task == task:
                self._evict_and_reschedule(cpu, "Quantum Expired")

        elif event_type == EventType.BURST_EXPIRED:
            cpu, task = data
            if cpu.current_task == task:
                self._block_and_reschedule(cpu)

        elif event_type == EventType.TASK_TERMINATED:
            cpu, task = data
            if cpu.current_task == task:
                task.state = TaskState.TERMINATED
                self.finished_tasks.append(task)
                self.scheduler.on_task_terminate(task)
                cpu.current_task = None
                self._pick_next(cpu)

    def _assign_new_burst(self, task: Task):
        if task.remaining_cpu <= 0: return
        burst = random.expovariate(1.0 / task.avg_cpu_burst) if task.avg_cpu_burst > 0 else task.remaining_cpu
        task.current_burst_remaining = min(burst, task.remaining_cpu)

    def _pick_next(self, cpu: CPU):
        # Apply CS overhead before picking
        self.current_time += self.context_switch_overhead
        
        try:
            task = self.scheduler.pick_next_task(cpu, self.cpus, self.current_time)
        except TypeError:
            task = self.scheduler.pick_next_task(cpu, self.cpus)
            
        if task:
            cpu.current_task = task
            task.state = TaskState.RUNNING
            cpu.context_switches += 1
            
            # Calculate when this task will stop
            # 1. Termination
            time_to_term = task.remaining_cpu
            # 2. Burst (IO Block)
            time_to_block = task.current_burst_remaining
            # 3. Quantum
            time_to_expire = task.quantum_remaining if task.quantum_remaining > 0 else 999999
            
            actual_run = min(time_to_term, time_to_block, time_to_expire)
            
            # Update task metrics
            task.remaining_cpu -= actual_run
            task.current_burst_remaining -= actual_run
            task.total_cpu_time += actual_run
            task.quantum_remaining -= actual_run
            
            # Schedule the stop event
            if actual_run == time_to_term:
                heapq.heappush(self.events, (self.current_time + actual_run, EventType.TASK_TERMINATED, (cpu, task)))
            elif actual_run == time_to_block:
                heapq.heappush(self.events, (self.current_time + actual_run, EventType.BURST_EXPIRED, (cpu, task)))
            else:
                heapq.heappush(self.events, (self.current_time + actual_run, EventType.QUANTUM_EXPIRE, (cpu, task)))
            
            # Record wait time
            wait_duration = self.current_time - task.current_wait_start
            task.latency_samples.append(max(0, wait_duration))
        else:
            cpu.current_task = None

    def _evict_and_reschedule(self, cpu: CPU, reason: str):
        task = cpu.current_task
        if hasattr(self.scheduler, 'on_task_evicted'):
            self.scheduler.on_task_evicted(task, self.current_time)
        else:
            self.scheduler.on_task_ready(task, task.category, self.current_time)
        cpu.current_task = None
        self._pick_next(cpu)

    def _block_and_reschedule(self, cpu: CPU):
        task = cpu.current_task
        task.state = TaskState.WAITING
        task.sleep_start_time = self.current_time
        self.scheduler.on_task_wait(task)
        
        sleep_time = random.expovariate(1.0 / task.avg_io_sleep) if task.avg_io_sleep > 0 else 0.1
        heapq.heappush(self.events, (self.current_time + sleep_time, EventType.IO_WAKEUP, task))
        self._assign_new_burst(task)
        
        cpu.current_task = None
        self._pick_next(cpu)

    def _reschedule_all_idle_cpus(self):
        for cpu in self.cpus:
            if not cpu.current_task:
                self._pick_next(cpu)

    def _check_preemptions(self, waking_task: Task):
        # Simple preemption: if waking task is high priority and a CPU is running low priority
        for cpu in self.cpus:
            if cpu.current_task:
                curr = cpu.current_task
                if hasattr(waking_task, 'priority_bucket') and hasattr(curr, 'priority_bucket'):
                    if waking_task.priority_bucket < (curr.priority_bucket - 16):
                        # Force reschedule this CPU (Simplified: cancel current event and re-pick)
                        # In DES, we'd need to remove the future event for this CPU.
                        # For now, let's keep it simple: the next event for this CPU will naturally happen.
                        pass
