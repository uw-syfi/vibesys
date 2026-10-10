# A one-node-per-container Slurm cluster for the slurm_cluster test tier.
# Slurm comes from the distribution packages, so a build is an apt install.
FROM ubuntu:24.04

ARG USER_NAME
ARG USER_UID
ARG USER_GID

RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      slurm-wlm slurmdbd mariadb-server munge openssh-server rsync bubblewrap python3 procps ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# The Slurm user must exist, with the host user's ids, on every node: files in the
# shared directory keep their numeric owner and the job runs as this user.
RUN (getent passwd "${USER_UID}" | cut -d: -f1 | xargs -r userdel -r || true) \
 && (getent group "${USER_GID}" | cut -d: -f1 | xargs -r groupdel || true) \
 && groupadd --gid "${USER_GID}" "${USER_NAME}" \
 && useradd --uid "${USER_UID}" --gid "${USER_GID}" --create-home --shell /bin/bash \
      --password '*' "${USER_NAME}"

COPY slurm.conf gres.conf cgroup.conf /etc/slurm/
COPY slurmdbd.conf /etc/slurm/slurmdbd.conf
COPY mariadb-test.cnf /etc/mysql/conf.d/zz-test.cnf
COPY entrypoint.sh /usr/local/bin/cluster-entrypoint
RUN chmod 0755 /usr/local/bin/cluster-entrypoint \
 && install -d -o slurm -g slurm /var/spool/slurmctld /var/log/slurm \
 && chown slurm:slurm /etc/slurm/slurmdbd.conf && chmod 0600 /etc/slurm/slurmdbd.conf \
 && install -d /var/spool/slurmd /run/sshd

ENTRYPOINT ["/usr/local/bin/cluster-entrypoint"]
