#include "models.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>

double get_time_ms() {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec * 1000.0 + ts.tv_nsec / 1000000.0;
}

#define MAX_CORES 16

typedef struct {
    Task* buckets[64];
    uint64_t bitmap;
    int task_count;
    pthread_spinlock_t lock; 
    
    // EBOS Desktop specific: Fast-Path (Low Jitter)
    Task* fast_path_head;
    Task* fast_path_tail;
} EBOS_RunQueue;

static EBOS_RunQueue core_active_rq[MAX_CORES];
static EBOS_RunQueue core_expired_rq[MAX_CORES];
static int num_system_cores = 4;
static bool desktop_mode = false;

void init_ebos(int cores) {
    num_system_cores = cores;
    for (int i = 0; i < cores; i++) {
        memset(&core_active_rq[i], 0, sizeof(EBOS_RunQueue));
        pthread_spin_init(&core_active_rq[i].lock, PTHREAD_PROCESS_PRIVATE);
        
        memset(&core_expired_rq[i], 0, sizeof(EBOS_RunQueue));
        pthread_spin_init(&core_expired_rq[i].lock, PTHREAD_PROCESS_PRIVATE);
    }
}

void set_ebos_mode(bool desktop) {
    desktop_mode = desktop;
}

int get_best_core() {
    int best = 0;
    int min_tasks = 99999;
    for (int i = 0; i < num_system_cores; i++) {
        if (core_active_rq[i].task_count < min_tasks) {
            min_tasks = core_active_rq[i].task_count;
            best = i;
        }
    }
    return best;
}

void add_to_core_rq(EBOS_RunQueue* rq, Task* task) {
    pthread_spin_lock(&rq->lock);
    
    // Fast-Path for Desktop mode
    if (desktop_mode && (task->category == CAT_INTERACTIVE || task->category == CAT_REALTIME)) {
        task->next = NULL;
        if (rq->fast_path_tail) {
            rq->fast_path_tail->next = task;
            rq->fast_path_tail = task;
        } else {
            rq->fast_path_head = rq->fast_path_tail = task;
        }
        pthread_spin_unlock(&rq->lock);
        return;
    }

    int b = task->priority_bucket;
    Task** curr = &rq->buckets[b];
    while (*curr && (*curr)->vruntime_residual < task->vruntime_residual) {
        curr = &(*curr)->next;
    }
    task->next = *curr;
    *curr = task;
    rq->bitmap |= (1ULL << b);
    rq->task_count++;
    pthread_spin_unlock(&rq->lock);
}

Task* pop_from_core_rq(EBOS_RunQueue* rq) {
    pthread_spin_lock(&rq->lock);
    
    // Fast-Path Priority for Desktop mode
    if (desktop_mode && rq->fast_path_head) {
        Task* t = rq->fast_path_head;
        rq->fast_path_head = t->next;
        if (!rq->fast_path_head) rq->fast_path_tail = NULL;
        pthread_spin_unlock(&rq->lock);
        return t;
    }

    if (rq->bitmap == 0) {
        pthread_spin_unlock(&rq->lock);
        return NULL;
    }
    int b = __builtin_ctzll(rq->bitmap);
    Task* task = rq->buckets[b];
    if (task) {
        rq->buckets[b] = task->next;
        if (!rq->buckets[b]) rq->bitmap &= ~(1ULL << b);
        rq->task_count--;
    }
    pthread_spin_unlock(&rq->lock);
    return task;
}

Task* ebos_pick_next_smp(int core_id) {
    Task* t = pop_from_core_rq(&core_active_rq[core_id]);
    if (t) return t;

    if (core_expired_rq[core_id].task_count > 0 || core_expired_rq[core_id].fast_path_head != NULL) {
        pthread_spin_lock(&core_active_rq[core_id].lock);
        pthread_spin_lock(&core_expired_rq[core_id].lock);
        
        for (int i = 0; i < 64; i++) {
            Task* tmp = core_active_rq[core_id].buckets[i];
            core_active_rq[core_id].buckets[i] = core_expired_rq[core_id].buckets[i];
            core_expired_rq[core_id].buckets[i] = tmp;
        }
        
        Task* tmp_h = core_active_rq[core_id].fast_path_head;
        core_active_rq[core_id].fast_path_head = core_expired_rq[core_id].fast_path_head;
        core_expired_rq[core_id].fast_path_head = tmp_h;
        
        Task* tmp_t = core_active_rq[core_id].fast_path_tail;
        core_active_rq[core_id].fast_path_tail = core_expired_rq[core_id].fast_path_tail;
        core_expired_rq[core_id].fast_path_tail = tmp_t;

        uint64_t tmp_bm = core_active_rq[core_id].bitmap;
        core_active_rq[core_id].bitmap = core_expired_rq[core_id].bitmap;
        core_expired_rq[core_id].bitmap = tmp_bm;
        
        int tmp_count = core_active_rq[core_id].task_count;
        core_active_rq[core_id].task_count = core_expired_rq[core_id].task_count;
        core_expired_rq[core_id].task_count = tmp_count;
        
        pthread_spin_unlock(&core_expired_rq[core_id].lock);
        pthread_spin_unlock(&core_active_rq[core_id].lock);
        return pop_from_core_rq(&core_active_rq[core_id]);
    }
    return NULL;
}

void ebos_requeue(Task* t, bool expired) {
    if (t->home_core == -1) t->home_core = get_best_core();
    if (expired) add_to_core_rq(&core_expired_rq[t->home_core], t);
    else add_to_core_rq(&core_active_rq[t->home_core], t);
}

// Baseline implementations (MLFQ, CFS, RR)
#define MLFQ_LEVELS 4
static Task* mlfq_queues[MLFQ_LEVELS];
static pthread_spinlock_t mlfq_lock;
static double mlfq_last_boost = 0;

void init_mlfq() {
    memset(mlfq_queues, 0, sizeof(mlfq_queues));
    pthread_spin_init(&mlfq_lock, PTHREAD_PROCESS_PRIVATE);
    mlfq_last_boost = get_time_ms();
}

void mlfq_add(Task* t, int level) {
    pthread_spin_lock(&mlfq_lock);
    t->next = mlfq_queues[level];
    mlfq_queues[level] = t;
    pthread_spin_unlock(&mlfq_lock);
}

Task* mlfq_pick_next(double current_time) {
    if (current_time - mlfq_last_boost > 100.0) {
        pthread_spin_lock(&mlfq_lock);
        for (int i = 1; i < MLFQ_LEVELS; i++) {
            while (mlfq_queues[i]) {
                Task* t = mlfq_queues[i];
                mlfq_queues[i] = t->next;
                t->next = mlfq_queues[0];
                mlfq_queues[0] = t;
            }
        }
        mlfq_last_boost = current_time;
        pthread_spin_unlock(&mlfq_lock);
    }
    pthread_spin_lock(&mlfq_lock);
    for (int i = 0; i < MLFQ_LEVELS; i++) {
        if (mlfq_queues[i]) {
            Task* t = mlfq_queues[i];
            mlfq_queues[i] = t->next;
            pthread_spin_unlock(&mlfq_lock);
            return t;
        }
    }
    pthread_spin_unlock(&mlfq_lock);
    return NULL;
}

static Task* cfs_list = NULL;
static pthread_spinlock_t cfs_lock;

void init_cfs() {
    cfs_list = NULL;
    pthread_spin_init(&cfs_lock, PTHREAD_PROCESS_PRIVATE);
}

void cfs_add(Task* t) {
    pthread_spin_lock(&cfs_lock);
    Task** curr = &cfs_list;
    double weight_t = (t->category == CAT_REALTIME) ? 10.0 : (t->category == CAT_INTERACTIVE ? 5.0 : 1.0);
    double vr_t = t->total_cpu_time / weight_t;

    while (*curr) {
        double weight_curr = ((*curr)->category == CAT_REALTIME) ? 10.0 : ((*curr)->category == CAT_INTERACTIVE ? 5.0 : 1.0);
        double vr_curr = (*curr)->total_cpu_time / weight_curr;
        if (vr_t < vr_curr) break;
        curr = &(*curr)->next;
    }
    t->next = *curr;
    *curr = t;
    pthread_spin_unlock(&cfs_lock);
}

Task* cfs_pick_next() {
    pthread_spin_lock(&cfs_lock);
    if (!cfs_list) { pthread_spin_unlock(&cfs_lock); return NULL; }
    Task* best = cfs_list;
    cfs_list = best->next;
    pthread_spin_unlock(&cfs_lock);
    return best;
}
