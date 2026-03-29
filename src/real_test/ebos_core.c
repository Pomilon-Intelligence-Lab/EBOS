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
    Task* fast_path_head;
    Task* fast_path_tail;
} EBOS_PrioArray;

typedef struct {
    EBOS_PrioArray* active;
    EBOS_PrioArray* expired;
    EBOS_PrioArray arrays[2];
    pthread_spinlock_t lock;
} EBOS_RunQueue;

static EBOS_RunQueue core_rqs[MAX_CORES];
static int num_system_cores = 4;
static bool desktop_mode = false;

void init_ebos(int cores) {
    num_system_cores = cores;
    for (int i = 0; i < cores; i++) {
        memset(&core_rqs[i], 0, sizeof(EBOS_RunQueue));
        core_rqs[i].active = &core_rqs[i].arrays[0];
        core_rqs[i].expired = &core_rqs[i].arrays[1];
        pthread_spin_init(&core_rqs[i].lock, PTHREAD_PROCESS_PRIVATE);
    }
}

void set_ebos_mode(bool desktop) {
    desktop_mode = desktop;
}

int get_best_core() {
    int best = 0;
    int min_tasks = 99999;
    for (int i = 0; i < num_system_cores; i++) {
        if (core_rqs[i].active->task_count < min_tasks) {
            min_tasks = core_rqs[i].active->task_count;
            best = i;
        }
    }
    return best;
}

void _add_to_array(EBOS_PrioArray* array, Task* task) {
    // Fast-Path for Desktop mode
    if (desktop_mode && (task->category == CAT_INTERACTIVE || task->category == CAT_REALTIME)) {
        task->next = NULL;
        if (array->fast_path_tail) {
            array->fast_path_tail->next = task;
            array->fast_path_tail = task;
        } else {
            array->fast_path_head = array->fast_path_tail = task;
        }
        return;
    }

    int b = task->priority_bucket;
    Task** curr = &array->buckets[b];
    while (*curr && (*curr)->vruntime_residual < task->vruntime_residual) {
        curr = &(*curr)->next;
    }
    task->next = *curr;
    *curr = task;
    array->bitmap |= (1ULL << b);
    array->task_count++;
}

void ebos_requeue(Task* t, bool expired) {
    if (t->home_core == -1) t->home_core = get_best_core();
    EBOS_RunQueue* rq = &core_rqs[t->home_core];
    
    pthread_spin_lock(&rq->lock);
    if (expired) _add_to_array(rq->expired, t);
    else _add_to_array(rq->active, t);
    pthread_spin_unlock(&rq->lock);
}

Task* _pop_from_array(EBOS_PrioArray* array) {
    if (desktop_mode && array->fast_path_head) {
        Task* t = array->fast_path_head;
        array->fast_path_head = t->next;
        if (!array->fast_path_head) array->fast_path_tail = NULL;
        return t;
    }

    if (array->bitmap == 0) return NULL;
    int b = __builtin_ctzll(array->bitmap);
    Task* task = array->buckets[b];
    if (task) {
        array->buckets[b] = task->next;
        if (!array->buckets[b]) array->bitmap &= ~(1ULL << b);
        array->task_count--;
    }
    return task;
}

Task* ebos_pick_next_smp(int core_id) {
    EBOS_RunQueue* rq = &core_rqs[core_id];
    pthread_spin_lock(&rq->lock);
    
    Task* t = _pop_from_array(rq->active);
    if (t) {
        pthread_spin_unlock(&rq->lock);
        return t;
    }

    // Swap pointers if expired has tasks
    if (rq->expired->task_count > 0 || rq->expired->fast_path_head != NULL) {
        EBOS_PrioArray* tmp = rq->active;
        rq->active = rq->expired;
        rq->expired = tmp;
        t = _pop_from_array(rq->active);
    }
    
    pthread_spin_unlock(&rq->lock);
    return t;
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
