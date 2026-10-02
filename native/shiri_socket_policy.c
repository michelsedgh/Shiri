/* SPDX-License-Identifier: MIT
 * Root-only, map-free cgroup2 explicit-bind enforcement for room OwnTone.
 *
 * CLI: attach FD DEV INO BOOT_UUID PORT
 *      verify FD DEV INO BOOT_UUID PORT ID4 TAG4 ID6 TAG6
 * The broker passes an already-held, identity-checked directory descriptor.
 * BPF_PROG_ATTACH references persist through broker death, until cgroup removal.
 * This does not prohibit listen()'s implicit ephemeral autobind. The broker must
 * separately verify that the namespace ephemeral range excludes 3869..3939.
 * Primary ABI: Linux v5.15 include/uapi/linux/bpf.h and kernel/bpf/cgroup.c.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <inttypes.h>
#include <limits.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define SHIRI_MAX_PROGRAMS 64u
#define SHIRI_MAX_TRANSLATED 1024u
#define SHIRI_MAX_INSTRUCTIONS 32u
#define SHIRI_ALLOW_MULTI 2u
#define SHIRI_SOCK_ADDR_TYPE 18u

/* Architecture-independent instruction description. The Linux adapter assigns
 * the real UAPI bitfields rather than assuming their byte or compiler layout. */
struct shiri_instruction {
  uint8_t code, dst, src;
  int16_t offset;
  int32_t immediate;
};

struct shiri_offsets {
  uint16_t family, user_family, type, protocol, port;
};

struct shiri_program {
  uint32_t id, type, length;
  char tag[17];
  unsigned char instructions[SHIRI_MAX_TRANSLATED];
};

struct shiri_query {
  uint32_t count, flags, ids[SHIRI_MAX_PROGRAMS];
};

struct shiri_proof {
  struct shiri_program inet4, inet6;
};

struct shiri_operations {
  void *context;
  int (*validate)(void *, int, uint64_t, uint64_t, const char *);
  int (*load)(void *, int, unsigned int);
  int (*info)(void *, int, struct shiri_program *);
  int (*query)(void *, int, int, int, struct shiri_query *);
  int (*attach)(void *, int, int, int);
  int (*detach)(void *, int, int, int);
  int (*open_id)(void *, uint32_t);
  void (*close_fd)(void *, int);
};

static int
shiri_port_valid(unsigned int port)
{
  return port >= 3869 && port <= 3939 && (port - 3869) % 10 == 0;
}

static int
shiri_decimal(const char *text, uint64_t maximum, uint64_t *value)
{
  uint64_t number = 0;
  const unsigned char *cursor = (const unsigned char *)text;
  if (!cursor || !*cursor || (*cursor == '0' && cursor[1]))
    return -1;
  for (; *cursor; ++cursor) {
    unsigned int digit = *cursor - '0';
    if (digit > 9 || number > maximum / 10 ||
        (number == maximum / 10 && digit > maximum % 10))
      return -1;
    number = number * 10 + digit;
  }
  *value = number;
  return 0;
}

static int
shiri_hex(const char *text, size_t length)
{
  size_t index;
  if (!text || strlen(text) != length)
    return 0;
  for (index = 0; index < length; ++index)
    if (!((text[index] >= '0' && text[index] <= '9') ||
          (text[index] >= 'a' && text[index] <= 'f')))
      return 0;
  return 1;
}

static int
shiri_boot_valid(const char *text)
{
  size_t index;
  if (!text || strlen(text) != 36)
    return 0;
  for (index = 0; index < 36; ++index) {
    if (index == 8 || index == 13 || index == 18 || index == 23) {
      if (text[index] != '-') return 0;
    } else if (!((text[index] >= '0' && text[index] <= '9') ||
                 (text[index] >= 'a' && text[index] <= 'f')))
      return 0;
  }
  return 1;
}

static size_t
shiri_emit(int ipv6, unsigned int port, uint32_t network_port,
           struct shiri_offsets offsets,
           struct shiri_instruction instructions[SHIRI_MAX_INSTRUCTIONS])
{
  size_t count = 0, family_jump, user_family_jump, udp_jump, type_jump;
  size_t tcp_jump, port_jump, udp_block, udp_protocol_jump, exit_index;
  if (!shiri_port_valid(port)) return 0;
  memset(instructions, 0, SHIRI_MAX_INSTRUCTIONS * sizeof(*instructions));
#define EMIT(c, d, s, o, i) do { \
  instructions[count++] = (struct shiri_instruction){c, d, s, o, i}; \
} while (0)
  EMIT(0xb7, 0, 0, 0, 0); /* MOV64_IMM r0=deny */
  if (ipv6) {
    EMIT(0x95, 0, 0, 0, 0); /* EXIT */
    return count;
  }
  if (offsets.family > INT16_MAX || offsets.user_family > INT16_MAX ||
      offsets.type > INT16_MAX || offsets.protocol > INT16_MAX ||
      offsets.port > INT16_MAX ||
      (offsets.family | offsets.user_family | offsets.type | offsets.protocol | offsets.port) % 4)
    return 0;
  EMIT(0x61, 2, 1, (int16_t)offsets.family, 0); /* LDXW */
  family_jump = count; EMIT(0x55, 2, 0, 0, 2); /* JNE AF_INET */
  EMIT(0x61, 2, 1, (int16_t)offsets.user_family, 0);
  user_family_jump = count; EMIT(0x55, 2, 0, 0, 2);
  EMIT(0x61, 2, 1, (int16_t)offsets.type, 0);
  udp_jump = count; EMIT(0x15, 2, 0, 0, 2); /* JEQ SOCK_DGRAM */
  type_jump = count; EMIT(0x55, 2, 0, 0, 1); /* JNE SOCK_STREAM */
  EMIT(0x61, 2, 1, (int16_t)offsets.protocol, 0);
  tcp_jump = count; EMIT(0x55, 2, 0, 0, 6); /* JNE IPPROTO_TCP */
  EMIT(0x61, 2, 1, (int16_t)offsets.port, 0);
  port_jump = count; EMIT(0x55, 2, 0, 0, (int32_t)network_port);
  EMIT(0xb7, 0, 0, 0, 1);
  EMIT(0x95, 0, 0, 0, 0);
  udp_block = count;
  EMIT(0x61, 2, 1, (int16_t)offsets.protocol, 0);
  udp_protocol_jump = count; EMIT(0x55, 2, 0, 0, 17); /* JNE UDP */
  EMIT(0xb7, 0, 0, 0, 1);
  exit_index = count; EMIT(0x95, 0, 0, 0, 0);
#define JUMP_TO(index, target) instructions[index].offset = (int16_t)((target) - (index) - 1)
  JUMP_TO(family_jump, exit_index);
  JUMP_TO(user_family_jump, exit_index);
  JUMP_TO(udp_jump, udp_block);
  JUMP_TO(type_jump, exit_index);
  JUMP_TO(tcp_jump, exit_index);
  JUMP_TO(port_jump, exit_index);
  JUMP_TO(udp_protocol_jump, exit_index);
#undef JUMP_TO
#undef EMIT
  return count;
}

static int
shiri_program_matches(const struct shiri_program *expected,
                      const struct shiri_program *actual)
{
  uint32_t index;
  if (expected->length > SHIRI_MAX_TRANSLATED) return 0;
  for (index = 0; index < expected->length; ++index)
    if (expected->instructions[index]) break;
  if (index == expected->length) return 0;
  return expected->id && actual->id && expected->type == SHIRI_SOCK_ADDR_TYPE &&
         actual->type == SHIRI_SOCK_ADDR_TYPE && expected->length > 0 &&
         expected->length <= SHIRI_MAX_TRANSLATED &&
         actual->length == expected->length && shiri_hex(expected->tag, 16) &&
         !strcmp(actual->tag, expected->tag) &&
         !memcmp(actual->instructions, expected->instructions, expected->length);
}

static int
shiri_query_valid(const struct shiri_query *query)
{
  uint32_t index, other;
  if (query->count > SHIRI_MAX_PROGRAMS) return 0;
  for (index = 0; index < query->count; ++index) {
    if (!query->ids[index]) return 0;
    for (other = 0; other < index; ++other)
      if (query->ids[other] == query->ids[index]) return 0;
  }
  return 1;
}

static int
shiri_attachment_verified(const struct shiri_operations *ops, int cgroup_fd,
                          int ipv6, const struct shiri_program *reference,
                          const struct shiri_program *saved)
{
  struct shiri_query local = {0}, effective = {0};
  struct shiri_program actual = {0};
  uint32_t index;
  int descriptor = -1, result = -1;
  if (ops->query(ops->context, cgroup_fd, ipv6, 0, &local) ||
      !shiri_query_valid(&local) || local.count != 1 ||
      local.flags != SHIRI_ALLOW_MULTI || local.ids[0] != saved->id ||
      ops->query(ops->context, cgroup_fd, ipv6, 1, &effective) ||
      !shiri_query_valid(&effective))
    return -1;
  for (index = 0; index < effective.count; ++index)
    if (effective.ids[index] == saved->id) break;
  if (index == effective.count) return -1;
  descriptor = ops->open_id(ops->context, saved->id);
  if (descriptor < 0) return -1;
  if (!ops->info(ops->context, descriptor, &actual) &&
      actual.id == saved->id && !strcmp(actual.tag, saved->tag) &&
      shiri_program_matches(reference, &actual))
    result = 0;
  ops->close_fd(ops->context, descriptor);
  return result;
}

static int
shiri_policy_apply(const struct shiri_operations *ops, int attach,
                   int cgroup_fd, uint64_t device, uint64_t inode,
                   const char *boot, unsigned int port,
                   const struct shiri_proof *saved, struct shiri_proof *proof)
{
  int descriptors[2] = {-1, -1}, attached[2] = {0, 0};
  struct shiri_proof reference = {0};
  struct shiri_program *programs[2] = {&reference.inet4, &reference.inet6};
  struct shiri_query query;
  int kind, result = -1;
  memset(proof, 0, sizeof(*proof));
  if (cgroup_fd < 3 || !device || !inode || !shiri_boot_valid(boot) ||
      !shiri_port_valid(port) || (!attach && !saved) ||
      ops->validate(ops->context, cgroup_fd, device, inode, boot))
    return -1;
  if (attach) {
    for (kind = 0; kind < 2; ++kind) {
      memset(&query, 0, sizeof(query));
      if (ops->query(ops->context, cgroup_fd, kind, 0, &query) ||
          !shiri_query_valid(&query) || query.count) goto cleanup;
      if (ops->query(ops->context, cgroup_fd, kind, 1, &query) ||
          !shiri_query_valid(&query)) goto cleanup;
    }
  }
  for (kind = 0; kind < 2; ++kind) {
    descriptors[kind] = ops->load(ops->context, kind, port);
    if (descriptors[kind] < 0 ||
        ops->info(ops->context, descriptors[kind], programs[kind]) ||
        !shiri_program_matches(programs[kind], programs[kind])) goto cleanup;
    if (attach) {
      if (ops->attach(ops->context, cgroup_fd, kind, descriptors[kind])) goto cleanup;
      attached[kind] = 1;
    }
  }
  if (attach) *proof = reference;
  else *proof = *saved;
  if (shiri_attachment_verified(ops, cgroup_fd, 0, &reference.inet4, &proof->inet4) ||
      shiri_attachment_verified(ops, cgroup_fd, 1, &reference.inet6, &proof->inet6) ||
      ops->validate(ops->context, cgroup_fd, device, inode, boot)) goto cleanup;
  result = 0;
cleanup:
  if (result) {
    memset(proof, 0, sizeof(*proof));
    for (kind = 1; kind >= 0; --kind)
      if (attached[kind])
        (void)ops->detach(ops->context, cgroup_fd, kind, descriptors[kind]);
  }
  for (kind = 0; kind < 2; ++kind)
    if (descriptors[kind] >= 0) ops->close_fd(ops->context, descriptors[kind]);
  return result;
}

#if defined(__linux__) && !defined(SHIRI_POLICY_PORTABLE_TEST)
#include <arpa/inet.h>
#include <fcntl.h>
#include <linux/bpf.h>
#include <linux/magic.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/vfs.h>
#include <unistd.h>

_Static_assert(BPF_F_ALLOW_MULTI == SHIRI_ALLOW_MULTI, "BPF attach flag ABI");
_Static_assert(BPF_PROG_TYPE_CGROUP_SOCK_ADDR == SHIRI_SOCK_ADDR_TYPE, "BPF program type ABI");

static int
kernel_call(enum bpf_cmd command, union bpf_attr *attributes)
{
  return (int)syscall(SYS_bpf, command, attributes, sizeof(*attributes));
}

static int
kernel_validate(void *unused, int descriptor, uint64_t device,
                uint64_t inode, const char *boot)
{
  struct stat info;
  struct statfs filesystem;
  char current_boot[38] = {0};
  int boot_fd;
  ssize_t length;
  (void)unused;
  if (geteuid() != 0 || fstat(descriptor, &info) || fstatfs(descriptor, &filesystem) ||
      !S_ISDIR(info.st_mode) || info.st_uid != 0 || (info.st_mode & 0022) ||
      (uint64_t)info.st_dev != device || (uint64_t)info.st_ino != inode ||
      filesystem.f_type != CGROUP2_SUPER_MAGIC) return -1;
  boot_fd = open("/proc/sys/kernel/random/boot_id", O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
  if (boot_fd < 0) return -1;
  length = read(boot_fd, current_boot, sizeof(current_boot));
  close(boot_fd);
  return length == 37 && current_boot[36] == '\n' && !memcmp(current_boot, boot, 36) ? 0 : -1;
}

static int
kernel_load(void *unused, int ipv6, unsigned int port)
{
  struct shiri_instruction emitted[SHIRI_MAX_INSTRUCTIONS];
  struct bpf_insn instructions[SHIRI_MAX_INSTRUCTIONS] = {0};
  struct shiri_offsets offsets = {
    offsetof(struct bpf_sock_addr, family), offsetof(struct bpf_sock_addr, user_family),
    offsetof(struct bpf_sock_addr, type), offsetof(struct bpf_sock_addr, protocol),
    offsetof(struct bpf_sock_addr, user_port)
  };
  union bpf_attr attributes = {0};
  char log[16384] = {0};
  const char license[] = "GPL";
  size_t index, count = shiri_emit(ipv6, port, htons((uint16_t)port), offsets, emitted);
  (void)unused;
  if (!count) return -1;
  for (index = 0; index < count; ++index) {
    instructions[index].code = emitted[index].code;
    instructions[index].dst_reg = emitted[index].dst;
    instructions[index].src_reg = emitted[index].src;
    instructions[index].off = emitted[index].offset;
    instructions[index].imm = emitted[index].immediate;
  }
  attributes.prog_type = BPF_PROG_TYPE_CGROUP_SOCK_ADDR;
  attributes.expected_attach_type = ipv6 ? BPF_CGROUP_INET6_BIND : BPF_CGROUP_INET4_BIND;
  attributes.insn_cnt = (uint32_t)count;
  attributes.insns = (uintptr_t)instructions;
  attributes.license = (uintptr_t)license;
  attributes.log_buf = (uintptr_t)log;
  attributes.log_size = sizeof(log);
  attributes.log_level = 1;
  memcpy(attributes.prog_name, ipv6 ? "shiri_bind6" : "shiri_bind4", 11);
  return kernel_call(BPF_PROG_LOAD, &attributes);
}

static int
kernel_info(void *unused, int descriptor, struct shiri_program *result)
{
  union bpf_attr attributes = {0};
  struct bpf_prog_info info = {0};
  unsigned int index;
  (void)unused;
  memset(result, 0, sizeof(*result));
  info.xlated_prog_len = sizeof(result->instructions);
  info.xlated_prog_insns = (uintptr_t)result->instructions;
  attributes.info.bpf_fd = descriptor;
  attributes.info.info_len = sizeof(info);
  attributes.info.info = (uintptr_t)&info;
  if (kernel_call(BPF_OBJ_GET_INFO_BY_FD, &attributes) ||
      !info.id || !info.xlated_prog_len || info.xlated_prog_len > sizeof(result->instructions) ||
      info.xlated_prog_len % sizeof(struct bpf_insn))
    return -1;
  result->id = info.id;
  result->type = info.type;
  result->length = info.xlated_prog_len;
  for (index = 0; index < BPF_TAG_SIZE; ++index)
    (void)snprintf(result->tag + index * 2, 3, "%02x", info.tag[index]);
  return 0;
}

static int
kernel_query(void *unused, int descriptor, int ipv6, int effective, struct shiri_query *result)
{
  union bpf_attr attributes = {0};
  (void)unused;
  memset(result, 0, sizeof(*result));
  attributes.query.target_fd = descriptor;
  attributes.query.attach_type = ipv6 ? BPF_CGROUP_INET6_BIND : BPF_CGROUP_INET4_BIND;
  attributes.query.query_flags = effective ? BPF_F_QUERY_EFFECTIVE : 0;
  attributes.query.prog_cnt = SHIRI_MAX_PROGRAMS;
  attributes.query.prog_ids = (uintptr_t)result->ids;
  if (kernel_call(BPF_PROG_QUERY, &attributes)) return -1;
  result->count = attributes.query.prog_cnt;
  result->flags = attributes.query.attach_flags;
  return 0;
}

static int
kernel_attach(void *unused, int descriptor, int ipv6, int program)
{
  union bpf_attr attributes = {0};
  (void)unused;
  attributes.target_fd = descriptor;
  attributes.attach_bpf_fd = program;
  attributes.attach_type = ipv6 ? BPF_CGROUP_INET6_BIND : BPF_CGROUP_INET4_BIND;
  attributes.attach_flags = BPF_F_ALLOW_MULTI;
  return kernel_call(BPF_PROG_ATTACH, &attributes);
}

static int
kernel_detach(void *unused, int descriptor, int ipv6, int program)
{
  union bpf_attr attributes = {0};
  (void)unused;
  attributes.target_fd = descriptor;
  attributes.attach_bpf_fd = program;
  attributes.attach_type = ipv6 ? BPF_CGROUP_INET6_BIND : BPF_CGROUP_INET4_BIND;
  return kernel_call(BPF_PROG_DETACH, &attributes);
}

static int
kernel_open_id(void *unused, uint32_t identifier)
{
  union bpf_attr attributes = {0};
  (void)unused;
  attributes.prog_id = identifier;
  return kernel_call(BPF_PROG_GET_FD_BY_ID, &attributes);
}

static void
kernel_close(void *unused, int descriptor)
{
  (void)unused;
  close(descriptor);
}

int
main(int argc, char **argv)
{
  const struct shiri_operations ops = {NULL, kernel_validate, kernel_load, kernel_info,
    kernel_query, kernel_attach, kernel_detach, kernel_open_id, kernel_close};
  uint64_t descriptor, device, inode, port, id4 = 0, id6 = 0;
  struct shiri_proof saved = {0}, proof = {0};
  int attach;
  if (geteuid() != 0) {
    fputs("Bind policy installation and verification require the root broker.\n", stderr); return 1;
  }
  if (argc != 7 && argc != 11) goto invalid;
  attach = !strcmp(argv[1], "attach");
  if ((attach && argc != 7) || (!attach && (strcmp(argv[1], "verify") || argc != 11)) ||
      shiri_decimal(argv[2], INT_MAX, &descriptor) || descriptor < 3 ||
      shiri_decimal(argv[3], UINT64_MAX, &device) || !device ||
      shiri_decimal(argv[4], UINT64_MAX, &inode) || !inode ||
      !shiri_boot_valid(argv[5]) || shiri_decimal(argv[6], 65535, &port) ||
      !shiri_port_valid((unsigned int)port)) goto invalid;
  if (!attach) {
    if (shiri_decimal(argv[7], UINT32_MAX, &id4) || !id4 || !shiri_hex(argv[8], 16) ||
        shiri_decimal(argv[9], UINT32_MAX, &id6) || !id6 || !shiri_hex(argv[10], 16) || id4 == id6)
      goto invalid;
    saved.inet4.id = (uint32_t)id4; memcpy(saved.inet4.tag, argv[8], 17);
    saved.inet6.id = (uint32_t)id6; memcpy(saved.inet6.tag, argv[10], 17);
  }
  if (shiri_policy_apply(&ops, attach, (int)descriptor, device, inode, argv[5],
                         (unsigned int)port, &saved, &proof)) {
    fputs("Cannot prove the exact cgroup bind policy; daemon must remain gated.\n", stderr); return 1;
  }
  printf("{\"version\":1,\"port\":%" PRIu64 ",\"cgroup_dev\":%" PRIu64
         ",\"cgroup_inode\":%" PRIu64 ",\"boot_id\":\"%s\","
         "\"inet4\":{\"id\":%u,\"tag\":\"%s\"},\"inet6\":{\"id\":%u,\"tag\":\"%s\"},"
         "\"instructions_verified\":true,\"effective_verified\":true}\n",
         port, device, inode, argv[5], proof.inet4.id, proof.inet4.tag, proof.inet6.id, proof.inet6.tag);
  return ferror(stdout) ? 1 : 0;
invalid:
  fputs("Use attach FD DEV INO BOOT_UUID PORT or verify FD DEV INO BOOT_UUID PORT ID4 TAG4 ID6 TAG6.\n", stderr);
  return 2;
}
#endif
