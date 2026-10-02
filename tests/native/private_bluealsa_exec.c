/* Test-only launcher: actual BlueALSA may create Unix sockets only. */
#define _GNU_SOURCE
#include <errno.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/prctl.h>
#include <sys/socket.h>
#include <sys/syscall.h>
#include <linux/audit.h>
#include <linux/filter.h>
#include <linux/seccomp.h>

#if defined(__x86_64__)
# define TEST_ARCH AUDIT_ARCH_X86_64
#elif defined(__aarch64__)
# define TEST_ARCH AUDIT_ARCH_AARCH64
#else
# error Only the reviewed Linux x86_64/aarch64 syscall ABIs are supported
#endif

static void guard(void)
{
  struct sock_filter instructions[] = {
    BPF_STMT(BPF_LD|BPF_W|BPF_ABS,offsetof(struct seccomp_data,arch)),
    BPF_JUMP(BPF_JMP|BPF_JEQ|BPF_K,TEST_ARCH,1,0),
    BPF_STMT(BPF_RET|BPF_K,SECCOMP_RET_KILL_PROCESS),
    BPF_STMT(BPF_LD|BPF_W|BPF_ABS,offsetof(struct seccomp_data,nr)),
    /* Also reject the x32 ABI rather than accidentally bypass the filter. */
    BPF_JUMP(BPF_JMP|BPF_JGE|BPF_K,0x40000000u,0,1),
    BPF_STMT(BPF_RET|BPF_K,SECCOMP_RET_KILL_PROCESS),
    BPF_JUMP(BPF_JMP|BPF_JEQ|BPF_K,__NR_socket,2,0),
    BPF_JUMP(BPF_JMP|BPF_JEQ|BPF_K,__NR_socketpair,1,0),
    BPF_STMT(BPF_RET|BPF_K,SECCOMP_RET_ALLOW),
    BPF_STMT(BPF_LD|BPF_W|BPF_ABS,offsetof(struct seccomp_data,args[0])),
    BPF_JUMP(BPF_JMP|BPF_JEQ|BPF_K,AF_UNIX,1,0),
    BPF_STMT(BPF_RET|BPF_K,SECCOMP_RET_ERRNO|EAFNOSUPPORT),
    BPF_STMT(BPF_RET|BPF_K,SECCOMP_RET_ALLOW),
  };
  struct sock_fprog program = {sizeof(instructions)/sizeof(instructions[0]),instructions};
  if(prctl(PR_SET_NO_NEW_PRIVS,1,0,0,0)<0 || prctl(PR_SET_SECCOMP,SECCOMP_MODE_FILTER,&program)<0) {
    perror("Private BlueALSA syscall guard"); exit(1);
  }
  const int domains[] = {AF_INET,AF_INET6,AF_NETLINK,AF_BLUETOOTH};
  for(size_t i=0;i<sizeof(domains)/sizeof(domains[0]);i++) {
    errno=0;
    int fd=socket(domains[i],SOCK_RAW,0);
    if(fd>=0 || errno!=EAFNOSUPPORT) {
      if(fd>=0)close(fd);
      fputs("Non-Unix socket guard did not hold\n",stderr);exit(1);
    }
  }
  int pair[2];
  if(socketpair(AF_UNIX,SOCK_SEQPACKET|SOCK_CLOEXEC,0,pair)<0) {
    perror("Private Unix PCM transport");exit(1);
  }
  close(pair[0]);close(pair[1]);
  fputs("Private BlueALSA socket guard enforced\n",stderr);
}

int main(int argc,char **argv)
{
  if(argc<2 || geteuid()==0) {
    fputs("Use an exact binary under an unprivileged test UID\n",stderr);return 2;
  }
  guard();
  if(argc==2 && !strcmp(argv[1],"--self-test"))return 0;
  execv(argv[1],argv+1);
  perror("Private BlueALSA exec");return 1;
}
