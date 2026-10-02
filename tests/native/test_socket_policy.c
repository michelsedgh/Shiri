/* Execute the production emitter and lifecycle transaction under sanitizers.
 * The bytecode interpreter and kernel fixture are tests, not kernel evidence. */
#define SHIRI_POLICY_PORTABLE_TEST
#include "../../native/shiri_socket_policy.c"
#include <assert.h>
#include <arpa/inet.h>

static unsigned int
evaluate(const struct shiri_instruction *instructions, size_t count,
         const uint32_t context[128])
{
  uint64_t registers[3] = {0};
  size_t cursor = 0, steps = 0;
  while (cursor < count && ++steps < 64) {
    struct shiri_instruction item = instructions[cursor++];
    assert(item.dst <= 2 && item.src <= 2);
    switch (item.code) {
      case 0xb7: registers[item.dst] = (uint64_t)(int64_t)item.immediate; break;
      case 0x61:
        assert(item.src == 1 && item.offset >= 0 && item.offset % 4 == 0 && item.offset < 512);
        registers[item.dst] = context[item.offset / 4]; break;
      case 0x55:
        if (registers[item.dst] != (uint64_t)(int64_t)item.immediate) cursor += item.offset;
        break;
      case 0x15:
        if (registers[item.dst] == (uint64_t)(int64_t)item.immediate) cursor += item.offset;
        break;
      case 0x95: return (unsigned int)registers[0];
      default: assert(!"Unexpected production instruction");
    }
  }
  assert(!"Program fell off its end or looped");
  return 0;
}

static void
test_emitted_policy(void)
{
  struct shiri_instruction instructions[SHIRI_MAX_INSTRUCTIONS];
  struct shiri_offsets offsets = {28, 4, 36, 44, 52};
  uint32_t context[128] = {0};
  unsigned int own, port, type, protocol, family, user_family;
  uint64_t cases = 0;
  size_t count;
  const unsigned int families[] = {0, 1, 2, 10, 16, 17};
  const unsigned int types[] = {0, 1, 2, 3, 5};
  const unsigned int protocols[] = {0, 6, 17, 132, UINT32_MAX};
  for (own = 3869; own <= 3939; own += 10) {
    count = shiri_emit(0, own, htons((uint16_t)own), offsets, instructions);
    assert(count && count <= SHIRI_MAX_INSTRUCTIONS);
    context[offsets.family / 4] = 2;
    context[offsets.user_family / 4] = 2;
    for (port = 0; port <= UINT16_MAX; ++port) {
      context[offsets.port / 4] = htons((uint16_t)port);
      for (type = 0; type < sizeof(types) / sizeof(*types); ++type) {
        context[offsets.type / 4] = types[type];
        for (protocol = 0; protocol < sizeof(protocols) / sizeof(*protocols); ++protocol) {
          unsigned int expected = (types[type] == 2 && protocols[protocol] == 17) ||
            (types[type] == 1 && protocols[protocol] == 6 && port == own);
          context[offsets.protocol / 4] = protocols[protocol];
          assert(evaluate(instructions, count, context) == expected);
          ++cases;
        }
      }
    }
    for (family = 0; family < sizeof(families) / sizeof(*families); ++family)
      for (user_family = 0; user_family < sizeof(families) / sizeof(*families); ++user_family) {
        context[offsets.family / 4] = families[family];
        context[offsets.user_family / 4] = families[user_family];
        context[offsets.type / 4] = 2; context[offsets.protocol / 4] = 17;
        assert(evaluate(instructions, count, context) == (families[family] == 2 && families[user_family] == 2));
      }
    count = shiri_emit(1, own, htons((uint16_t)own), offsets, instructions);
    assert(count == 2);
    for (port = 0; port <= UINT16_MAX; ++port) {
      context[offsets.port / 4] = htons((uint16_t)port);
      assert(evaluate(instructions, count, context) == 0);
      ++cases;
    }
  }
  assert(!shiri_emit(0, 0, 0, offsets, instructions));
  offsets.family = 3;
  assert(!shiri_emit(0, 3869, htons(3869), offsets, instructions));
  printf("emitted-policy cases=%" PRIu64 "\n", cases);
}

struct kernel_fixture {
  struct shiri_program programs[128];
  uint32_t hooks[2], inherited;
  unsigned int loads, calls, fail_at, validates, opens, closes, detaches;
  int wrong_family, altered_bytecode, altered_tag, missing_effective, second_identity_failure;
};

static int
fails(struct kernel_fixture *state)
{
  return ++state->calls == state->fail_at;
}

static int
fake_validate(void *context, int fd, uint64_t dev, uint64_t ino, const char *boot)
{
  struct kernel_fixture *state = context;
  assert(fd == 7 && dev == 99 && ino == 123);
  assert(!strcmp(boot, "11111111-2222-3333-4444-555555555555"));
  ++state->validates;
  return fails(state) || (state->second_identity_failure && state->validates > 1) ? -1 : 0;
}

static int
fake_load(void *context, int kind, unsigned int port)
{
  struct kernel_fixture *state = context;
  struct shiri_program *program;
  unsigned int fd;
  assert(shiri_port_valid(port));
  if (fails(state)) return -1;
  fd = ++state->loads + 10;
  assert(fd < 128);
  ++state->opens;
  program = &state->programs[fd];
  program->id = fd + 100;
  program->type = SHIRI_SOCK_ADDR_TYPE;
  program->length = 8;
  program->instructions[0] = 0xb7;
  program->instructions[1] = (unsigned char)kind;
  program->instructions[2] = (unsigned char)(port >> 8);
  program->instructions[3] = (unsigned char)port;
  snprintf(program->tag, sizeof(program->tag), "%08x%08x", port, kind);
  return (int)fd;
}

static int
fake_info(void *context, int fd, struct shiri_program *program)
{
  struct kernel_fixture *state = context;
  if (fails(state)) return -1;
  assert(fd > 10 && fd < 128);
  *program = state->programs[fd];
  if (state->wrong_family && state->loads > 2 && program->id <= 112) program->type++;
  if (state->altered_bytecode && state->loads > 2 && program->id <= 112) program->instructions[3]++;
  if (state->altered_tag && state->loads > 2 && program->id <= 112) program->tag[0] = 'f';
  return 0;
}

static int
fake_query(void *context, int fd, int kind, int effective, struct shiri_query *query)
{
  struct kernel_fixture *state = context;
  assert(fd == 7 && (kind == 0 || kind == 1));
  if (fails(state)) return -1;
  memset(query, 0, sizeof(*query));
  query->flags = SHIRI_ALLOW_MULTI;
  if (effective && state->inherited) query->ids[query->count++] = state->inherited;
  if (state->hooks[kind] && (!effective || !state->missing_effective)) query->ids[query->count++] = state->hooks[kind];
  return 0;
}

static int
fake_attach(void *context, int fd, int kind, int program_fd)
{
  struct kernel_fixture *state = context;
  assert(fd == 7 && !state->hooks[kind]);
  if (fails(state)) return -1;
  state->hooks[kind] = state->programs[program_fd].id;
  return 0;
}

static int
fake_detach(void *context, int fd, int kind, int program_fd)
{
  struct kernel_fixture *state = context;
  assert(fd == 7 && state->hooks[kind] == state->programs[program_fd].id);
  ++state->detaches;
  state->hooks[kind] = 0;
  return 0;
}

static int
fake_open_id(void *context, uint32_t id)
{
  struct kernel_fixture *state = context;
  unsigned int fd;
  if (fails(state)) return -1;
  for (fd = 11; fd < 128; ++fd)
    if (state->programs[fd].id == id) { ++state->opens; return (int)fd; }
  return -1;
}

static void
fake_close(void *context, int fd)
{
  struct kernel_fixture *state = context;
  assert(fd > 10 && fd < 128);
  ++state->closes;
}

static int
apply(struct kernel_fixture *state, int attach, const struct shiri_proof *saved, struct shiri_proof *proof)
{
  const struct shiri_operations ops = {state, fake_validate, fake_load, fake_info,
    fake_query, fake_attach, fake_detach, fake_open_id, fake_close};
  return shiri_policy_apply(&ops, attach, 7, 99, 123,
    "11111111-2222-3333-4444-555555555555", 3939, saved, proof);
}

static void
test_transactions(void)
{
  struct kernel_fixture state = {0};
  struct shiri_proof proof, verified, altered;
  unsigned int count, fault, scenario;
  state.inherited = 800;
  assert(!apply(&state, 1, NULL, &proof));
  count = state.calls;
  assert(proof.inet4.id && proof.inet6.id && state.hooks[0] && state.hooks[1]);
  assert(state.opens == state.closes && !state.detaches);
  assert(!apply(&state, 0, &proof, &verified));
  assert(verified.inet4.id == proof.inet4.id && verified.inet6.id == proof.inet6.id);
  assert(state.opens == state.closes && !state.detaches);
  assert(apply(&state, 1, NULL, &verified));
  assert(state.hooks[0] == proof.inet4.id && state.hooks[1] == proof.inet6.id);
  for (fault = 1; fault <= count; ++fault) {
    memset(&state, 0, sizeof(state)); state.fail_at = fault;
    assert(apply(&state, 1, NULL, &verified));
    assert(!verified.inet4.id && !verified.inet6.id);
    assert(!state.hooks[0] && !state.hooks[1]);
    assert(state.opens == state.closes);
  }
  for (scenario = 0; scenario < 7; ++scenario) {
    memset(&state, 0, sizeof(state));
    assert(!apply(&state, 1, NULL, &proof));
    altered = proof;
    switch (scenario) {
      case 0: state.altered_bytecode = 1; break;
      case 1: state.altered_tag = 1; break;
      case 2: state.wrong_family = 1; break;
      case 3: state.missing_effective = 1; break;
      case 4: altered.inet4.id = 999; break;
      case 5: altered.inet4.tag[0] = 'f'; break;
      case 6: state.second_identity_failure = 1; break;
    }
    assert(apply(&state, 0, &altered, &verified));
    assert(!verified.inet4.id && !verified.inet6.id);
    assert(state.hooks[0] == proof.inet4.id && state.hooks[1] == proof.inet6.id);
    assert(!state.detaches && state.opens == state.closes);
  }
  memset(&state, 0, sizeof(state)); state.second_identity_failure = 1;
  assert(apply(&state, 1, NULL, &verified));
  assert(!state.hooks[0] && !state.hooks[1] && state.detaches == 2);
  assert(state.opens == state.closes);
  puts("lifecycle: every attach fault rolled back; stale/altered/ineffective policy rejected; owned references persist");
}

static void
test_arguments(void)
{
  uint64_t result;
  unsigned int index;
  const char *invalid[] = {"", "00", "01", "-1", "+1", " 1", "1 ", "1x", "18446744073709551616"};
  assert(!shiri_decimal("18446744073709551615", UINT64_MAX, &result) && result == UINT64_MAX);
  for (index = 0; index < sizeof(invalid) / sizeof(*invalid); ++index)
    assert(shiri_decimal(invalid[index], UINT64_MAX, &result));
  assert(shiri_decimal("4294967296", UINT32_MAX, &result));
  assert(shiri_boot_valid("11111111-2222-3333-4444-555555555555"));
  assert(!shiri_boot_valid("11111111222233334444555555555555"));
  assert(!shiri_boot_valid("AAAAAAAA-2222-3333-4444-555555555555"));
  assert(shiri_hex("0123456789abcdef", 16));
  assert(!shiri_hex("0123456789abcdeF", 16));
}

int
main(void)
{
  test_arguments(); test_emitted_policy(); test_transactions();
  puts("socket-policy portable actual-C checks passed");
  return 0;
}
