#!/bin/bash
# Start the daemons for one role (head or node), then idle until stopped.
#   head: munged, sshd, slurmctld     node: munged, slurmd
# Inputs, all mounted read-only by the harness: /run/cluster/munge.key,
# and for the head /run/cluster/authorized_keys.
set -euo pipefail
role="$1"

install -d -o munge -g munge -m 0755 /run/munge /var/log/munge /var/lib/munge
install -o munge -g munge -m 0400 /run/cluster/munge.key /etc/munge/munge.key
gosu_munge() { setpriv --reuid=munge --regid=munge --init-groups "$@"; }
gosu_munge munged

case "$role" in
head)
  user="$(getent passwd "$CLUSTER_UID" | cut -d: -f1)"
  install -d -o "$user" -g "$user" -m 0700 "/home/$user/.ssh"
  install -o "$user" -g "$user" -m 0600 /run/cluster/authorized_keys "/home/$user/.ssh/authorized_keys"
  ssh-keygen -A >/dev/null
  /usr/sbin/sshd -o PasswordAuthentication=no -o UsePAM=no -o PermitRootLogin=no
  # Accounting: MariaDB, then slurmdbd, then the controller that reports to it.
  install -d -o mysql -g mysql /run/mysqld
  setpriv --reuid=mysql --regid=mysql --init-groups mariadbd --skip-networking &
  until mariadb-admin ping --silent 2>/dev/null; do sleep 0.1; done
  mariadb -e "CREATE DATABASE IF NOT EXISTS slurm_acct_db;
    CREATE USER IF NOT EXISTS 'slurm'@'localhost' IDENTIFIED BY 'slurm';
    GRANT ALL ON slurm_acct_db.* TO 'slurm'@'localhost';"
  slurmdbd
  until sacctmgr -n list cluster >/dev/null 2>&1; do sleep 0.1; done
  slurmctld
  ;;
node)
  # slurmd keeps its step daemons in a cgroup scope that systemd would create.
  mkdir -p /sys/fs/cgroup/system.slice
  slurmd -N node1
  ;;
*)
  echo "unknown role: $role" >&2
  exit 2
  ;;
esac
touch /run/cluster/ready 2>/dev/null || touch /tmp/ready
exec sleep infinity
