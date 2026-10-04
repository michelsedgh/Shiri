/* Actual admitted-owner CONTROL/PCM/gain code; no socket or audio device. */
#define _GNU_SOURCE
#include SHIRI_SPEECH_SOURCE
#include <assert.h>

static unsigned checks;
#define CHECK(v) do { assert(v); ++checks; } while (0)
static const uint64_t now0 = UINT64_C(10000000000);
static const uint8_t owner_a[16] = {1}, owner_b[16] = {2};
static const uint32_t text_envelope = (300u << 16) | 600u;
static const uint32_t duck = 18350u;

static void put32(uint8_t *p, uint32_t v)
{ p[0] = v >> 24; p[1] = v >> 16; p[2] = v >> 8; p[3] = v; }
static void put64(uint8_t *p, uint64_t v)
{ put32(p, v >> 32); put32(p + 4, v); }
static int read16(const uint8_t *p)
{ unsigned v = p[0] | ((unsigned)p[1] << 8); return v & 32768u ? (int)v - 65536 : (int)v; }
static void raw16(uint8_t *p, int v)
{ unsigned n = v < 0 ? v + 65536 : v; p[0] = n; p[1] = n >> 8; }
static struct speech_state fresh(void)
{
  struct speech_state s;
  memset(&s, 0, sizeof(s)); s.gain = 1;
  CHECK(speech_hex("b6786543-7eb2-443d-83b1-65b984123a76", s.room, 1) == 0);
  CHECK(speech_hex("123456789abcdef0123456789abcdef0", s.launch, 0) == 0);
  CHECK(speech_begin(&s, owner_a, now0) == 0);
  return s;
}
static uint64_t at(unsigned frame)
{ return now0 + (uint64_t)frame * UINT64_C(1000000000) / SPEECH_RATE; }
static size_t packet(uint8_t *p, const struct speech_state *s, const uint8_t id[16],
                     uint64_t sequence, unsigned frames, int sample, unsigned active,
                     uint32_t gain, uint32_t envelope, uint64_t now)
{
  memset(p, 0, SPEECH_HEADER + 2 * SPEECH_FRAMES);
  memcpy(p, "SHRITTS1", 8); p[8] = 2; p[9] = frames ? 1 : 2; p[11] = SPEECH_HEADER;
  put32(p + 12, frames * 2); put64(p + 16, sequence);
  memcpy(p + 24, s->room, 16); memcpy(p + 40, s->launch, 16); memcpy(p + 80, id, 16);
  put64(p + 56, now); put32(p + 64, frames); put32(p + 68, gain);
  put32(p + 72, active); put32(p + 76, envelope);
  for (unsigned i = 0; i < frames; ++i) raw16(p + SPEECH_HEADER + 2 * i, sample);
  return SPEECH_HEADER + 2 * frames;
}
static void control(struct speech_state *s, unsigned active, uint32_t gain,
                    uint32_t envelope, unsigned frame)
{
  uint8_t p[SPEECH_HEADER + 2 * SPEECH_FRAMES];
  size_t n = packet(p, s, s->owner, s->sequence + 1, 0, 0, active, gain, envelope, at(frame));
  CHECK(speech_receive(s, p, n, at(frame)) == 0);
}
static void refused(struct speech_state *s, uint8_t *p, size_t n, uint64_t now)
{
  struct speech_state before = *s;
  CHECK(speech_receive(s, p, n, now) < 0);
  before.refused = s->refused;
  CHECK(memcmp(&before, s, sizeof(before)) == 0);
}
static int mix_one(struct speech_state *s, unsigned frame, int carrier)
{
  uint8_t pcm[4]; raw16(pcm, carrier); raw16(pcm + 2, carrier);
  speech_mix(s, pcm, sizeof(pcm), 1, SPEECH_RATE, 16, 2, at(frame));
  CHECK(read16(pcm) == read16(pcm + 2));
  return read16(pcm);
}
static void ramp(struct speech_state *s, unsigned first, unsigned frames,
                 double start, double target, int refresh)
{
  int previous = (int)lround(10000 * start);
  for (unsigned i = 0; i < frames; ++i) {
    if (refresh && i % 480 == 0) control(s, 1, (uint32_t)lround(target * SPEECH_FULL_GAIN), s->envelope, first + i);
    int value = mix_one(s, first + i, 10000);
    double expected = start + (target - start) * (double)(i + 1) / frames;
    CHECK(fabs(s->gain - expected) < 1e-12);
    CHECK(value == (int)lround(10000 * expected));
    CHECK(target < start ? value <= previous : value >= previous);
    previous = value;
    if (i + 1 < frames) CHECK(s->gain != target);
  }
  CHECK(s->gain == target && s->ramp_remaining == 0);
}

static void full_target_durations_and_no_pcm_wait(void)
{
  struct speech_state s = fresh();
  CHECK(mix_one(&s, 0, 10000) == 10000); /* BEGIN alone never ducks. */
  control(&s, 1, duck, text_envelope, 1);
  CHECK(s.active && !s.count && !s.running && !s.first_admission_ns);
  CHECK(s.gain == 1 && s.envelope_set);
  ramp(&s, 1, 14400, 1, (double)duck / SPEECH_FULL_GAIN, 1);
  CHECK(!s.pcm_packets && !s.admitted_frames && !s.mixed_frames && !s.priming_events);
  control(&s, 0, duck, text_envelope, 14401);
  ramp(&s, 14401, 28800, (double)duck / SPEECH_FULL_GAIN, 1, 0);
  /* Small target changes still take the complete 300/600 ms durations. */
  control(&s, 1, 58982, text_envelope, 43201);
  ramp(&s, 43201, 14400, 1, 58982.0 / SPEECH_FULL_GAIN, 1);
  CHECK(speech_finish(&s, owner_a) == 0);
  ramp(&s, 57601, 28800, 58982.0 / SPEECH_FULL_GAIN, 1, 0);
}

static void bounded_validation_and_immutable_policy(void)
{
  static const uint32_t bad[] = {1, 40u << 16, (39u << 16) | 40u,
    (40u << 16) | 39u, (2001u << 16) | 40u, (40u << 16) | 5001u, UINT32_MAX};
  uint8_t p[SPEECH_HEADER + 2 * SPEECH_FRAMES];
  struct speech_state s = fresh(); size_t n;
  for (unsigned i = 0; i < sizeof(bad) / sizeof(bad[0]); ++i) {
    n = packet(p, &s, owner_a, 1, 0, 0, 1, duck, bad[i], now0);
    refused(&s, p, n, now0);
    CHECK(!s.envelope_set && s.gain == 1);
  }
  n = packet(p, &s, owner_a, 1, 1, 100, 1, duck, text_envelope, now0);
  put32(p + 64, 0); refused(&s, p, n, now0); /* invalid PCM cannot lock policy */
  control(&s, 1, duck, text_envelope, 0);
  for (unsigned i = 0; i < 20; ++i) mix_one(&s, i, 10000);
  n = packet(p, &s, owner_a, s.sequence + 1, 0, 0, 0, duck, 0, at(20));
  refused(&s, p, n, at(20));
  n = packet(p, &s, owner_a, s.sequence + 1, 1, 100, 1, duck, (300u << 16) | 601u, at(20));
  refused(&s, p, n, at(20));
  n = packet(p, &s, owner_a, s.sequence + 1, 0, 0, 0, duck, text_envelope, at(20));
  CHECK(speech_receive(&s, p, n, at(20)) == 0);
  for (unsigned attack = 40; attack <= 2000; attack += 1960) {
    s = fresh(); control(&s, 1, 0, (attack << 16) | 5000u, 0);
    ramp(&s, 0, attack * 48, 1, 0, 1);
    CHECK(speech_cancel(&s, owner_a) == 0);
    ramp(&s, attack * 48, 5000u * 48, 0, 1, 0);
  }
}

static void cancel_expiry_and_retarget_are_continuous(void)
{
  struct speech_state s = fresh();
  control(&s, 1, duck, text_envelope, 0);
  for (unsigned i = 0; i < 4800; ++i) mix_one(&s, i, 10000);
  double middle = s.gain;
  CHECK(middle > (double)duck / SPEECH_FULL_GAIN && middle < 1);
  CHECK(speech_cancel(&s, owner_a) == 0 && s.gain == middle && s.envelope == text_envelope);
  ramp(&s, 4800, 28800, middle, 1, 0);
  CHECK(speech_begin(&s, owner_b, at(33600)) == 0);
  control(&s, 1, 0, text_envelope, 33600);
  for (unsigned i = 0; i < 4800; ++i) mix_one(&s, 33600 + i, 10000);
  middle = s.gain;
  control(&s, 1, 32768, text_envelope, 38400); /* 2/3 -> 1/2 is a full attack */
  ramp(&s, 38400, 14400, middle, .5, 1);
  control(&s, 1, 58982, text_envelope, 52800); /* .5 -> .9 uses full release */
  ramp(&s, 52800, 28800, .5, 58982.0 / SPEECH_FULL_GAIN, 1);
  /* Lost producer renewal restores from its actual partial gain at expiry. */
  s = fresh(); control(&s, 1, 0, text_envelope, 0);
  for (unsigned i = 0; i < 9600; ++i) mix_one(&s, i, 10000);
  middle = s.gain; CHECK(s.active && middle > 0);
  speech_expire(&s, now0 + SPEECH_AGE_NS);
  CHECK(!s.active && s.gain == middle && s.envelope == text_envelope);
  ramp(&s, 12000, 28800, middle, 1, 0);
}

static void successor_and_natural_tail(void)
{
  uint8_t p[SPEECH_HEADER + 2 * SPEECH_FRAMES];
  struct speech_state s = fresh(); size_t n;
  control(&s, 1, duck, text_envelope, 0);
  for (unsigned i = 0; i < 4800; ++i) mix_one(&s, i, 10000);
  CHECK(speech_cancel(&s, owner_a) == 0);
  for (unsigned i = 0; i < 4800; ++i) mix_one(&s, 4800 + i, 10000);
  double before = s.gain; uint32_t remaining = s.ramp_remaining;
  CHECK(speech_begin(&s, owner_b, at(9600)) == 0);
  CHECK(s.gain == before && s.envelope == text_envelope && s.ramp_remaining == remaining && !s.envelope_set);
  n = packet(p, &s, owner_a, s.sequence + 100, 0, 0, 1, 0, (40u << 16) | 40u, at(9600));
  refused(&s, p, n, at(9600)); CHECK(!s.envelope_set);
  control(&s, 1, 32768, (200u << 16) | 800u, 9600);
  CHECK(s.gain == before && s.envelope == ((200u << 16) | 800u));
  ramp(&s, 9600, 9600, before, .5, 1);
  n = packet(p, &s, owner_b, s.sequence + 1, 1, 123, 1, 32768, s.envelope, at(19200));
  CHECK(speech_receive(&s, p, n, at(19200)) == 0);
  CHECK(speech_finish(&s, owner_b) == 0 && s.count == 1 && !s.accepting && !s.active);
  /* The final queued voice retains its duck policy and arrives without a
   * fade-completion wait. The next music sample begins smooth restoration. */
  CHECK(mix_one(&s, 20160, 10000) == 5123 && !s.count && s.gain == .5);
  ramp(&s, 20161, 38400, .5, 1, 0);
  CHECK(s.mixed_frames == 1 && s.admitted_frames == 1);
}

static void quiet_pcm_preserves_explicit_early_control(void)
{
  uint8_t p[SPEECH_HEADER + 2 * SPEECH_FRAMES];
  struct speech_state s = fresh();
  control(&s, 1, duck, text_envelope, 0);
  size_t n = packet(p, &s, owner_a, s.sequence + 1, 1, 0, 0, 0, text_envelope, at(1));
  CHECK(speech_receive(&s, p, n, at(1)) == 0 && s.active && s.duck == duck);
  CHECK(mix_one(&s, 961, 10000) < 10000 && !s.count);
  CHECK(speech_cancel(&s, owner_a) == 0);
  double start = s.gain; ramp(&s, 962, 28800, start, 1, 0);
  s = fresh();
  n = packet(p, &s, owner_a, 1, 1, 0, 0, 0, text_envelope, now0);
  CHECK(speech_receive(&s, p, n, now0) == 0 && !s.active && s.envelope_set && s.envelope == text_envelope);
  CHECK(mix_one(&s, 960, 10000) == 10000 && s.gain == 1);
}

static void inactive_lock_first_pcm_and_same_policy_successor(void)
{
  uint8_t p[SPEECH_HEADER + 2 * SPEECH_FRAMES];
  struct speech_state s = fresh();
  control(&s, 0, duck, text_envelope, 0);
  CHECK(s.envelope_set && !s.active && s.gain == 1);
  size_t n = packet(p, &s, owner_a, s.sequence + 1, 1, 123, 1, duck, text_envelope, at(1));
  CHECK(speech_receive(&s, p, n, at(1)) == 0 && s.active);
  for (unsigned i = 1; i < 961; ++i) mix_one(&s, i, 10000);
  CHECK(s.mixed_frames == 0 && s.ramp_remaining > 0);
  int first_voice = mix_one(&s, 961, 10000);
  CHECK(first_voice == (int)lround(10000 * s.gain) + 123);
  CHECK(s.mixed_frames == 1 && s.ramp_remaining > 0 && s.gain > (double)duck / SPEECH_FULL_GAIN);
  CHECK(speech_cancel(&s, owner_a) == 0);
  for (unsigned i = 0; i < 4800; ++i) mix_one(&s, 962 + i, 10000);
  double current = s.gain; uint32_t remaining = s.ramp_remaining;
  CHECK(speech_begin(&s, owner_b, at(5762)) == 0);
  CHECK(s.gain == current && s.ramp_remaining == remaining && s.envelope == text_envelope);
  control(&s, 0, duck, text_envelope, 5762);
  CHECK(s.gain == current && s.ramp_remaining == remaining && s.envelope_set);
  /* Matching successor policy must not restart an already-running release. */
  CHECK(mix_one(&s, 5762, 10000) >= (int)lround(current * 10000));
  CHECK(s.ramp_remaining == remaining - 1);
}

int main(void)
{
  full_target_durations_and_no_pcm_wait(); bounded_validation_and_immutable_policy();
  cancel_expiry_and_retarget_are_continuous(); successor_and_natural_tail();
  quiet_pcm_preserves_explicit_early_control();
  inactive_lock_first_pcm_and_same_policy_successor();
  printf("duck1: %u actual-media checks; early owned CONTROL, full-target duration, legacy isolation, terminal restoration\n", checks);
  return 0;
}
