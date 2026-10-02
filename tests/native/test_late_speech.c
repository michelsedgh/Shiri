/* Compile with SHIRI_SPEECH_SOURCE naming the exact maintained source. */
#include SHIRI_SPEECH_SOURCE
#include <assert.h>

static unsigned checks;
#define CHECK(condition) do { assert(condition); ++checks; } while (0)
static const uint64_t origin = UINT64_C(10000000000);

static void put32(uint8_t *p, uint32_t n)
{
  p[0] = (uint8_t)(n >> 24); p[1] = (uint8_t)(n >> 16);
  p[2] = (uint8_t)(n >> 8); p[3] = (uint8_t)n;
}
static void put64(uint8_t *p, uint64_t n)
{
  put32(p, (uint32_t)(n >> 32)); put32(p + 4, (uint32_t)n);
}
static struct speech_state fresh(void)
{
  struct speech_state s;
  memset(&s, 0, sizeof(s)); s.gain = 1.0;
  CHECK(speech_hex("b6786543-7eb2-443d-83b1-65b984123a76", s.room, 1) == 0);
  CHECK(speech_hex("123456789abcdef0123456789abcdef0", s.launch, 0) == 0);
  return s;
}
static size_t packet(uint8_t *p, const struct speech_state *s, unsigned op,
                     uint64_t seq, unsigned frames, unsigned duck, unsigned active)
{
  unsigned i;
  memset(p, 0, SPEECH_HEADER + 2 * SPEECH_FRAMES);
  memcpy(p, "SHRITTS1", 8); p[8] = 1; p[9] = (uint8_t)op; p[11] = SPEECH_HEADER;
  put32(p + 12, frames * 2); put64(p + 16, seq);
  memcpy(p + 24, s->room, 16); memcpy(p + 40, s->launch, 16);
  put64(p + 56, origin); put32(p + 64, frames); put32(p + 68, duck); put32(p + 72, active);
  for (i = 0; i < frames && i < SPEECH_FRAMES; ++i) {
    p[SPEECH_HEADER + 2 * i] = 100; p[SPEECH_HEADER + 2 * i + 1] = 0;
  }
  return SPEECH_HEADER + frames * 2;
}
static void raw16(uint8_t *p, int value)
{
  unsigned n = (unsigned)(value < 0 ? value + 65536 : value);
  p[0] = (uint8_t)n; p[1] = (uint8_t)(n >> 8);
}
static int read16(const uint8_t *p)
{
  unsigned n = p[0] | ((unsigned)p[1] << 8);
  return n & 32768u ? (int)n - 65536 : (int)n;
}
static void music(uint8_t *p, unsigned frames, int value)
{
  unsigned i;
  for (i = 0; i < frames * 2; ++i) raw16(p + 2 * i, value);
}
static void refused_without_effect(struct speech_state *s, const uint8_t *p, size_t n, uint64_t now)
{
  struct speech_state before = *s;
  CHECK(speech_receive(s, p, n, now) < 0);
  before.refused = s->refused;
  CHECK(memcmp(&before, s, sizeof(before)) == 0);
}

static void malformed(void)
{
  uint8_t p[SPEECH_HEADER + 2 * SPEECH_FRAMES], good[sizeof(p)];
  struct speech_state s = fresh(); size_t n; unsigned i;
  static const unsigned fields[] = { 0, 8, 9, 10, 11, 12, 24, 40, 72, 76 };
  n = packet(good, &s, 1, 1, 960, 65536, 1);
  for (i = 0; i < sizeof(fields) / sizeof(fields[0]); ++i) {
    memcpy(p, good, sizeof(p)); p[fields[i]] ^= 4;
    refused_without_effect(&s, p, n, origin);
  }
  refused_without_effect(&s, good, 0, origin);
  refused_without_effect(&s, good, SPEECH_HEADER - 1, origin);
  refused_without_effect(&s, good, n - 1, origin);
  refused_without_effect(&s, good, n + 1, origin);
  refused_without_effect(&s, good, n, 0);
  refused_without_effect(&s, good, n, UINT64_MAX);
  memcpy(p, good, sizeof(p)); put64(p + 16, 0); refused_without_effect(&s, p, n, origin);
  put64(p + 16, UINT64_MAX); refused_without_effect(&s, p, n, origin);
  memcpy(p, good, sizeof(p)); put32(p + 64, 0); refused_without_effect(&s, p, n, origin);
  put32(p + 64, UINT32_MAX); refused_without_effect(&s, p, n, origin);
  memcpy(p, good, sizeof(p)); put32(p + 68, 65537); refused_without_effect(&s, p, n, origin);
  memcpy(p, good, sizeof(p)); put32(p + 72, 2); refused_without_effect(&s, p, n, origin);
  memcpy(p, good, sizeof(p)); put64(p + 56, 0); refused_without_effect(&s, p, n, origin);
  put64(p + 56, UINT64_MAX); refused_without_effect(&s, p, n, origin);
  put64(p + 56, origin - SPEECH_AGE_NS); refused_without_effect(&s, p, n, origin);
  put64(p + 56, origin + SPEECH_FUTURE_NS + 1); refused_without_effect(&s, p, n, origin);
  CHECK(speech_receive(&s, good, n, origin) == 0);
  refused_without_effect(&s, good, n, origin); /* duplicate */
  memcpy(p, good, sizeof(p)); put64(p + 16, 3);
  CHECK(speech_receive(&s, p, n, origin) == 0); /* loss is not retimed or replayed */
  put64(p + 16, 2); refused_without_effect(&s, p, n, origin);
  n = packet(p, &s, 2, 4, 1, 65536, 1); refused_without_effect(&s, p, n, origin);
  n = packet(p, &s, 2, 4, 0, 65536, 1); CHECK(speech_receive(&s, p, n, origin) == 0);
  CHECK(s.sequence == 4 && s.count == 1920);
}

static void boundaries(void)
{
  uint8_t p[SPEECH_HEADER + 2 * SPEECH_FRAMES]; struct speech_state s = fresh(); size_t n;
  n = packet(p, &s, 1, 1, 1, 65536, 1);
  put64(p + 56, origin - SPEECH_AGE_NS + 1);
  CHECK(speech_receive(&s, p, n, origin) == 0);
  CHECK(s.expires[0] == origin + 1);
  n = packet(p, &s, 1, 2, 1, 65536, 1); put64(p + 56, origin + SPEECH_FUTURE_NS);
  CHECK(speech_receive(&s, p, n, origin) == 0);
  CHECK(s.expires[1] == origin + SPEECH_AGE_NS); /* never retain future data >250ms */
  CHECK(speech_hex("00000000000000000000000000000000", s.launch, 0) < 0);
  CHECK(speech_hex("123456789ABCDEF0123456789abcdef0", s.launch, 0) < 0);
  CHECK(speech_hex("b67865437eb2443d83b165b984123a76", s.room, 1) < 0);
  CHECK(speech_hex("b6786543_7eb2-443d-83b1-65b984123a76", s.room, 1) < 0);
}

static void queue_bounds(void)
{
  uint8_t p[SPEECH_HEADER + 2 * SPEECH_FRAMES], pcm[3840];
  struct speech_state s = fresh(); unsigned i; size_t n;
  for (i = 0; i < 12; ++i) {
    n = packet(p, &s, 1, i + 1, 960, 65536, 1);
    CHECK(speech_receive(&s, p, n, origin) == 0);
  }
  n = packet(p, &s, 1, 13, 480, 65536, 1); CHECK(speech_receive(&s, p, n, origin) == 0);
  CHECK(s.count == 12000);
  n = packet(p, &s, 1, 14, 1, 0, 1); refused_without_effect(&s, p, n, origin);
  music(pcm, 960, 0); speech_mix(&s, pcm, sizeof(pcm), 960, 48000, 16, 2, origin);
  CHECK(s.count == 11040 && s.head == 960);
  n = packet(p, &s, 1, 14, 960, 65536, 1); CHECK(speech_receive(&s, p, n, origin) == 0);
  CHECK(s.count == 12000); /* ring wrap */
  for (i = 0; i < 12; ++i) {
    music(pcm, 960, 0); speech_mix(&s, pcm, sizeof(pcm), 960, 48000, 16, 2, origin);
    for (unsigned j = 0; j < 1920; ++j) CHECK(read16(pcm + 2 * j) == 100);
  }
  CHECK(s.count == 480);
  speech_expire(&s, origin + SPEECH_AGE_NS); CHECK(!s.count && !s.active && s.expired == 480);
}

static void pcm_and_gain(void)
{
  uint8_t p[SPEECH_HEADER + 2 * SPEECH_FRAMES], guarded[3842], *pcm = guarded + 1, copy[3840];
  struct speech_state s = fresh(); size_t n; unsigned i;
  guarded[0] = 19; guarded[3841] = 23;
  n = packet(p, &s, 1, 1, 960, 65536, 1);
  for (i = 0; i < 960; ++i) raw16(p + SPEECH_HEADER + 2 * i, i % 2 ? -32768 : 32767);
  CHECK(speech_receive(&s, p, n, origin) == 0);
  music(pcm, 960, 1000); speech_mix(&s, pcm, 3840, 960, 48000, 16, 2, origin);
  for (i = 0; i < 960; ++i) {
    CHECK(read16(pcm + 4 * i) == (i % 2 ? -31768 : 32767));
    CHECK(read16(pcm + 4 * i + 2) == read16(pcm + 4 * i));
  }
  CHECK(!s.count && guarded[0] == 19 && guarded[3841] == 23);
  music(pcm, 960, -1000); memcpy(copy, pcm, 3840);
  speech_mix(&s, pcm, 3840, 960, 44100, 16, 2, origin); CHECK(!memcmp(copy, pcm, 3840));
  speech_mix(&s, pcm, 3840, 960, 48000, 32, 2, origin); CHECK(!memcmp(copy, pcm, 3840));
  speech_mix(&s, pcm, 3840, 960, 48000, 16, 1, origin); CHECK(!memcmp(copy, pcm, 3840));
  speech_mix(&s, pcm, 3839, 960, 48000, 16, 2, origin); CHECK(!memcmp(copy, pcm, 3840));
  speech_mix(&s, pcm, 3840, INT_MAX, 48000, 16, 2, origin); CHECK(!memcmp(copy, pcm, 3840));
  n = packet(p, &s, 2, 2, 0, 13107, 1); CHECK(speech_receive(&s, p, n, origin) == 0);
  for (i = 0; i < 2; ++i) {
    music(pcm, 960, 10000); speech_mix(&s, pcm, 3840, 960, 48000, 16, 2, origin);
  }
  CHECK(fabs(s.gain - 13107.0 / 65536) < 1e-10);
  CHECK(read16(pcm + 3838) == 2000);
  n = packet(p, &s, 2, 3, 0, 13107, 0); CHECK(speech_receive(&s, p, n, origin) == 0);
  for (i = 0; i < 13; ++i) {
    music(pcm, 960, 10000); speech_mix(&s, pcm, 3840, 960, 48000, 16, 2, origin);
  }
  CHECK(s.gain == 1 && read16(pcm + 3838) == 10000);
  n = packet(p, &s, 1, 4, 960, 0, 1); CHECK(speech_receive(&s, p, n, origin) == 0);
  music(pcm, 960, 10000); speech_mix(&s, pcm, 3840, 960, 48000, 16, 2, origin + SPEECH_AGE_NS);
  CHECK(s.count == 0 && !s.active && read16(pcm + 3838) == 10000);
  n = packet(p, &s, 1, 5, 960, 65536, 1); CHECK(speech_receive(&s, p, n, origin) == 0);
  music(pcm, 960, 10000); speech_mix(&s, pcm, 3840, 960, 48000, 16, 2, 0);
  CHECK(!s.count && !s.active && read16(pcm) == 10000); /* clock failure cannot emit queued voice */
}

static void eof_drains_and_restores_after_last_sample(void)
{
  uint8_t p[SPEECH_HEADER + 2 * SPEECH_FRAMES], pcm[3840];
  struct speech_state s = fresh(); size_t n; unsigned i;
  n = packet(p, &s, 1, 1, 120, 32768, 1);
  CHECK(speech_receive(&s, p, n, origin) == 0);
  n = packet(p, &s, 2, 2, 0, 32768, 0);
  CHECK(speech_receive(&s, p, n, origin + 10000000) == 0);
  CHECK(!s.active && s.count == 120);
  music(pcm, 960, 0);
  speech_mix(&s, pcm, sizeof(pcm), 960, 48000, 16, 2, origin + 100000000);
  for (i = 0; i < 960; ++i) {
    CHECK(read16(pcm + 4 * i) == (i < 120 ? 100 : 0));
    CHECK(read16(pcm + 4 * i + 2) == (i < 120 ? 100 : 0));
  }
  CHECK(!s.count && s.gain == 1.0);
  /* A frame inside one block consumes the last queued voice, then the next
   * frame begins restoration. The lease cannot keep ducking after EOF. */
  s.gain = 0.5;
  n = packet(p, &s, 1, 3, 1, 32768, 1); CHECK(speech_receive(&s, p, n, origin) == 0);
  n = packet(p, &s, 2, 4, 0, 32768, 0); CHECK(speech_receive(&s, p, n, origin) == 0);
  music(pcm, 960, 12000); speech_mix(&s, pcm, sizeof(pcm), 960, 48000, 16, 2, origin);
  CHECK(read16(pcm) == 6100);
  CHECK(read16(pcm + 4) == 6001);
  CHECK(read16(pcm + 3838) == 6959);
}

static void quiet_packets_never_duck_music(void)
{
  uint8_t p[SPEECH_HEADER + 2 * SPEECH_FRAMES], pcm[3840];
  struct speech_state s = fresh(); size_t n; unsigned i;
  n = packet(p, &s, 1, 1, 960, 0, 0);
  memset(p + SPEECH_HEADER, 0, 1920);
  CHECK(speech_receive(&s, p, n, origin) == 0);
  music(pcm, 960, 10000); speech_mix(&s, pcm, sizeof(pcm), 960, 48000, 16, 2, origin);
  CHECK(s.gain == 1.0 && !s.active && !s.count);
  for (i = 0; i < 1920; ++i) CHECK(read16(pcm + 2 * i) == 10000);
  n = packet(p, &s, 1, 2, 960, 0, 0);
  for (i = 0; i < 960; ++i) raw16(p + SPEECH_HEADER + 2 * i, -5);
  CHECK(speech_receive(&s, p, n, origin) == 0);
  music(pcm, 960, 10000); speech_mix(&s, pcm, sizeof(pcm), 960, 48000, 16, 2, origin);
  CHECK(s.gain == 1.0 && !s.active && !s.count);
  for (i = 0; i < 1920; ++i) CHECK(read16(pcm + 2 * i) == 9995);
}

static void queued_voice_keeps_its_own_gain(void)
{
  uint8_t p[SPEECH_HEADER + 2 * SPEECH_FRAMES], pcm[3840];
  struct speech_state s = fresh(); size_t n; unsigned i;
  const unsigned original_gain = 6554, eof_gain = 18350, successor_gain = 45875;
  s.gain = (double)original_gain / SPEECH_FULL_GAIN;
  n = packet(p, &s, 1, 1, 960, original_gain, 1);
  CHECK(speech_receive(&s, p, n, origin) == 0);
  n = packet(p, &s, 2, 2, 0, eof_gain, 0);
  CHECK(speech_receive(&s, p, n, origin) == 0);
  music(pcm, 960, 10000); speech_mix(&s, pcm, sizeof(pcm), 960, 48000, 16, 2, origin);
  CHECK(s.gain == (double)original_gain / SPEECH_FULL_GAIN);
  for (i = 0; i < 1920; ++i) CHECK(read16(pcm + 2 * i) == 1100);
  n = packet(p, &s, 1, 3, 960, original_gain, 1);
  CHECK(speech_receive(&s, p, n, origin) == 0);
  n = packet(p, &s, 2, 4, 0, successor_gain, 1);
  CHECK(speech_receive(&s, p, n, origin) == 0);
  music(pcm, 960, 10000); speech_mix(&s, pcm, sizeof(pcm), 960, 48000, 16, 2, origin);
  CHECK(s.gain == (double)original_gain / SPEECH_FULL_GAIN);
  for (i = 0; i < 1920; ++i) CHECK(read16(pcm + 2 * i) == 1100);
  music(pcm, 960, 10000); speech_mix(&s, pcm, sizeof(pcm), 960, 48000, 16, 2, origin);
  CHECK(s.gain > (double)original_gain / SPEECH_FULL_GAIN); /* new lease applies after old tail */
}

static void quiet_pcm_cannot_repolicy_or_extend_audible_lease(void)
{
  uint8_t p[SPEECH_HEADER + 2 * SPEECH_FRAMES], pcm[3840];
  struct speech_state s = fresh(); size_t n;
  n = packet(p, &s, 1, 1, 1, 6554, 1);
  CHECK(speech_receive(&s, p, n, origin) == 0);
  n = packet(p, &s, 1, 2, 960, 45875, 0); put64(p + 56, origin + 100000000);
  memset(p + SPEECH_HEADER, 0, 1920);
  CHECK(speech_receive(&s, p, n, origin + 100000000) == 0);
  CHECK(s.active && s.duck == 6554 && s.lease == origin + SPEECH_AGE_NS);
  speech_expire(&s, origin + SPEECH_AGE_NS);
  CHECK(!s.active && s.count == 960 && s.expires[s.head] == origin + 350000000);
  s.gain = 1.0; music(pcm, 960, 10000);
  speech_mix(&s, pcm, sizeof(pcm), 960, 48000, 16, 2, origin + SPEECH_AGE_NS);
  CHECK(s.gain == 1.0 && read16(pcm) == 10000 && read16(pcm + 3838) == 10000);
  n = packet(p, &s, 2, 3, 0, 6554, 1); CHECK(speech_receive(&s, p, n, origin) == 0);
  n = packet(p, &s, 1, 4, 960, 45875, 0); memset(p + SPEECH_HEADER, 0, 1920);
  CHECK(speech_receive(&s, p, n, origin) == 0);
  n = packet(p, &s, 2, 5, 0, 18350, 0); CHECK(speech_receive(&s, p, n, origin) == 0);
  s.gain = 1.0; music(pcm, 960, 10000);
  speech_mix(&s, pcm, sizeof(pcm), 960, 48000, 16, 2, origin);
  CHECK(s.gain == 1.0 && !s.active && !s.count && read16(pcm) == 10000);
}

int main(void)
{
  malformed(); boundaries(); queue_bounds(); pcm_and_gain(); eof_drains_and_restores_after_last_sample();
  quiet_packets_never_duck_music();
  queued_voice_keeps_its_own_gain();
  quiet_pcm_cannot_repolicy_or_extend_audible_lease();
  CHECK(shiri_speech_init(NULL, 0, NULL, NULL, 0) == 0);
  CHECK(shiri_speech_init(NULL, 9, NULL, NULL, 1) < 0);
  shiri_speech_poll(); shiri_speech_mix(NULL, 0, 0, 0, 0, 0); shiri_speech_deinit();
  CHECK(speech_now() > 0);
  printf("late-speech: %u actual-source checks passed\n", checks);
  return 0;
}
