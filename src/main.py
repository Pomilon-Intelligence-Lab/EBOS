from .models import Task, TaskState, TaskCategory
from .schedulers.ebos_desktop import EBOSDesktopScheduler
from .schedulers.ebos_server import EBOSServerScheduler
from .schedulers.baseline import RoundRobinScheduler, CFSScheduler, MLFQScheduler
from .engine import SimulationEngine
import random
import copy
import math
from typing import List, Dict

class SummaryReporter:
    def __init__(self):
        self.results = []

    def record(self, scenario: str, scheduler_name: str, metrics: Dict):
        self.results.append({
            "Scenario": scenario,
            "Scheduler": scheduler_name,
            **metrics
        })

    def print_table(self):
        if not self.results: return
        print("\n" + "="*125)
        print(f"{'SCENARIO':<22} | {'SCHED':<12} | {'AVG LAT':<8} | {'P99 JITTER':<10} | {'TPUT':<6} | {'CS':<6} | {'FIN'}")
        print("-" * 125)
        for r in self.results:
            print(f"{r['Scenario']:<22} | {r['Scheduler']:<12} | {r['Avg Latency']:<8.2f} | {r['P99 Jitter']:<10.2f} | {r['Throughput']:<6.1f} | {r['CS']:<6} | {r['Finished']}")
        print("="*125 + "\n")

reporter = SummaryReporter()

def calculate_p99(samples: List[float]) -> float:
    if not samples: return 0.0
    sorted_samples = sorted(samples)
    idx = int(0.99 * len(sorted_samples))
    return sorted_samples[min(idx, len(sorted_samples)-1)]

def run_simulation(scenario_name, scheduler, task_templates, max_time=10000, num_cores=4, nodes=2):
    tasks = [copy.deepcopy(t) for t in task_templates]
    engine = SimulationEngine(scheduler, num_cores=num_cores, nodes=nodes)
    for t in tasks:
        engine.add_task(t)
    
    print(f"  [Scenario] {scenario_name} starting with {scheduler.__class__.__name__}...", flush=True)
    engine.run(max_time)
    
    all_samples = []
    for t in engine.tasks:
        all_samples.extend(t.latency_samples)
        
    avg_latency = sum(all_samples) / len(all_samples) if all_samples else 0
    p99_jitter = calculate_p99(all_samples)
    throughput = len(engine.finished_tasks) / (engine.current_time / 1000.0) if engine.current_time > 0 else 0
    switches = sum(cpu.context_switches for cpu in engine.cpus)
    finished_str = f"{len(engine.finished_tasks)}/{len(tasks)}"
    
    sched_name = scheduler.__class__.__name__.replace("Scheduler", "")
    reporter.record(scenario_name, sched_name, {
        "Avg Latency": avg_latency,
        "P99 Jitter": p99_jitter,
        "Throughput": throughput,
        "CS": switches,
        "Finished": finished_str
    })

def get_real_world_trace():
    tasks = []
    # 1. High-FPS Game
    for i in range(1):
        tasks.append(Task(i, "Game_Engine", category=TaskCategory.MULTIMEDIA, total_cpu_needed=1000, avg_cpu_burst=6.9, avg_io_sleep=0.1))
    # 2. Browser
    for i in range(1, 4):
        tasks.append(Task(i, f"Browser_{i}", category=TaskCategory.INTERACTIVE, total_cpu_needed=500, avg_cpu_burst=15, avg_io_sleep=100))
    # 3. Audio
    for i in range(4, 5):
        tasks.append(Task(i, "Audio_Srv", category=TaskCategory.REALTIME, total_cpu_needed=300, avg_cpu_burst=1, avg_io_sleep=10))
    # 4. Batch
    for i in range(5, 15):
        tasks.append(Task(i, f"Batch_{i}", category=TaskCategory.NONE, total_cpu_needed=2000, avg_cpu_burst=100, avg_io_sleep=0))
    return tasks

def get_server_load():
    tasks = []
    for i in range(100):
        tasks.append(Task(i, f"Req_{i}", category=TaskCategory.NETWORK, total_cpu_needed=100, avg_cpu_burst=5, avg_io_sleep=50))
    for i in range(100, 110):
        tasks.append(Task(i, f"DB_{i}", category=TaskCategory.NONE, total_cpu_needed=500, avg_cpu_burst=50, avg_io_sleep=20))
    return tasks

def get_absurd_load():
    tasks = []
    for i in range(500):
        tasks.append(Task(i, f"W_{i}", category=TaskCategory.NETWORK, total_cpu_needed=50, avg_cpu_burst=2, avg_io_sleep=20))
    for i in range(500, 1000):
        tasks.append(Task(i, f"S_{i}", category=TaskCategory.NONE, total_cpu_needed=100, avg_cpu_burst=20, avg_io_sleep=10))
    return tasks

def run_all(name, workload, cores=4, max_time=10000):
    print(f"\n--- Benchmarking Scenario: {name} ---", flush=True)
    run_simulation(name, EBOSDesktopScheduler(cores), workload, max_time=max_time, num_cores=cores)
    run_simulation(name, EBOSServerScheduler(cores), workload, max_time=max_time, num_cores=cores)
    run_simulation(name, MLFQScheduler(), workload, max_time=max_time, num_cores=cores)
    run_simulation(name, CFSScheduler(), workload, max_time=max_time, num_cores=cores)
    run_simulation(name, RoundRobinScheduler(20), workload, max_time=max_time, num_cores=cores)

if __name__ == "__main__":
    random.seed(42)
    run_all("Modern Desktop", get_real_world_trace(), cores=4, max_time=15000)
    run_all("Server Concurrency", get_server_load(), cores=8, max_time=10000)
    run_all("Absurd Load (1k)", get_absurd_load(), cores=12, max_time=10000)
    reporter.print_table()
