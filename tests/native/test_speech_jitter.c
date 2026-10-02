/* Exercise the actual maintained media module; no clock or transport mirror. */
#define main original_late_speech_main
#include "test_late_speech.c"
#undef main

static size_t voice_packet(uint8_t *p, const struct speech_state *s,
                          uint64_t seq, unsigned frames, uint64_t emitted,
                          int voice, unsigned active)
{
  size_t n = packet(p, s, 1, seq, frames, SPEECH_FULL_GAIN, active);
  put64(p + 56, emitted);
  for (unsigned i = 0; i < frames; ++i) raw16(p + SPEECH_HEADER + 2 * i, voice);
  return n;
}

static void mix_block(struct speech_state *s, uint8_t *pcm, uint64_t now)
{
  music(pcm, 480, 1000);
  speech_mix(s, pcm, 1920, 480, 48000, 16, 2, now);
}

static void all_samples(const uint8_t *pcm, unsigned frames, int value)
{
  for (unsigned i = 0; i < frames * 2; ++i) CHECK(read16(pcm + 2 * i) == value);
}

#ifdef JITTER_PREIMAGE
int main(void)
{
  struct speech_state s = fresh();
  uint8_t p[SPEECH_HEADER + 2 * SPEECH_FRAMES], pcm[1920];
  size_t n = voice_packet(p, &s, 1, 960, origin, 100, 1);
  CHECK(speech_receive(&s, p, n, origin) == 0);
  mix_block(&s, pcm, origin); all_samples(pcm, 480, 1100);
  mix_block(&s, pcm, origin + 10000000); all_samples(pcm, 480, 1100);
  mix_block(&s, pcm, origin + 20000000); all_samples(pcm, 480, 1000);
  n = voice_packet(p, &s, 2, 960, origin + 30000000, 200, 1);
  CHECK(speech_receive(&s, p, n, origin + 30000000) == 0);
  mix_block(&s, pcm, origin + 30000000); all_samples(pcm, 480, 1200);
  CHECK(!s.refused && !s.expired && s.admitted == 2);
  printf("preimage: valid30ms second arrival produced480 silent voice frames; program unchanged\n");
  return 0;
}
#else

static void repaired_arrival(void)
{
  struct speech_state s = fresh();
  uint8_t p[SPEECH_HEADER + 2 * SPEECH_FRAMES], pcm[1920];
  size_t n = voice_packet(p, &s, 1, 960, origin, 100, 1);
  CHECK(speech_receive(&s, p, n, origin) == 0);
  CHECK(s.priming_until == origin + 20000000 && s.count == 960);
  mix_block(&s, pcm, origin); all_samples(pcm, 480, 1000);
  mix_block(&s, pcm, origin + 10000000); all_samples(pcm, 480, 1000);
  CHECK(s.count == 960 && s.mixed_frames == 0 && s.priming_frames == 960);
  mix_block(&s, pcm, origin + 20000000); all_samples(pcm, 480, 1100);
  n = voice_packet(p, &s, 2, 960, origin + 30000000, 200, 1);
  CHECK(speech_receive(&s, p, n, origin + 30000000) == 0);
  mix_block(&s, pcm, origin + 30000000); all_samples(pcm, 480, 1100);
  n = packet(p, &s, 2, 3, 0, 65536, 0); put64(p + 56, origin + 30000000);
  CHECK(speech_receive(&s, p, n, origin + 30000000) == 0);
  mix_block(&s, pcm, origin + 40000000); all_samples(pcm, 480, 1200);
  mix_block(&s, pcm, origin + 50000000); all_samples(pcm, 480, 1200);
  CHECK(!s.running && !s.count && !s.underflow_events && !s.underflow_frames);
  CHECK(s.mixed_frames == 1920 && s.admitted_frames == 1920);
  CHECK(s.max_interarrival_ns == 30000000 && s.priming_events == 1);
  CHECK(!s.refused && !s.expired && s.sequence == 3 && s.admitted == 3);
}

static void long_ordered_streams(void)
{
  uint8_t p[SPEECH_HEADER + 2 * SPEECH_FRAMES], pcm[1920];
  /* Every next 20 ms packet may arrive 10 ms late. Tick and program continue
   * exactly 10 ms; ordered streams exercise the queue across repeated wraps. */
  for (unsigned packets = 2; packets <= 64; ++packets) {
    struct speech_state s = fresh(); unsigned next = 0, consumed = 0;
    for (unsigned tick = 0; tick <= packets * 2 + 3; ++tick) {
      uint64_t now = origin + (uint64_t)tick * 10000000;
      if (next < packets && (tick == 0 || tick == next * 2 + 1)) {
        size_t n = voice_packet(p, &s, next + 1, 960, now, 100 + (int)next, 1);
        CHECK(speech_receive(&s, p, n, now) == 0); ++next;
        if (next == packets) {
          n = packet(p, &s, 2, packets + 1, 0, 65536, 0); put64(p + 56, now);
          CHECK(speech_receive(&s, p, n, now) == 0);
        }
      }
      mix_block(&s, pcm, now);
      if (tick >= 2 && consumed < packets * 960) {
        all_samples(pcm, 480, 1100 + (int)(consumed / 960)); consumed += 480;
      } else all_samples(pcm, 480, 1000);
    }
    CHECK(next == packets && consumed == packets * 960);
    CHECK(s.admitted_frames == consumed && s.mixed_frames == consumed && !s.count);
    CHECK(!s.expired && !s.refused && !s.underflow_events && !s.underflow_frames);
    CHECK(s.priming_frames == 960 && s.priming_events == 1 && !s.running);
  }
}

static void tiny_eof(void)
{
  uint8_t p[SPEECH_HEADER + 2 * SPEECH_FRAMES], pcm[3840];
  const unsigned sizes[] = {1, 20, 120, 480, 959, 960};
  for (unsigned k = 0; k < sizeof(sizes) / sizeof(sizes[0]); ++k) {
    struct speech_state s = fresh(); unsigned frames = sizes[k];
    size_t n = voice_packet(p, &s, 1, frames, origin, 123, 1);
    CHECK(speech_receive(&s, p, n, origin) == 0);
    n = packet(p, &s, 2, 2, 0, 65536, 0); put64(p + 56, origin + 1000000);
    CHECK(speech_receive(&s, p, n, origin + 1000000) == 0);
    mix_block(&s, pcm, origin); all_samples(pcm, 480, 1000);
    mix_block(&s, pcm, origin + 10000000); all_samples(pcm, 480, 1000);
    CHECK(s.count == frames && !s.active);
    music(pcm, 960, 1000); speech_mix(&s, pcm, 3840, 960, 48000, 16, 2, origin + 20000000);
    for (unsigned i = 0; i < 960; ++i) {
      CHECK(read16(pcm + 4 * i) == (i < frames ? 1123 : 1000));
      CHECK(read16(pcm + 4 * i + 2) == (i < frames ? 1123 : 1000));
    }
    CHECK(!s.count && !s.running && s.mixed_frames == frames);
    CHECK(!s.expired && !s.underflow_events && !s.underflow_frames);
  }
}

static void original_age_and_overflow(void)
{
  uint8_t p[SPEECH_HEADER + 2 * SPEECH_FRAMES], pcm[1920];
  struct speech_state s = fresh();
  size_t n = voice_packet(p, &s, 1, 960, origin - 235000000, 100, 1);
  CHECK(speech_receive(&s, p, n, origin) == 0);
  CHECK(s.expires[0] == origin + 15000000 && s.max_admission_age_ns == 235000000);
  mix_block(&s, pcm, origin); all_samples(pcm, 480, 1000);
  mix_block(&s, pcm, origin + 20000000); all_samples(pcm, 480, 1000);
  CHECK(!s.count && !s.active && !s.running && s.expired == 960 && !s.mixed_frames);
  s = fresh();
  for (unsigned i = 0; i < 12; ++i) {
    n = voice_packet(p, &s, i + 1, 960, origin, 100, 1);
    CHECK(speech_receive(&s, p, n, origin) == 0);
  }
  n = voice_packet(p, &s, 13, 480, origin, 100, 1);
  CHECK(speech_receive(&s, p, n, origin) == 0 && s.count == SPEECH_QUEUE);
  n = voice_packet(p, &s, 14, 1, origin, 100, 1);
  refused_without_effect(&s, p, n, origin);
  CHECK(s.admitted_frames == 12000 && s.priming_events == 1);
  mix_block(&s, pcm, origin); CHECK(s.count == 12000 && !s.mixed_frames);
  mix_block(&s, pcm, origin + SPEECH_AGE_NS); all_samples(pcm, 480, 1000);
  CHECK(s.expired == 12000 && !s.count && !s.active && !s.running);
  /* The ingress poll may expire a whole old run before the next mix tick.
   * Its successor must receive a fresh reserve, not inherit a retired prime. */
  s = fresh(); n = voice_packet(p, &s, 1, 960, origin, 100, 1);
  CHECK(speech_receive(&s, p, n, origin) == 0);
  speech_expire(&s, origin + SPEECH_AGE_NS);
  CHECK(!s.running && !s.count && !s.active && s.expired == 960);
  n = voice_packet(p, &s, 2, 960, origin + 260000000, 200, 1);
  CHECK(speech_receive(&s, p, n, origin + 260000000) == 0);
  CHECK(s.priming_until == origin + 280000000 && s.priming_events == 2);
  mix_block(&s, pcm, origin + 260000000); all_samples(pcm, 480, 1000);
  mix_block(&s, pcm, origin + 280000000); all_samples(pcm, 480, 1200);
}

static void actual_underflow_is_visible(void)
{
  uint8_t p[SPEECH_HEADER + 2 * SPEECH_FRAMES], pcm[1920];
  struct speech_state s = fresh();
  size_t n = voice_packet(p, &s, 1, 960, origin, 100, 1);
  CHECK(speech_receive(&s, p, n, origin) == 0);
  mix_block(&s, pcm, origin); mix_block(&s, pcm, origin + 10000000);
  mix_block(&s, pcm, origin + 20000000); all_samples(pcm, 480, 1100);
  mix_block(&s, pcm, origin + 30000000); all_samples(pcm, 480, 1100);
  mix_block(&s, pcm, origin + 40000000); all_samples(pcm, 480, 1000);
  mix_block(&s, pcm, origin + 50000000); all_samples(pcm, 480, 1000);
  CHECK(s.underflow_events == 1 && s.underflow_frames == 960);
  CHECK(s.last_underflow_ns == origin + 40000000 && s.underflowing);
  n = voice_packet(p, &s, 2, 960, origin + 60000000, 200, 1);
  CHECK(speech_receive(&s, p, n, origin + 60000000) == 0);
  mix_block(&s, pcm, origin + 60000000); all_samples(pcm, 480, 1200);
  CHECK(!s.underflowing && s.last_resume_ns == origin + 60000000);
  CHECK(s.priming_events == 1 && s.max_interarrival_ns == 60000000);
  CHECK(s.mixed_frames == 1440 && !s.refused && !s.expired);
}

static void queue_gain_quiet_and_nonce(void)
{
  uint8_t p[SPEECH_HEADER + 2 * SPEECH_FRAMES], pcm[1920];
  struct speech_state s = fresh();
  size_t n = packet(p, &s, 1, 1, 480, 6554, 1);
  CHECK(speech_receive(&s, p, n, origin) == 0);
  n = packet(p, &s, 2, 2, 0, 45875, 0);
  CHECK(speech_receive(&s, p, n, origin) == 0 && s.duck == 45875);
  s.gain = 6554.0 / 65536;
  music(pcm, 480, 10000); speech_mix(&s, pcm, 1920, 480, 48000, 16, 2, origin + 20000000);
  all_samples(pcm, 480, 1100); CHECK(!s.count && !s.running);
  s = fresh();
  n = voice_packet(p, &s, 1, 960, origin, 1, 0);
  CHECK(speech_receive(&s, p, n, origin) == 0 && !s.active);
  mix_block(&s, pcm, origin); all_samples(pcm, 480, 1000); CHECK(s.gain == 1);
  mix_block(&s, pcm, origin + 20000000); all_samples(pcm, 480, 1001);
  s = fresh();
  n = packet(p, &s, 1, 1, 480, 13107, 1);
  CHECK(speech_receive(&s, p, n, origin) == 0);
  uint64_t lease = s.lease;
  n = voice_packet(p, &s, 2, 480, origin + 10000000, 0, 0);
  CHECK(speech_receive(&s, p, n, origin + 10000000) == 0);
  CHECK(s.active && s.lease == lease && s.duck == 13107);
  s = fresh();
  n = voice_packet(p, &s, 1, 960, origin, 100, 1);
  p[40] ^= 1; refused_without_effect(&s, p, n, origin);
  CHECK(!s.running && !s.priming_events && !s.admitted_frames);
  CHECK(shiri_speech_init(NULL, 0, NULL, NULL, 0) == 0);
  speech = fresh(); speech_fd = -1;
  n = voice_packet(p, &speech, 1, 960, origin, 100, 1);
  CHECK(speech_receive(&speech, p, n, origin) == 0);
  shiri_speech_deinit(); CHECK(!speech.count && !speech.running && !speech.active);
}

static void status_read_only_and_saturation(void)
{
  struct shiri_speech_status out;
  uint8_t p[SPEECH_HEADER + 2 * SPEECH_FRAMES];
  speech = fresh(); speech_fd = -1;
  size_t n = voice_packet(p, &speech, 1, 960, origin, 100, 1);
  CHECK(speech_receive(&speech, p, n, origin) == 0);
  struct speech_state before = speech;
  shiri_speech_status(&out);
  CHECK(!memcmp(&before, &speech, sizeof(speech)));
  CHECK(!out.configured && out.observed_monotonic_ns && out.queue_frames == 960);
  CHECK(!memcmp(out.room, speech.room, 16) && !memcmp(out.launch, speech.launch, 16));
  CHECK(out.sequence == 1 && out.admitted_frames == 960 && out.mixed_frames == 0);
  CHECK(out.first_admission_ns == origin && out.last_emitted_ns == origin);
  speech_add(&speech.mixed_frames, UINT64_MAX); CHECK(speech.mixed_frames == UINT64_MAX);
  speech_add(&speech.mixed_frames, 1); CHECK(speech.mixed_frames == UINT64_MAX);
  speech_counter(&speech.mixed_frames); CHECK(speech.mixed_frames == UINT64_MAX);
  shiri_speech_status(NULL); CHECK(speech.mixed_frames == UINT64_MAX);
  shiri_speech_deinit();
}

static void clipping_and_existing_gain_law(void)
{
  const int values[] = {-32768, -32000, -1, 0, 1, 32000, 32767};
  uint8_t p[SPEECH_HEADER + 2 * SPEECH_FRAMES], pcm[3840];
  for (unsigned a = 0; a < sizeof(values) / sizeof(values[0]); ++a) {
    for (unsigned b = 0; b < sizeof(values) / sizeof(values[0]); ++b) {
      struct speech_state s = fresh();
      size_t n = voice_packet(p, &s, 1, 1, origin, values[a], 1);
      CHECK(speech_receive(&s, p, n, origin) == 0);
      music(pcm, 1, values[b]); speech_mix(&s, pcm, 4, 1, 48000, 16, 2, origin + 20000000);
      int expected = values[a] + values[b];
      if (expected > 32767) expected = 32767;
      if (expected < -32768) expected = -32768;
      all_samples(pcm, 1, expected);
      CHECK(s.mixed_frames == 1 && !s.count);
    }
  }
  struct speech_state s = fresh();
  size_t n = packet(p, &s, 2, 1, 0, 13107, 1);
  CHECK(speech_receive(&s, p, n, origin) == 0);
  for (unsigned i = 0; i < 4; ++i) {
    music(pcm, 480, 10000);
    speech_mix(&s, pcm, 1920, 480, 48000, 16, 2, origin + i * UINT64_C(10000000));
  }
  CHECK(fabs(s.gain - 13107.0 / 65536) < 1e-10);
  CHECK(read16(pcm + 1918) == 2000 && !s.running && !s.priming_events);
  n = packet(p, &s, 2, 2, 0, 13107, 0); put64(p + 56, origin + 40000000);
  CHECK(speech_receive(&s, p, n, origin + 40000000) == 0);
  for (unsigned i = 0; i < 25; ++i) {
    music(pcm, 480, 10000);
    speech_mix(&s, pcm, 1920, 480, 48000, 16, 2, origin + 40000000 + i * UINT64_C(10000000));
  }
  CHECK(s.gain == 1 && read16(pcm + 1918) == 10000);
}

int main(void)
{
  malformed(); boundaries(); repaired_arrival(); long_ordered_streams(); tiny_eof();
  original_age_and_overflow(); actual_underflow_is_visible(); queue_gain_quiet_and_nonce();
  status_read_only_and_saturation(); clipping_and_existing_gain_law();
  printf("jitter1: %u actual-module checks; ordered prefix/tail,20ms reserve,250ms expiry,visible underflow\n", checks);
  return 0;
}
#endif
