/* SPDX-License-Identifier: GPL-2.0-or-later
 * Unchanged OwnTone d6fb3edf5831de38134ebd92fcf09a730ddd37aa queue/lifecycle
 * functions. --source checks these definitions against the composed source. */
size_t
ringbuffer_read(uint8_t **dst, size_t dstlen, struct ringbuffer *buf)
{
  int remaining;

  *dst = buf->buffer + buf->read_pos;

  if (buf->read_avail == 0 || dstlen == 0)
    return 0;

  remaining = buf->size - buf->read_pos;

  // The number of bytes we will return will be MIN(dstlen, remaining, read_avail)
  if (dstlen > remaining)
    dstlen = remaining;
  if (dstlen > buf->read_avail)
    dstlen = buf->read_avail;

  buf->read_pos = (buf->read_pos + dstlen) % buf->size;

  buf->write_avail += dstlen;
  buf->read_avail -= dstlen;

  return dstlen;
}

size_t
ringbuffer_write(struct ringbuffer *buf, const void* src, size_t srclen)
{
  int remaining;

  if (buf->write_avail == 0 || srclen == 0)
    return 0;

  if (srclen > buf->write_avail)
   srclen = buf->write_avail;

  remaining = buf->size - buf->write_pos;
  if (srclen > remaining)
    {
      memcpy(buf->buffer + buf->write_pos, src, remaining);
      memcpy(buf->buffer, src + remaining, srclen - remaining);
    }
  else
    {
      memcpy(buf->buffer + buf->write_pos, src, srclen);
    }

  buf->write_pos = (buf->write_pos + srclen) % buf->size;

  buf->write_avail -= srclen;
  buf->read_avail += srclen;

  return srclen;
}

void
ringbuffer_free(struct ringbuffer *buf, bool content_only)
{
  if (!buf)
    return;

  free(buf->buffer);

  if (content_only)
    memset(buf, 0, sizeof(struct ringbuffer));
  else
    free(buf);
}

static void
playback_session_free(struct alsa_playback_session *pb)
{
  if (!pb)
    return;

  // Unsubscribe from qualities that sync_correct() might have requested
  if (pb->sync_resample_step != 0)
    outputs_quality_unsubscribe(&pb->quality);

  pcm_close(pb->pcm);

  ringbuffer_free(&pb->prebuf, 1);

  free(pb->latency_history);
  free(pb->volume_buf);
  free(pb);
}

static void
playback_session_remove_all(struct alsa_session *as)
{
  struct alsa_playback_session *s;

  for (s = as->pb; s; s = as->pb)
    {
      as->pb = s->next;
      playback_session_free(s);
    }
}

static void
alsa_status(struct alsa_session *as)
{
  outputs_cb(as->callback_id, as->device_id, as->state);
  as->callback_id = -1;

  if (as->state == OUTPUT_STATE_FAILED || as->state == OUTPUT_STATE_STOPPED)
    alsa_session_cleanup(as);
}

static int
alsa_device_flush(struct output_device *device, int callback_id)
{
  struct alsa_session *as = device->session;

  playback_session_remove_all(as);

  as->callback_id = callback_id;
  as->state = OUTPUT_STATE_CONNECTED;
  alsa_status(as);

  return 1;
}

