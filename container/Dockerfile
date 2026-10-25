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
      sudo \
      unzip \
      wget \
      whois \
      yq && \
    apt clean && \
    rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*

RUN useradd -m -s /bin/bash kali && \
    echo "kali ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers

USER kali

RUN pip3 install --break-system-packages --no-cache-dir \
      beautifulsoup4 \
      httpx \
      lxml \
      requests && \
    sudo npm install -g @openai/codex@0.118.0

COPY ./crypto-requirements.txt /tmp/crypto-requirements.txt
COPY ./crypto_selfcheck.py /usr/local/share/echo-agent/crypto_selfcheck.py
RUN pip3 install --break-system-packages --user --no-cache-dir --only-binary=:all: \
      -r /tmp/crypto-requirements.txt && \
    /usr/bin/python3 /usr/local/share/echo-agent/crypto_selfcheck.py

RUN printf '%s\n' 'PS1="${PS1%\\n*} \[\033[0;36m\]\D{%y-%m-%d} - \t\[\033[;32m\]\n${PS1##*\\n}"' >> /home/kali/.bashrc && \
    mkdir -p /home/kali/.local/bin && \
    mkdir -p /home/kali/workspace && \
    cd /home/kali/workspace && \
    git init

ENV TZ=Asia/Shanghai
ENV PYTHONUNBUFFERED=1

# Media/forensics tools live in a final layer so the existing toolchain stays cached.
USER root
RUN apt update && \
    apt install -y --no-install-recommends \
      binutils \
      ffmpeg && \
    apt clean && \
    rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*
USER kali

COPY ./.agents /home/kali/workspace/.agents
COPY ./AGENTS.md /home/kali/workspace/AGENTS.md

WORKDIR /home/kali/workspace
