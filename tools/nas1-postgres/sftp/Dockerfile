# SFTP-only sshd for db1's pgBackRest (repo2). One key, one chrooted user, no shell, no forwarding.
FROM debian:trixie-slim
RUN apt-get update && apt-get install -y --no-install-recommends openssh-server && rm -rf /var/lib/apt/lists/* \
 && rm -f /etc/ssh/ssh_host_* && mkdir -p /run/sshd \
 && groupadd -g 2990 pgbackup && useradd -u 2990 -g 2990 -M -d / -s /usr/sbin/nologin -p '*' pgbackup
COPY sshd_config /etc/ssh/sshd_config
CMD ["/usr/sbin/sshd", "-D", "-e"]
