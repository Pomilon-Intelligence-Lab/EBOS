import enum
from typing import List, Optional, Dict
import dataclasses

class TaskState(enum.Enum):
    READY = "ready"
    RUNNING = "running"
    WAITING = "waiting"
    TERMINATED = "terminated"

class TaskCategory(enum.Enum):
    REALTIME = "realtime"      # Bucket 0
    INTERACTIVE = "interactive" # +500
    KERNEL = "kernel"           # +400
    MULTIMEDIA = "multimedia"   # +300
    NETWORK = "network"         # +200
    STORAGE = "storage"         # +100
    NONE = "none"
    IDLE = "idle"               # Bucket 63

@dataclasses.dataclass
class Task:
    id: int
    name: str
    category: TaskCategory = TaskCategory.NONE
    
    # EBOS State
    interactive_score: int = 0    
    priority_bucket: int = 63     
    energy_credits: float = 0.0
    max_energy: float = 1000.0
    charge_rate: float = 5.0      
    vruntime_residual: float = 0.0
    deadline: float = 0.0
    quantum_bank: float = 0.0
    home_core: int = -1
    
    # Workload
    total_cpu_needed: float = 0.0
    remaining_cpu: float = 0.0
    avg_cpu_burst: float = 10.0
    avg_io_sleep: float = 50.0
    
    # Metrics
    total_cpu_time: float = 0.0
    io_wait_time: float = 0.0
    ready_wait_time: float = 0.0
    latency_samples: List[float] = dataclasses.field(default_factory=list)
    current_wait_start: float = 0.0
    arrival_time: float = 0.0
    completion_time: float = 0.0
    sleep_start_time: float = 0.0
    
    state: TaskState = TaskState.READY
    quantum_remaining: float = 0.0
    current_burst_remaining: float = 0.0
    
    def __hash__(self):
        return hash(self.id)

    def __lt__(self, other):
        if not isinstance(other, Task): return False
        return self.id < other.id

class CPU:
    def __init__(self, core_id: int, node_id: int = 0):
        self.core_id = core_id
        self.node_id = node_id 
        self.current_task: Optional[Task] = None
        self.context_switches: int = 0
        self.idle_time: float = 0.0
        self.busy_time: float = 0.0
        self.last_event_time: float = 0.0

    def __lt__(self, other):
        if not isinstance(other, CPU): return False
        return self.core_id < other.core_id
