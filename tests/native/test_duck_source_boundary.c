/* Actual source admission/ARM and actual mixer: first unrelated music prefix. */
#define _GNU_SOURCE
#include SHIRI_SPEECH_SOURCE
#include "shiri_source.h"
#include <assert.h>

static unsigned checks;
#define CHECK(v) do { assert(v); ++checks; } while (0)
enum command_state { COMMAND_END, COMMAND_PENDING };
enum { PLAY_STOPPED, PLAY_PAUSED, PLAY_PLAYING };
static struct shiri_source_state shiri_source_state;
/* @ACTUAL_SOURCE_PARAM@ */
static bool pb_timer_native, pb_timer_speech_only, pb_timer_native_waiting_for_pcm;
static bool shiri_output_end_valid;
static int player_state, shiri_source_flush_failed, shiri_source_start_failed;
static struct { int read_deficit; } pb_session;
static int arm_failure, ready_failure, deadline_failure, fresh_calls;
static unsigned seals, arms;
static uint64_t operation;
static void device_shiri_flush_cb(void) {}
static void device_shiri_cleanup_cb(void) {}
static void device_streaming_cb(void) {}
static int shiri_source_wait_begin(struct shiri_source_param *p)
{ (void)p; return deadline_failure ? -1 : 0; }
static int pb_timer_stop(void)
{ pb_timer_native = pb_timer_speech_only = false; return 0; }
static int input_pipe_shiri_seal(uint64_t op)
{ ++seals; operation = op; return 0; }
static void input_flush(void *p) { (void)p; }
static void outputs_resampling_reset(void) {}
static int outputs_shiri_flush(void (*cb)(void), int *failed)
{ (void)cb; *failed = 0; return 0; }
static void outputs_metadata_purge(void) {}
static int outputs_shiri_ready(void (*cb)(void))
{ (void)cb; return !ready_failure; }
static int input_pipe_shiri_arm(const struct shiri_pcm_owner *owner, uint64_t op)
{
  ++arms; CHECK(op == operation && shiri_source_owner_equal(owner, &shiri_source_state.last.owner));
  return arm_failure ? -1 : 0;
}
static int input_pipe_shiri_disarm(const struct shiri_pcm_owner *owner, uint64_t op)
{ (void)owner; (void)op; return 0; }
static void pb_session_stop(void) { player_state = PLAY_STOPPED; }
static int pb_timer_start(void) { pb_timer_native = true; return 0; }
static void outputs_shiri_release(void (*cb)(void)) { (void)cb; }
static int shiri_speech_bed_arm(void) { return 0; }
static void fresh_music_hook(void) { ++fresh_calls; shiri_speech_fresh_music(); }
#define shiri_speech_fresh_music fresh_music_hook
#define DPRINTF(...) ((void)0)
/* @ACTUAL_SOURCE_SEAL_FLUSH@ */
/* @ACTUAL_SOURCE_ARM@ */
#undef shiri_speech_fresh_music

static void raw16(uint8_t *p, int v)
{ unsigned n = v < 0 ? v + 65536 : v; p[0] = n; p[1] = n >> 8; }
static void reset(bool old_music)
{
  memset(&speech, 0, sizeof(speech)); speech_fd = 42;
  speech.owner_set = 1; speech.owner[0] = 1;
  speech.envelope = (300u << 16) | 600u; speech.envelope_set = 1;
  speech.gain = .28; speech.ramp_start = .28; speech.ramp_target = 1;
  speech.ramp_frames = speech.ramp_remaining = 28800;
  memset(&shiri_source_state, 0, sizeof(shiri_source_state));
  shiri_source_state.initialized = true;
  shiri_source_state.last.owner.incarnation[0] = 1;
  shiri_source_state.last.owner.session[0] = old_music ? 2 : 0;
  shiri_source_state.last.owner.epoch = old_music ? 1 : 0;
  shiri_source_state.last.owner.generation = 1;
  shiri_source_state.last.operation_generation = old_music ? 2 : 1;
  player_state = old_music ? PLAY_PAUSED : PLAY_STOPPED;
  pb_timer_native = pb_timer_speech_only = false;
  arm_failure = ready_failure = deadline_failure = fresh_calls = 0;
  seals = arms = 0;
}
static struct shiri_source_param next_music(bool old_music)
{
  struct shiri_source_param p = {0}; p.request = shiri_source_state.last;
  ++p.request.operation_generation;
  if (old_music) ++p.request.owner.generation;
  else { p.request.owner.session[0] = 2; ++p.request.owner.epoch; }
  return p;
}
static void admit(struct shiri_source_param *p, int *result)
{
  CHECK(shiri_source_seal_flush(p, result) == COMMAND_END && *result == 0);
  CHECK(speech.gain == .28); /* Not before the exact successful ARM. */
  CHECK(shiri_source_arm(p, result) == COMMAND_END);
}
static void idle_terminal_first_prefix_is_exact(bool cancel)
{
  reset(false);
  if (cancel) CHECK(speech_cancel(&speech, speech.owner) == 0);
  else CHECK(speech_finish(&speech, speech.owner) == 0);
  struct shiri_source_param p = next_music(false); int result;
  uint8_t prefix[480 * 4], original[sizeof(prefix)];
  for (unsigned i = 0; i < 480; ++i) {
    raw16(prefix + 4 * i, (int)(i % 31) * 1800 - 27000);
    raw16(prefix + 4 * i + 2, 27000 - (int)(i % 31) * 1800);
  }
  memcpy(original, prefix, sizeof(prefix));
  admit(&p, &result);
  CHECK(result == 0 && fresh_calls == 1 && p.fresh_music_gain && speech.gain == 1);
  CHECK(speech.ramp_remaining == 0 && !shiri_source_state.faulted);
  speech_mix(&speech, prefix, sizeof(prefix), 480, 48000, 16, 2, UINT64_C(10000000000));
  CHECK(!memcmp(original, prefix, sizeof(prefix))); /* Both channels, including frame0. */
  CHECK(shiri_source_state.last.operation_generation == p.request.operation_generation);
  CHECK(shiri_source_owner_equal(&shiri_source_state.last.owner, &p.request.owner));
}
static void ongoing_and_queued_voice_keep_gain(void)
{
  for (unsigned kind = 0; kind < 4; ++kind) {
    reset(false);
    speech.accepting = kind == 0; speech.active = kind == 1;
    speech.running = kind == 2; speech.count = kind == 3;
    if (speech.count) { speech.audible[0] = 1; speech.gains[0] = 18350; speech.expires[0] = UINT64_MAX; }
    struct speech_state before = speech;
    struct shiri_source_param p = next_music(false); int result;
    admit(&p, &result);
    CHECK(result == 0 && fresh_calls == 1 && !memcmp(&before, &speech, sizeof(speech)));
  }
}
static void pause_flush_failures_and_replay_keep_gain(void)
{
  reset(true); struct shiri_source_param p = next_music(true); int result;
  struct speech_state before = speech;
  admit(&p, &result);
  CHECK(result == 0 && !p.fresh_music_gain && fresh_calls == 0);
  CHECK(!memcmp(&before, &speech, sizeof(speech)) && player_state == PLAY_PAUSED);
  p.request = shiri_source_state.last;
  CHECK(shiri_source_seal_flush(&p, &result) == COMMAND_END && result == SHIRI_SOURCE_REPLAY);
  CHECK(fresh_calls == 0 && !memcmp(&before, &speech, sizeof(speech)));
  for (unsigned kind = 0; kind < 3; ++kind) {
    reset(false); p = next_music(false); before = speech;
    CHECK(shiri_source_seal_flush(&p, &result) == COMMAND_END && result == 0);
    if (kind == 0) arm_failure = 1;
    if (kind == 1) ready_failure = 1;
    if (kind == 2) ++p.request.operation_generation;
    CHECK(shiri_source_arm(&p, &result) == COMMAND_END && result < 0);
    CHECK(fresh_calls == 0 && !memcmp(&before, &speech, sizeof(speech)));
  }
  reset(false); p = next_music(false); before = speech; deadline_failure = 1;
  CHECK(shiri_source_seal_flush(&p, &result) == COMMAND_END && result < 0);
  CHECK(fresh_calls == 0 && !memcmp(&before, &speech, sizeof(speech)));
}
int main(void)
{
  idle_terminal_first_prefix_is_exact(false); idle_terminal_first_prefix_is_exact(true);
  ongoing_and_queued_voice_keep_gain(); pause_flush_failures_and_replay_keep_gain();
  printf("duck1 source: %u actual-source/media checks; exact fresh music prefix, terminal-only reset, ongoing voice and paused-owner continuity\n", checks);
  return 0;
}
