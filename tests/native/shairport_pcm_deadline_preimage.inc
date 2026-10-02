static int play_native(int number, void *pcm, int samples, int sample_type, uint32_t rtp, uint64_t playtime) {
  uint8_t message[SHIRI_PCM_HEADER + SHIRI_PCM_MAX_PAYLOAD];
  struct shiri_pcm_packet packet;
  ssize_t n;
  int old_cancel, ret = 0;
  pthread_setcancelstate(PTHREAD_CANCEL_DISABLE, &old_cancel);
  pthread_mutex_lock(&lock);
  if (number != connection) { ret = ESTALE; goto done; }
  if (samples <= 0 || samples > (int)SHIRI_PCM_MAX_FRAMES || !configured || !pcm) { ret = EINVAL; goto done; }
  if (fd < 0) { ret = EPIPE; goto done; }
  if (sample_type != play_samples_are_timed || !playtime) {
    /* Native lead-in/missing-frame silence has no anchor; don't invent one. */
    state.sequence++;
    state.frame_index += (uint64_t)samples; gap = 1; goto done;
  }
  packet = state;
  packet.kind = SHIRI_PCM; packet.frames = (uint32_t)samples; packet.payload = (uint32_t)samples * 4u;
  packet.rtp = rtp; packet.presentation = playtime;
  packet.flags |= gap ? SHIRI_PCM_GAP : 0;
  ret = sample_clocks_recovered(&packet);
  if (ret) goto clock_failed;
  packet.sequence = state.sequence + 1;
  shiri_pcm_encode(message, &packet);
  memcpy(message + SHIRI_PCM_HEADER, pcm, packet.payload);
  /* Encoding/copying cannot refresh the deadline or advance unsent media. */
  ret = clock_recovery_deadline();
  if (ret) goto clock_failed;
  state.sequence = packet.sequence;
  n = send(fd, message, SHIRI_PCM_HEADER + packet.payload, MSG_DONTWAIT | MSG_NOSIGNAL);
  state.frame_index += (uint64_t)samples;
  if (n == (ssize_t)(SHIRI_PCM_HEADER + packet.payload)) {
    gap = 0;
    clock_recovery_sent();
  }
  else if (n < 0 && (errno == EAGAIN || errno == ENOBUFS)) gap = 1;
  else { ret = EPIPE; close_route(); }
  goto done;
clock_failed:
  warn("Shiri native PCM clock sampling failed; this exact producer remains fenced.");
  warn("Shiri native PCM clock recovery refused: result=%d last_batch_result=%d "
       "batches=%u refused=%u elapsed_ns=%llu.", ret, clock_recovery.last_batch_result,
       clock_recovery.batches, clock_recovery.refused,
       (unsigned long long)clock_recovery.elapsed_ns);
  close_route();
done:
  clock_order.active = 0;
  pthread_mutex_unlock(&lock);
  pthread_setcancelstate(old_cancel, NULL);
  return ret;
}
