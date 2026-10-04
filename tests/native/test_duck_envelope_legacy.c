/* Emit an exact PCM vector from the actual preimage/postimage legacy path. */
#define _GNU_SOURCE
#include SHIRI_SPEECH_SOURCE
#include <assert.h>

static const uint8_t owner_a[16] = {1}, owner_b[16] = {2};
static void put32(uint8_t *p, uint32_t v)
{ p[0] = v >> 24; p[1] = v >> 16; p[2] = v >> 8; p[3] = v; }
static void put64(uint8_t *p, uint64_t v)
{ put32(p, v >> 32); put32(p + 4, v); }
static void raw16(uint8_t *p, int v)
{ unsigned n = v < 0 ? v + 65536 : v; p[0] = n; p[1] = n >> 8; }
static void send_packet(struct speech_state *s, unsigned frames, int sample,
                        unsigned active, unsigned gain, uint64_t now)
{
  uint8_t p[SPEECH_HEADER + 2 * SPEECH_FRAMES];
  memset(p, 0, sizeof(p)); memcpy(p, "SHRITTS1", 8);
  p[8] = 2; p[9] = frames ? 1 : 2; p[11] = SPEECH_HEADER;
  put32(p + 12, frames * 2); put64(p + 16, s->sequence + 1);
  memcpy(p + 24, s->room, 16); memcpy(p + 40, s->launch, 16); memcpy(p + 80, s->owner, 16);
  put64(p + 56, now); put32(p + 64, frames); put32(p + 68, gain); put32(p + 72, active);
  for (unsigned i = 0; i < frames; ++i) raw16(p + SPEECH_HEADER + 2 * i, sample);
  assert(speech_receive(s, p, SPEECH_HEADER + 2 * frames, now) == 0);
}
int main(void)
{
  struct speech_state s; uint8_t pcm[480 * 4];
  const uint64_t origin = UINT64_C(10000000000);
  memset(&s, 0, sizeof(s)); s.gain = 1;
  assert(speech_hex("b6786543-7eb2-443d-83b1-65b984123a76", s.room, 1) == 0);
  assert(speech_hex("123456789abcdef0123456789abcdef0", s.launch, 0) == 0);
  assert(speech_begin(&s, owner_a, origin) == 0);
  for (unsigned tick = 0; tick < 80; ++tick) {
    uint64_t now = origin + (uint64_t)tick * UINT64_C(10000000);
    if (tick == 0) send_packet(&s, 0, 0, 1, 13107, now);
    if (tick == 6) send_packet(&s, 480, 100, 1, 32768, now);
    if (tick == 8) send_packet(&s, 0, 0, 1, 58982, now);
    if (tick == 12) send_packet(&s, 0, 0, 0, 32768, now);
    if (tick == 13) send_packet(&s, 960, 0, 0, 0, now);
    if (tick == 15) assert(speech_finish(&s, owner_a) == 0);
    if (tick == 25) assert(speech_begin(&s, owner_b, now) == 0);
    if (tick == 26) send_packet(&s, 0, 0, 1, 0, now);
    if (tick == 31) send_packet(&s, 1, 123, 1, 13107, now);
    if (tick == 32) assert(speech_finish(&s, owner_b) == 0);
    if (tick == 34) assert(speech_cancel(&s, owner_b) == 0);
    for (unsigned i = 0; i < 480; ++i) {
      raw16(pcm + i * 4, (int)(i % 41) * 1600 - 32000);
      raw16(pcm + i * 4 + 2, 32000 - (int)(i % 41) * 1600);
    }
    speech_mix(&s, pcm, sizeof(pcm), 480, SPEECH_RATE, 16, 2, now);
    assert(fwrite(pcm, sizeof(pcm), 1, stdout) == 1);
  }
  assert(s.gain == 1 && !s.count && !s.active);
  return 0;
}
