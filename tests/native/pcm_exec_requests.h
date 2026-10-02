/* Keep ALSA userspace and kernel UAPI types in separate translation units. */
#ifndef SHIRI_PCM_EXEC_REQUESTS_H
#define SHIRI_PCM_EXEC_REQUESTS_H
enum shiri_pcm_request {
  SHIRI_PCM_VERSION, SHIRI_PCM_PREFER, SHIRI_CTL_WRITE, SHIRI_CTL_TLV_WRITE,
  SHIRI_CTL_TLV_COMMAND, SHIRI_CTL_ADD, SHIRI_CTL_REMOVE, SHIRI_CTL_POWER,
  SHIRI_CTL_CARD_INFO
};
unsigned long shiri_control_request(enum shiri_pcm_request request);
#endif
