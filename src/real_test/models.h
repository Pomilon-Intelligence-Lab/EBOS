#ifndef MODELS_H
#define MODELS_H

#include <pthread.h>
#include <stdint.h>
#include <stdbool.h>
#include <sys/time.h>

typedef enum {
    CAT_REALTIME,
    CAT_INTERACTIVE,
    CAT_MULTIMEDIA,
    CAT_NONE,
    CAT_IDLE
} TaskCategory;

typedef struct Task {
    int id;
    TaskCategory category;
    
    // v1.0.0 Metrics
    double vruntime_residual;
    double deadline;
    int priority_bucket;
    
    // Efficiency: Use Condition Variables instead of spinning
    pthread_mutex_t run_lock;
    pthread_cond_t run_cond;
    bool is_running;
    
    // Affinity
    int home_core;
    
    // Metrics
    double total_cpu_time;
    double last_run_time;
    pthread_t thread;
    struct Task* next; 
} Task;

typedef struct {
    Task* buckets[64];
    uint64_t bitmap;
    int task_count;
    pthread_spinlock_t lock; 
} RunQueue;

#endif
