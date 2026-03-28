#include "models.h"
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
#include <time.h>
#include <string.h>
#include <pthread.h>

extern void init_ebos(int cores);
extern void set_ebos_mode(bool desktop);
extern Task* ebos_pick_next_smp(int core_id);
extern void ebos_requeue(Task* t, bool expired);

extern void init_mlfq();
extern void mlfq_add(Task* t, int level);
extern Task* mlfq_pick_next(double current_time);

extern void init_cfs();
extern void cfs_add(Task* t);
extern Task* cfs_pick_next();

extern double get_time_ms();

#define MAX_BENCH_TASKS 1000
#define NUM_CORES 8
#define ITERATIONS_PER_CORE 1000

Task bench_tasks[MAX_BENCH_TASKS];
bool global_done = false;
double max_jitter_global = 0;
pthread_spinlock_t jitter_spin;

void do_intensive_work() {
    volatile uint64_t n = 50000;
    while(n--) ;
}

void* task_body(void* arg) {
    Task* t = (Task*)arg;
    while (1) {
        pthread_mutex_lock(&t->run_lock);
        while (!t->is_running && !global_done) {
            pthread_cond_wait(&t->run_cond, &t->run_lock);
        }
        if (global_done && !t->is_running) {
            pthread_mutex_unlock(&t->run_lock);
            break;
        }
        pthread_mutex_unlock(&t->run_lock);

        double start = get_time_ms();
        if (t->category == CAT_INTERACTIVE) usleep(1);
        else do_intensive_work();
        double end = get_time_ms();
        t->total_cpu_time += (end - start);

        pthread_mutex_lock(&t->run_lock);
        t->is_running = false;
        pthread_cond_signal(&t->run_cond);
        pthread_mutex_unlock(&t->run_lock);
    }
    return NULL;
}

typedef enum { MODE_EBOS_DESKTOP, MODE_EBOS_SERVER, MODE_MLFQ, MODE_CFS, MODE_RR } SchedMode;

typedef struct {
    int core_id;
    SchedMode mode;
    int n_tasks;
} CoreArgs;

static Task* rr_head = NULL;
static Task* rr_tail = NULL;
static pthread_spinlock_t rr_lock;

void rr_add(Task* t) {
    pthread_spin_lock(&rr_lock);
    t->next = NULL;
    if (rr_tail) { rr_tail->next = t; rr_tail = t; }
    else { rr_head = rr_tail = t; }
    pthread_spin_unlock(&rr_lock);
}

Task* rr_pop() {
    pthread_spin_lock(&rr_lock);
    if (!rr_head) { pthread_spin_unlock(&rr_lock); return NULL; }
    Task* t = rr_head;
    rr_head = t->next;
    if (!rr_head) rr_tail = NULL;
    pthread_spin_unlock(&rr_lock);
    return t;
}

void* core_loop(void* a) {
    CoreArgs* args = (CoreArgs*)a;
    int cid = args->core_id;
    SchedMode mode = args->mode;
    
    for(int i = 0; i < ITERATIONS_PER_CORE; i++) {
        if (global_done) break;
        Task* n = NULL;
        
        if (mode == MODE_EBOS_DESKTOP || mode == MODE_EBOS_SERVER) n = ebos_pick_next_smp(cid);
        else if (mode == MODE_MLFQ) n = mlfq_pick_next(get_time_ms());
        else if (mode == MODE_CFS) n = cfs_pick_next();
        else n = rr_pop();

        if(!n) { 
            usleep(10); 
            i--; 
            continue; 
        }

        if(n->category == CAT_INTERACTIVE) {
            double now = get_time_ms();
            if (n->last_run_time > 0) {
                double jitter = now - n->last_run_time;
                pthread_spin_lock(&jitter_spin);
                if(jitter > max_jitter_global) max_jitter_global = jitter;
                pthread_spin_unlock(&jitter_spin);
            }
            n->last_run_time = now;
        }

        pthread_mutex_lock(&n->run_lock);
        n->is_running = true; 
        pthread_cond_signal(&n->run_cond);
        while(n->is_running && !global_done) {
            pthread_cond_wait(&n->run_cond, &n->run_lock);
        }
        pthread_mutex_unlock(&n->run_lock);

        if (mode == MODE_EBOS_DESKTOP || mode == MODE_EBOS_SERVER) ebos_requeue(n, true);
        else if (mode == MODE_MLFQ) mlfq_add(n, 1);
        else if (mode == MODE_CFS) cfs_add(n);
        else rr_add(n);
    }
    return NULL;
}

void run_final_benchmark(const char* scenario_name, int n_tasks, SchedMode mode) {
    global_done = false; 
    max_jitter_global = 0;
    init_ebos(NUM_CORES);
    set_ebos_mode(mode == MODE_EBOS_DESKTOP);
    init_mlfq();
    init_cfs();
    rr_head = rr_tail = NULL;
    pthread_spin_init(&rr_lock, 0);

    for (int i = 0; i < n_tasks; i++) {
        memset(&bench_tasks[i], 0, sizeof(Task));
        bench_tasks[i].id = i;
        bench_tasks[i].category = (i < 10) ? CAT_INTERACTIVE : CAT_NONE;
        bench_tasks[i].priority_bucket = (i < 10) ? 0 : 63;
        bench_tasks[i].home_core = -1;
        pthread_mutex_init(&bench_tasks[i].run_lock, NULL);
        pthread_cond_init(&bench_tasks[i].run_cond, NULL);
        pthread_create(&bench_tasks[i].thread, NULL, task_body, &bench_tasks[i]);
        
        if (mode == MODE_EBOS_DESKTOP || mode == MODE_EBOS_SERVER) ebos_requeue(&bench_tasks[i], false);
        else if (mode == MODE_MLFQ) mlfq_add(&bench_tasks[i], 0);
        else if (mode == MODE_CFS) cfs_add(&bench_tasks[i]);
        else rr_add(&bench_tasks[i]);
    }

    double start_time = get_time_ms();
    pthread_t cores[NUM_CORES];
    CoreArgs args[NUM_CORES];
    for(int i = 0; i < NUM_CORES; i++) {
        args[i].core_id = i;
        args[i].mode = mode;
        args[i].n_tasks = n_tasks;
        pthread_create(&cores[i], NULL, core_loop, &args[i]);
    }
    for(int i = 0; i < NUM_CORES; i++) pthread_join(cores[i], NULL);

    global_done = true;
    for (int i = 0; i < n_tasks; i++) {
        pthread_mutex_lock(&bench_tasks[i].run_lock);
        pthread_cond_signal(&bench_tasks[i].run_cond);
        pthread_mutex_unlock(&bench_tasks[i].run_lock);
        pthread_join(bench_tasks[i].thread, NULL);
    }
    double total_time = get_time_ms() - start_time;

    const char* mname = (mode==MODE_EBOS_DESKTOP?"EBOS-D":(mode==MODE_EBOS_SERVER?"EBOS-S":(mode==MODE_MLFQ?"MLFQ":(mode==MODE_CFS?"CFS":"RR"))));
    printf("| %-18s | %-12s | %10.2fms | %10.4fms |\n", scenario_name, mname, total_time, max_jitter_global);
}

int main() {
    pthread_spin_init(&jitter_spin, 0);
    printf("\n=== Real-World SMP C/pthread Fair Benchmark Sweep ===\n");
    printf("| %-18s | %-12s | %-12s | %-12s |\n", "SCENARIO", "SCHEDULER", "TOTAL TIME", "MAX JITTER");
    printf("|--------------------|--------------|--------------|--------------|\n");

    run_final_benchmark("Desktop Mix", 50, MODE_EBOS_DESKTOP);
    run_final_benchmark("Desktop Mix", 50, MODE_EBOS_SERVER);
    run_final_benchmark("Desktop Mix", 50, MODE_MLFQ);
    run_final_benchmark("Desktop Mix", 50, MODE_CFS);
    run_final_benchmark("Desktop Mix", 50, MODE_RR);
    printf("|--------------------|--------------|--------------|--------------|\n");

    run_final_benchmark("Absurd Load (500)", 500, MODE_EBOS_DESKTOP);
    run_final_benchmark("Absurd Load (500)", 500, MODE_EBOS_SERVER);
    run_final_benchmark("Absurd Load (500)", 500, MODE_MLFQ);
    run_final_benchmark("Absurd Load (500)", 500, MODE_CFS);
    run_final_benchmark("Absurd Load (500)", 500, MODE_RR);
    printf("|--------------------|--------------|--------------|--------------|\n\n");
    return 0;
}
