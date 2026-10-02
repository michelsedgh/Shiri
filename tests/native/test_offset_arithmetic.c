/* Actual OwnTone arithmetic seams; no network, clock, audio or library I/O. */
#include <assert.h>
#include <inttypes.h>
#include <limits.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

static unsigned logs;
#define E_LOG 0
#define E_WARN 1
#define L_PLAYER 0
#define L_FIFO 0
#define L_CAST 0
#define L_LAUDIO 0
#define CAST_OFFSET_MAX 1000
#define CAST_DEVICE_START_DELAY_MS 100
#define DPRINTF(...) (++logs)
struct output_device { int offset_ms; const char *name; };
static uint64_t configured_delay;
static uint64_t outputs_buffer_duration_ms_get(void) { return configured_delay; }

/* @ACTUAL_DELAY_FUNCTIONS@ */
/* @ACTUAL_CONFIG_FUNCTIONS@ */

static long signed_buffer;
static uint64_t outputs_buffer_duration_ms;
typedef struct { struct { long number; } def; } cfg_opt_t;
static cfg_opt_t option = { { 2250 } };
static void *cfg;
static void *cfg_getsec(void *ignored, const char *name) { (void)ignored; (void)name; return NULL; }
static cfg_opt_t *cfg_getopt(void *ignored, const char *name) { (void)ignored; (void)name; return &option; }
static long cfg_opt_getnint(cfg_opt_t *ignored, unsigned index) { (void)ignored; (void)index; return signed_buffer; }
static int configure_buffer(void)
{
  cfg_opt_t *start_buffer_opt;
  /* @ACTUAL_BUFFER_CONFIG@ */
  return 0;
}

static unsigned delay_cases(uint64_t (*calculate)(int), unsigned extra)
{
  const unsigned bases[] = { 0, 1, 999, 1000, 1999, 2000, 2250, 4000, 60000 };
  const int extremes[] = { INT_MIN, INT_MIN + 1, -2001, -2000, -1001, -1000, -1, 0, 1, 1000, 2000, INT_MAX };
  unsigned i, j, count = 0;
  for (i = 0; i < sizeof(bases) / sizeof(bases[0]); i++)
    {
      uint64_t base = bases[i] + extra;
      configured_delay = bases[i];
      for (j = 0; j < sizeof(extremes) / sizeof(extremes[0]); j++)
        {
          int offset = extremes[j];
          int valid = offset >= 0 || base >= (uint64_t)(-(int64_t)offset);
          uint64_t expected = valid ? (uint64_t)((int64_t)base + offset) : base;
          logs = 0;
          assert(calculate(offset) == expected);
          assert(logs == (unsigned)!valid);
          count++;
        }
      /* Every supported HTTP offset, rather than only hand-picked bounds. */
      for (int offset = -2000; offset <= 2000; offset++)
        {
          int valid = offset >= 0 || base >= (uint64_t)(-(int64_t)offset);
          uint64_t expected = valid ? (uint64_t)((int64_t)base + offset) : base;
          logs = 0;
          assert(calculate(offset) == expected);
          assert(logs == (unsigned)!valid);
          count++;
        }
    }
  return count;
}

static unsigned config_cases(int (*configure)(int))
{
  unsigned count = 0;
  for (int value = -2001; value <= 2001; value++)
    {
      logs = 0;
      assert(configure(value) == (value < -1000 || value > 1000 ? 23 : value));
      assert(logs == (unsigned)(value < -1000 || value > 1000));
      count++;
    }
  const int bounds[] = { INT_MIN, INT_MIN + 1, INT_MAX - 1, INT_MAX };
  for (unsigned i = 0; i < sizeof(bounds) / sizeof(bounds[0]); i++)
    {
      logs = 0;
      assert(configure(bounds[i]) == 23);
      assert(logs == 1);
      count++;
    }
  return count;
}

int main(void)
{
#ifdef TEST_PREIMAGE
  configured_delay = 1000;
  assert(fifo_delay(-2000) == UINT64_MAX - 999);
  assert(cast_delay(-2000) == UINT64_MAX - 899);
  assert(pulse_delay(-2000) == UINT64_MAX - 999);
  signed_buffer = -1;
  assert(configure_buffer() == 0);
  assert(outputs_buffer_duration_ms == UINT64_MAX);
  puts("Actual preimage: three negative-offset delay wraps and negative configuration unsigned conversion reproduced");
#else
  unsigned count = delay_cases(fifo_delay, 0) + delay_cases(cast_delay, 100) + delay_cases(pulse_delay, 0);
  count += config_cases(alsa_config) + config_cases(cast_config) + config_cases(pulse_config);
  const long invalid[] = { LONG_MIN, -2000, -1 };
  for (unsigned i = 0; i < sizeof(invalid) / sizeof(invalid[0]); i++)
    {
      signed_buffer = invalid[i];
      outputs_buffer_duration_ms = 2250;
      logs = 0;
      assert(configure_buffer() == -1);
      assert(outputs_buffer_duration_ms == 2250 && logs == 1);
      count++;
    }
  const long valid[] = { 0, 1, 250, 1000, 2000, 2250, 4000, 60000 };
  for (unsigned i = 0; i < sizeof(valid) / sizeof(valid[0]); i++)
    {
      signed_buffer = valid[i];
      assert(configure_buffer() == 0);
      assert(outputs_buffer_duration_ms == (uint64_t)valid[i]);
      count++;
    }
  printf("Actual offset/configuration seams: %u cases; all supported offsets, zero boundary, invalid negative delay, INT_MIN, and signed buffer validation\n", count);
#endif
  return 0;
}
