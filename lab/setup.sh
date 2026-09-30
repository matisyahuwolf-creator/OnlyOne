#!/usr/bin/env bash
# Only One lab: turns a fresh Ubuntu server (Hetzner, 16 GB) into the project's permanent home.
# Run once as root:   curl -fsSL https://raw.githubusercontent.com/matisyahuwolf-creator/OnlyOne/main/lab/setup.sh | bash
# It installs: git + git-lfs, GitHub's gh, Python, Java 21, Node 22, Claude Code, and Neo4j 5 (only reachable
# from the server itself). It makes a user "lab" to work as. No passwords are printed except the one place
# Neo4j's is saved: /home/lab/.neo4j-password (readable only by lab).
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
say(){ printf '\n\033[1m==> %s\033[0m\n' "$*"; }

say "System packages"
apt-get update -y
apt-get install -y git git-lfs curl tmux htop unzip jq python3 python3-pip python3-venv openjdk-21-jre-headless ufw ca-certificates gnupg

say "GitHub CLI"
if ! command -v gh >/dev/null; then
  curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg -o /usr/share/keyrings/githubcli-archive-keyring.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/github-cli.list
  apt-get update -y && apt-get install -y gh
fi

say "Node 22 and Claude Code"
if ! command -v node >/dev/null || ! node -v | grep -q '^v2[2-9]'; then
  curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
  apt-get install -y nodejs
fi
npm install -g @anthropic-ai/claude-code

say "The lab user"
id lab >/dev/null 2>&1 || useradd -m -s /bin/bash lab
echo "lab ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/lab && chmod 440 /etc/sudoers.d/lab
[ -f /root/.ssh/authorized_keys ] && mkdir -p /home/lab/.ssh && cp /root/.ssh/authorized_keys /home/lab/.ssh/ && chown -R lab:lab /home/lab/.ssh && chmod 700 /home/lab/.ssh

say "Neo4j 5 (local only)"
if [ ! -d /opt/neo4j ]; then
  curl -fsSL https://dist.neo4j.org/neo4j-community-5.26.0-unix.tar.gz -o /tmp/neo4j.tgz
  tar xzf /tmp/neo4j.tgz -C /opt && mv /opt/neo4j-community-5.26.0 /opt/neo4j && rm /tmp/neo4j.tgz
  cat >> /opt/neo4j/conf/neo4j.conf <<'CONF'
server.default_listen_address=127.0.0.1
server.memory.heap.initial_size=4g
server.memory.heap.max_size=4g
server.memory.pagecache.size=6g
CONF
  PW=$(head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 24)
  /opt/neo4j/bin/neo4j-admin dbms set-initial-password "$PW" >/dev/null
  printf 'NEO4J_URI=bolt://127.0.0.1:7687\nNEO4J_USERNAME=neo4j\nNEO4J_PASSWORD=%s\n' "$PW" > /home/lab/.neo4j-password
  chown lab:lab /home/lab/.neo4j-password && chmod 600 /home/lab/.neo4j-password
  chown -R lab:lab /opt/neo4j
  cat > /etc/systemd/system/neo4j.service <<'UNIT'
[Unit]
Description=Neo4j (Only One lab)
After=network.target
[Service]
User=lab
ExecStart=/opt/neo4j/bin/neo4j console
Restart=on-failure
LimitNOFILE=60000
[Install]
WantedBy=multi-user.target
UNIT
  systemctl daemon-reload && systemctl enable --now neo4j
fi

say "Firewall: only SSH comes in"
ufw allow OpenSSH >/dev/null && ufw --force enable >/dev/null

say "Done. Next, as the lab user:"
cat <<'NEXT'

  su - lab
  gh auth login            # choose GitHub.com, HTTPS, "Login with a web browser"; open the link it shows
  gh repo clone matisyahuwolf-creator/Research && cd Research && git lfs pull
  claude                   # log in to your Claude account once (it shows a link), then type /exit
  tmux new -s lab 'claude remote-control'

Then open the Claude app: the lab shows up as a session you can talk to.
To come back to it later:  su - lab  then  tmux attach -t lab
NEXT
