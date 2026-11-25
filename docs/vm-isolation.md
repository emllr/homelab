# Isolating internet-facing VMs

**Goal:** a compromised web VM should reach nothing but the internet ports it needs and the specific services it
uses: not the hypervisor, the LAN or other VMs.

## Design
- **One network per trust level.** Each is a libvirt NAT network whose bridge ports are isolated, so guests on the
  same network can't reach each other either.
- **A drop-only nftables table.** In nftables a packet must be accepted by every base chain on a hook, so a drop
  here is final whatever ufw or libvirt allow. The table never needs to know about their rules. It is loaded by its
  own unit, because `nftables.service` starts with `flush ruleset`, which would wipe theirs.
- **Relays instead of routes.** Nothing may open a connection into an isolated network by routing. Each permitted
  flow is a socket-activated `systemd-socket-proxyd` on the hypervisor with one listener, one target and a client
  allowlist.
- **Outbound is an allowlist** of ports, to public addresses only. Blocking private ranges also limits SSRF.
- **DNS** goes to public resolvers, not the router, so guests can't list local names.

## Trade-offs
- The server behind a relay sees the bridge gateway as its client. Who may connect is enforced on the hypervisor;
  roles and passwords do the rest.
- libvirt applies a changed network only after a restart, which detaches running guests. That step stays manual.
- A relay's connection limit has to be sized to the clients' pools or it becomes the bottleneck.

Role: [`roles/vm_isolation`](../roles/vm_isolation). The example defaults describe two networks and one relay.
