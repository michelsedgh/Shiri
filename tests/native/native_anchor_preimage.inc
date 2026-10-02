static int
pb_timer_native_prepare(void)
{
  struct itimerspec tick;
  struct timespec now, anchor;
  uint64_t operation;
  int state, ret;
  state = input_pipe_shiri_state(&operation);
  if (state != 1 || operation != pb_timer_native_operation)
    return -1;
  if (clock_gettime(CLOCK_MONOTONIC, &now) < 0)
    return -1;
  if (pb_timer_native_anchored)
    return timespec_cmp(now, pb_timer_native_anchor) < 0 ? 0 : 1;
  ret = input_peek_sync(&anchor);
  if (ret < 0 || timespec_cmp(now, pb_timer_native_wait_until) >= 0)
    return -1;
  if (!ret)
    return 0;
  /* Never replace a late native anchor with the receive or timer clock. */
  if (timespec_cmp(anchor, now) <= 0)
    return -1;
  tick.it_interval = player_tick_interval;
  tick.it_value = anchor;
#ifdef HAVE_TIMERFD
  ret = timerfd_settime(pb_timer_fd, TFD_TIMER_ABSTIME, &tick, NULL);
#else
  ret = timer_settime(pb_timer, TIMER_ABSTIME, &tick, NULL);
#endif
  if (ret < 0)
    return -1;
  pb_timer_native_anchor = anchor;
  pb_timer_native_anchored = true;
  /* Control/quality markers must not substitute CLOCK_MONOTONIC now for this
   * first sample's anchor when the session has not read any PCM yet. */
  pb_session.start_ts = anchor;
  pb_session.pts = anchor;
  return 0;
}
