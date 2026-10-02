/* Compile the actual callback register functions extracted from outputs.c. */
#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <limits.h>
#include <stdio.h>

#define OUTPUTS_MAX_CALLBACKS 64
#define ARRAY_SIZE(a) (sizeof(a) / sizeof((a)[0]))
static void ignored_log(const char *format, ...) { (void)format; }
#define DPRINTF(level, facility, ...) ignored_log(__VA_ARGS__)
#define E_LOG 0
#define E_DBG 0
#define L_PLAYER 0
enum output_device_state { OUTPUT_STATE_CONNECTED, OUTPUT_STATE_FAILED };
struct output_device { uint64_t id; const char *name; };
typedef void (*output_status_cb)(struct output_device *, enum output_device_state);
static const char *player_pmap(output_status_cb callback) { (void)callback; return "callback"; }
static int activations;
static void *outputs_deferredev;
static void event_active(void *event, int what, int count) {
  (void)event; (void)what; (void)count; activations++;
}

/* @ACTUAL_CALLBACK_SOURCE@ */

static void old_callback(struct output_device *device, enum output_device_state state) {
  (void)device; (void)state;
}
static void new_callback(struct output_device *device, enum output_device_state state) {
  (void)device; (void)state;
}
int main(void) {
  struct output_device first = { .id=1, .name="first" };
  struct output_device second = { .id=2, .name="second" };
  int old_id, new_id, second_id;
  size_t i;
  old_id = callback_add(&first, old_callback);
  new_id = callback_add(&first, new_callback);
  outputs_cb(old_id, first.id, OUTPUT_STATE_CONNECTED);
  /* A late old ACK must not mark the replacement callback ready. */
  for (i=0; i<ARRAY_SIZE(outputs_cb_register); i++) assert(!outputs_cb_register[i].ready);
  assert(activations==0);
  outputs_cb(new_id, second.id, OUTPUT_STATE_CONNECTED);
  assert(activations==0); /* Token must also belong to this exact device. */
  outputs_cb(new_id, first.id, OUTPUT_STATE_CONNECTED);
  assert(activations==1);
  callback_remove(&first);
  second_id = callback_add(&second, new_callback);
  outputs_cb(new_id, first.id, OUTPUT_STATE_CONNECTED);
  assert(activations==1); /* Reused array slot is not a reused token. */
  outputs_cb(second_id, second.id, OUTPUT_STATE_FAILED);
  assert(activations==2);
  outputs_cb(-1, 0, OUTPUT_STATE_FAILED);
  outputs_cb(INT_MAX, first.id, OUTPUT_STATE_CONNECTED);
  assert(activations==2);
  callback_remove(&second);
  for (i=0; i<100000; i++) {
    old_id = callback_add(&first, old_callback);
    new_id = callback_add(&first, new_callback);
    assert(old_id>=0 && new_id>old_id);
    outputs_cb(old_id, first.id, OUTPUT_STATE_CONNECTED);
    assert(activations==2);
    callback_remove(&first);
  }
  puts("Actual OwnTone callback register: stale ACK, device identity and 100000 replacements passed");
  return 0;
}
