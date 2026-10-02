/* Actual native kernel ioctl encodings; no duplicated/guessed request values. */
#include <sys/time.h>
#include <time.h>
#include <sys/ioctl.h>
#include <sound/asound.h>
#include "pcm_exec_requests.h"

unsigned long
shiri_control_request(enum shiri_pcm_request request)
{
  switch (request) {
    case SHIRI_PCM_VERSION: return SNDRV_CTL_IOCTL_PVERSION;
    case SHIRI_PCM_PREFER: return SNDRV_CTL_IOCTL_PCM_PREFER_SUBDEVICE;
    case SHIRI_CTL_WRITE: return SNDRV_CTL_IOCTL_ELEM_WRITE;
    case SHIRI_CTL_TLV_WRITE: return SNDRV_CTL_IOCTL_TLV_WRITE;
    case SHIRI_CTL_TLV_COMMAND: return SNDRV_CTL_IOCTL_TLV_COMMAND;
    case SHIRI_CTL_ADD: return SNDRV_CTL_IOCTL_ELEM_ADD;
    case SHIRI_CTL_REMOVE: return SNDRV_CTL_IOCTL_ELEM_REMOVE;
    case SHIRI_CTL_POWER: return SNDRV_CTL_IOCTL_POWER;
    case SHIRI_CTL_CARD_INFO: return SNDRV_CTL_IOCTL_CARD_INFO;
  }
  return 0;
}
