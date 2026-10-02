/* Interpret the exact production classic-BPF filter under ASan/UBSan.
 * Actual seccomp/ALSA enforcement is a separate root Linux test. */
#define SHIRI_PCM_PORTABLE_TEST
#include "../../native/shiri_pcm_exec.c"
#include <assert.h>
#include <inttypes.h>
#include <stdlib.h>

static uint32_t
evaluate(const struct shiri_filter_instruction *instructions, size_t count,
         const uint32_t data[32])
{
  uint32_t accumulator = 0;
  size_t cursor = 0, steps = 0;
  while (cursor < count && ++steps <= SHIRI_SECCOMP_MAX) {
    struct shiri_filter_instruction item = instructions[cursor++];
    switch (item.code) {
      case 0x20:
        assert(item.value % 4 == 0 && item.value < 128);
        accumulator = data[item.value / 4]; break;
      case 0x15: cursor += accumulator == item.value ? item.yes : item.no; break;
      case 0x45: cursor += accumulator & item.value ? item.yes : item.no; break;
      case 0x54: accumulator &= item.value; break;
      case 0x06: return item.value;
      default: assert(!"Unsupported production filter instruction");
    }
  }
  assert(!"Filter fell off its end or looped");
  return 0;
}

static void
test_emitted_filter(void)
{
  struct shiri_filter_instruction instructions[SHIRI_SECCOMP_MAX];
  struct shiri_filter_parameters parameters = {
    0xc000003e, 16, 425, 426, 427, 0x80045500, 0x40045532, 4, 12, 48
  };
  uint32_t data[32] = {0};
  uint32_t direction, size, number, command, architecture, syscall_number, family;
  uint64_t cases = 0;
  size_t count;
  for (architecture = 0; architecture < 2; ++architecture) {
    parameters.architecture = architecture ? 0xc00000b7 : 0xc000003e;
    parameters.ioctl_number = architecture ? 29 : 16;
    count = shiri_pcm_filter(parameters, instructions);
    assert(count <= SHIRI_SECCOMP_MAX);
    data[parameters.architecture_offset / 4] = parameters.architecture;
    data[parameters.number_offset / 4] = parameters.ioctl_number;
    for (direction = 0; direction < 4; ++direction)
      for (size = 0; size < 16384; ++size)
        for (number = 0; number < 256; ++number) {
          uint32_t expected;
          command = direction << 30 | size << 16 | 0x5500 | number;
          expected = command == 0x80045500 || command == 0x40045532 ? SHIRI_SECCOMP_ALLOW : SHIRI_SECCOMP_DENY;
          data[parameters.request_low_offset / 4] = command;
          assert(evaluate(instructions, count, data) == expected);
          ++cases;
        }
    for (family = 0; family < 256; ++family)
      for (number = 0; number < 256; ++number) {
        data[parameters.request_low_offset / 4] = family << 8 | number;
        assert(evaluate(instructions, count, data) == (family == 'U' ? SHIRI_SECCOMP_DENY : SHIRI_SECCOMP_ALLOW));
      }
    /* Actual syscall ioctl casts command to unsigned int; high64 bits do not
     * grant access to a different command. Only the matching low word matters. */
    data[parameters.request_low_offset / 4 + 1] = UINT32_MAX;
    data[parameters.request_low_offset / 4] = parameters.version_request;
    assert(evaluate(instructions, count, data) == SHIRI_SECCOMP_ALLOW);
    data[parameters.request_low_offset / 4] = 0xc4c85513; /* ELEM_WRITE */
    assert(evaluate(instructions, count, data) == SHIRI_SECCOMP_DENY);
    data[parameters.request_low_offset / 4 + 1] = 0;
    for (syscall_number = 0; syscall_number < 1024; ++syscall_number) {
      data[parameters.number_offset / 4] = syscall_number;
      assert(evaluate(instructions, count, data) ==
        (syscall_number == parameters.ioctl_number || (syscall_number >= 425 && syscall_number <= 427)
          ? SHIRI_SECCOMP_DENY : SHIRI_SECCOMP_ALLOW));
      data[parameters.number_offset / 4] = syscall_number | 0x40000000;
      assert(evaluate(instructions, count, data) == SHIRI_SECCOMP_DENY);
    }
    data[parameters.architecture_offset / 4] = architecture ? 0x40000028 : 0x40000003; /* compat ARM/i386 */
    data[parameters.number_offset / 4] = parameters.ioctl_number;
    assert(evaluate(instructions, count, data) == SHIRI_SECCOMP_KILL);
    data[parameters.architecture_offset / 4] = 0;
    assert(evaluate(instructions, count, data) == SHIRI_SECCOMP_KILL);
  }
  printf("control-command encodings=%" PRIu64 "; native-only; x32/compat/io_uring fenced\n", cases);
}

static void
test_arguments(void)
{
  char *arguments[129] = {"/opt/shiri/bin/owntone", "-c", "/run/shiri-worker/config/owntone.conf"};
  char *large = malloc(4098);
  unsigned int index;
  assert(large);
  assert(shiri_pcm_arguments(3, arguments));
  arguments[0] = "owntone"; assert(!shiri_pcm_arguments(3, arguments));
  arguments[0] = "/opt/shiri/bin/owntone";
  assert(!shiri_pcm_arguments(0, arguments));
  memset(large, 'a', 4097); large[4097] = 0;
  arguments[1] = large; assert(!shiri_pcm_arguments(3, arguments));
  large[4096] = 0; assert(shiri_pcm_arguments(3, arguments));
  for (index = 1; index < 128; ++index) arguments[index] = large;
  assert(!shiri_pcm_arguments(128, arguments));
  assert(!shiri_pcm_arguments(129, arguments));
  arguments[1] = NULL; assert(!shiri_pcm_arguments(3, arguments));
  free(large);
}

int
main(void)
{
  test_emitted_filter(); test_arguments();
  puts("PCM exec portable actual-C checks passed");
  return 0;
}
