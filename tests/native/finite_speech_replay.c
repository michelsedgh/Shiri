/* Portable replay of the EXACT maintained late-speech module, not a queue
 * model. No socket credentials, player startup or kernel timing are claimed. */
#include SHIRI_SPEECH_SOURCE
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>

static int read_exact(void *data, size_t bytes)
{
  return fread(data, 1, bytes, stdin) == bytes;
}
static uint32_t read32(void)
{
  uint8_t b[4];
  if (!read_exact(b, sizeof(b))) exit(2);
  return speech_u32(b);
}
static uint64_t read64(void)
{
  uint8_t b[8];
  if (!read_exact(b, sizeof(b))) exit(2);
  return speech_u64(b);
}
int main(void)
{
  uint8_t magic[8], packet[SPEECH_HEADER + SPEECH_FRAMES*2], pcm[SPEECH_FRAMES*4];
  struct speech_state state;
  uint64_t now, previous = 0;
  uint32_t events, bytes, frames, mixed = 0, received = 0;
  int kind;
  (void)&speech_hex; /* Module parser helpers stay compiled unchanged. */
  if (!read_exact(magic, sizeof(magic)) || memcmp(magic, "FTTSv1!!", 8)) return 2;
  memset(&state, 0, sizeof(state)); state.gain = 1.0;
  if (!read_exact(state.room, 16) || !read_exact(state.launch, 16)) return 2;
  events = read32();
  if (!events || events > 3000) return 2;
  for (uint32_t i = 0; i < events; ++i) {
    kind = fgetc(stdin); now = read64();
    if (!now || now < previous) return 2;
    previous = now;
    if (kind == 1) {
      bytes = read32();
      if (bytes < SPEECH_HEADER || bytes > sizeof(packet) || !read_exact(packet, bytes)) return 2;
      (void)speech_receive(&state, packet, bytes, now); ++received;
    } else if (kind == 2) {
      frames = read32(); bytes = read32();
      if (!frames || frames > SPEECH_FRAMES || bytes != frames*4 || !read_exact(pcm, bytes)) return 2;
      speech_mix(&state, pcm, bytes, (int)frames, SPEECH_RATE, 16, 2, now);
      if (fwrite(pcm, 1, bytes, stdout) != bytes) return 2;
      ++mixed;
    } else return 2;
  }
  if (fgetc(stdin) != EOF || fflush(stdout)) return 2;
  fprintf(stderr, "{\"received_events\":%u,\"mixed_events\":%u,\"admitted\":%" PRIu64
          ",\"refused\":%" PRIu64 ",\"expired\":%" PRIu64 ",\"remaining_frames\":%u,"
          "\"kernel_credentials_proven\":false}\n", received, mixed, state.admitted,
          state.refused, state.expired, state.count);
  return 0;
}
