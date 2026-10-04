# Loopback driver installation

Shiri's native production audio paths do not need `snd-aloop`. Virtual-device
qualification can opt in with `SHIRI_INSTALL_LOOPBACK=1`. A fresh official
Ubuntu Jammy ARM64 cloud image boots kernel `5.15.0-194-generic` without that
optional driver. For this explicit option, the installer checks availability
after its runtime APT dependencies, before compiling helpers or creating the
application virtual environment.

`kernel_modules.py` first performs an actual dry-run module probe, ignoring
an `install` replacement in modprobe configuration. An available module or
built-in driver needs no package lookup, APT refresh or kernel change. The
guarded `modprobe snd-aloop` step then loads the driver; a dry-run
success does not prove that loading will succeed on a particular host.

Automatic provisioning is limited to Ubuntu's exact running generic kernel.
The helper admits `linux-modules-extra-<running-release>`, requiring native
architecture, a recognized Ubuntu kernel source and the same package version
as the already configured exact kernel image. A missing package candidate,
Debian/custom kernel or unfinished existing package state causes a clear
failure; Shiri does not select another kernel release, a generic metapackage,
a reboot or a reinstall of an already saved extra-modules package.

The optional exact-file path in `apt_dependencies.install` preserves its
existing dependency lock, temporary deny-all service policy and needrestart
list-only environment. Its default callers retain their previous behavior.
Inside that protection, the exact-file path requires a clean dpkg audit,
refreshes APT metadata, validates candidates and rejects a simulated addition,
upgrade, removal or configuration beyond the explicitly admitted files.
If the extras package requires an absent `wireless-regdb`, that registry data
package is admitted separately. An already installed registry remains at its
existing version; a proposed registry upgrade is refused.

Each exact-version download goes into a private directory. The helper checks
its regular root-owned file identity, bounded size, metadata SHA256 and the
archive's package/version/architecture fields. It compares all saved package
states again before calling dpkg with only those archive files. Dpkg cannot
expand that request through APT's dependency solver. A dependency that changes
after simulation therefore fails configuration instead of installing a kernel
image or another dependency. The ordinary trusted package scripts and their
new triggers can finish; no pending-trigger bypass is used. Afterward every
original package must retain its original version, architecture and configured
state, the new packages must be configured, the running kernel release must
match and the loopback probe must succeed.

Installing matching modules can run the Ubuntu package's depmod and
same-release initramfs maintenance. This does not replace the running kernel
or update a kernel-image package. The helper trusts the host's root-managed
APT configuration and admitted Ubuntu package scripts. It cannot prevent an
independent administrator from changing the host concurrently. A failed dpkg
configuration can leave its explicitly admitted package unfinished; preserve
that evidence and repair the cause rather than running a broad fix-broken or
kernel upgrade automatically.

Canonical's package index lists the exact ARM64 extras package and its
image/registry dependencies. The Jammy APT manual states that simulation
does not hold package locks, which is why an accepted simulation alone is
insufficient and actual mutation uses verified archive files.
See [the Ubuntu extras package](https://packages.ubuntu.com/ca/jammy-updates/linux-modules-extra-5.15.0-194-generic),
[APT download/simulation behavior](https://manpages.ubuntu.com/manpages/jammy/man8/apt-get.8.html)
and [dpkg's file installation and trigger behavior](https://manpages.ubuntu.com/manpages/jammy/man1/dpkg.1.html).

The focused non-root regressions exercise supported/no-op and refused hosts,
candidate identity, existing image/version binding, missing registry admission,
strict transaction parsing, archive tampering, concurrent package-state changes,
postchecks and real temporary policy inode restoration on failures. These tests
do not install packages or validate an actual fresh-host install.

The separate isolated Linux reproduction passed on October 1, 2026, using
the official Ubuntu Jammy ARM64 cloud image. The exact helper installed only
the matching `5.15.0-194.204` extras and the absent registry, preserved every
original package identity and the operator policy's bytes/inode, and loaded
`snd-aloop` as a real ALSA Loopback device without changing the running kernel.
Its receipt is `/tmp/shiri-clean-os-kernel-install-result.json`. The guest used
original signed Ubuntu indexes and hash-verified archives through a private
fixture server. The first attempt retained an encoded-URL fixture failure
before installation; the corrected attempt started from the clean snapshot
and shut down gracefully. This verifies the helper's missing-driver path;
full installer, service and reboot checks remain separate.
