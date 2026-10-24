FROM --platform=amd64 kalilinux/kali-rolling:latest

ENV DEBIAN_FRONTEND=noninteractive
ENV WGET_RETRY_FLAGS="--no-verbose --tries=5 --timeout=30 --waitretry=5 --retry-connrefused"
ENV PATH="/home/kali/.local/bin:${PATH}"

RUN apt update && \
    apt install -y --no-install-recommends \
      bash \
      ca-certificates \
      curl \
      dnsutils \
      fd-find \
      file \
      git \
      iproute2 \
      iputils-ping \
      jq \
      less \
      netcat-traditional \
      nmap \
      nodejs \
      npm \
      openssh-client \
      python3 \
      python3-pip \
      ripgrep \
      openjdk-21-jdk-headless \
      sudo \
      unzip \
      wget \
      whois \
      yq && \
    apt clean && \
    rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*


ENV GHIDRA_INSTALL_DIR=/opt/ghidra
ENV JAVA_HOME=/usr/lib/jvm/java-21-openjdk
RUN java_home="$(dirname "$(dirname "$(readlink -f "$(command -v java)")")")" && \
    ln -sfn "$java_home" /usr/lib/jvm/java-21-openjdk && \
    wget ${WGET_RETRY_FLAGS} -O /tmp/ghidra.zip \
      https://github.com/NationalSecurityAgency/ghidra/releases/download/Ghidra_12.1.4_build/ghidra_12.1.4_PUBLIC_20260921.zip && \
    unzip -q /tmp/ghidra.zip -d /opt && \
    mv /opt/ghidra_12.1.4_PUBLIC /opt/ghidra && \
    rm /tmp/ghidra.zip && \
    pip3 install --break-system-packages --no-cache-dir pyghidra-mcp==0.2.7

RUN useradd -m -s /bin/bash kali && \
    echo "kali ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers

USER kali

RUN pip3 install --break-system-packages --no-cache-dir \
      beautifulsoup4 \
      httpx \
      lxml \
      requests && \
    sudo npm install -g @openai/codex@0.118.0

RUN printf '%s\n' 'PS1="${PS1%\\n*} \[\033[0;36m\]\D{%y-%m-%d} - \t\[\033[;32m\]\n${PS1##*\\n}"' >> /home/kali/.bashrc && \
    mkdir -p /home/kali/.local/bin && \
    mkdir -p /home/kali/workspace && \
    cd /home/kali/workspace && \
    git init

ENV TZ=Asia/Shanghai
ENV PYTHONUNBUFFERED=1

COPY ./.agents /home/kali/workspace/.agents
COPY ./AGENTS.md /home/kali/workspace/AGENTS.md

WORKDIR /home/kali/workspace

COPY ./entrypoint.sh /opt/echo-agent/entrypoint.sh
COPY ./codex-config.toml /opt/echo-agent/codex-config.toml
USER root
RUN chmod 755 /opt/echo-agent/entrypoint.sh && \
    mkdir -p /home/kali/.codex /home/kali/ghidra-projects && \
    cp /opt/echo-agent/codex-config.toml /home/kali/.codex/config.toml && \
    chown -R kali:kali /home/kali/.codex /home/kali/ghidra-projects /opt/echo-agent
USER kali

ENTRYPOINT ["/opt/echo-agent/entrypoint.sh"]
CMD ["sleep", "infinity"]
